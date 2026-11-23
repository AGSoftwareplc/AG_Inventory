"""Products and categories."""
import sqlite3

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from core import RuleError, audit, fail, get_db, parse_money, parse_qty, requires, tx
from rules import move_stock

bp = Blueprint('products', __name__, url_prefix='/products')

UNITS = ['pcs', 'kg', 'g', 'litre', 'ml', 'm', 'box', 'pack', 'dozen', 'bottle', 'bag']


def categories(active_only=False):
    sql = 'SELECT * FROM categories' + (' WHERE active=1' if active_only else '') + ' ORDER BY name'
    return get_db().execute(sql).fetchall()


@bp.route('/')
@requires('products', 'inventory', 'pos')
def index():
    q = request.args.get('q', '').strip()
    cat = request.args.get('cat', '')
    status = request.args.get('status', 'active')
    sql = ('SELECT p.*, c.name AS cat FROM products p LEFT JOIN categories c ON c.id=p.category_id '
           'WHERE 1=1')
    args = []
    if q:
        sql += ' AND (p.name LIKE ? OR p.code LIKE ? OR p.barcode = ?)'
        args += [f'%{q}%', f'%{q}%', q]
    if cat:
        sql += ' AND p.category_id=?'
        args.append(cat)
    if status == 'active':
        sql += ' AND p.active=1'
    elif status == 'inactive':
        sql += ' AND p.active=0'
    elif status == 'low':
        sql += ' AND p.active=1 AND p.min_stock>0 AND p.stock<=p.min_stock'
    rows = get_db().execute(sql + ' ORDER BY p.name LIMIT 1000', args).fetchall()
    return render_template('products/index.html', rows=rows, cats=categories(), q=q, cat=cat,
                           status=status)


def _form_values(f, is_new):
    v = {
        'code': f.get('code', '').strip(), 'name': f.get('name', '').strip(),
        'category_id': int(f['category_id']) if f.get('category_id') else None,
        'description': f.get('description', '').strip(), 'unit': f.get('unit', 'pcs').strip() or 'pcs',
        'barcode': f.get('barcode', '').strip() or None,
        'price': parse_money(f.get('price'), 'Selling price'),
        'min_stock': parse_qty(f.get('min_stock') or '0', 'Minimum stock', allow_zero=True),
    }
    if not v['code'] or not v['name']:
        raise RuleError('Product code and name are required.')
    if is_new:
        v['cost'] = parse_money(f.get('cost'), 'Purchase cost')
        v['opening'] = parse_qty(f.get('opening') or '0', 'Opening stock', allow_zero=True)
    return v


@bp.route('/new', methods=['GET', 'POST'])
@bp.route('/<int:pid>/edit', methods=['GET', 'POST'])
@requires('products')
def edit(pid=None):
    db = get_db()
    p = db.execute('SELECT * FROM products WHERE id=?', (pid,)).fetchone() if pid else None
    if pid and not p:
        return fail('Product not found.', 'products.index')
    if request.method == 'POST':
        try:
            v = _form_values(request.form, p is None)
            with tx() as db:
                if p is None:
                    pid = db.execute(
                        'INSERT INTO products(code, name, category_id, description, unit, barcode, '
                        'price, avg_cost, min_stock) VALUES (?,?,?,?,?,?,?,?,?)',
                        (v['code'], v['name'], v['category_id'], v['description'], v['unit'],
                         v['barcode'], v['price'], v['cost'], v['min_stock'])).lastrowid
                    if v['opening'] > 0:
                        move_stock(db, pid, v['opening'], 'opening', g.user['id'], 'opening',
                                   reason='Opening stock', in_cost=v['cost'])
                    audit('product_create', v['code'], db)
                else:
                    db.execute('UPDATE products SET code=?, name=?, category_id=?, description=?, '
                               'unit=?, barcode=?, price=?, min_stock=?, active=? WHERE id=?',
                               (v['code'], v['name'], v['category_id'], v['description'], v['unit'],
                                v['barcode'], v['price'], v['min_stock'],
                                1 if request.form.get('active') else 0, pid))
                    if v['price'] != p['price']:
                        db.execute('INSERT INTO price_changes(product_id, old_price, new_price, '
                                   'user_id) VALUES (?,?,?,?)', (pid, p['price'], v['price'], g.user['id']))
                        audit('price_change', f"{v['code']}: {p['price']} -> {v['price']}", db)
            flash('Product saved.', 'ok')
            return redirect(url_for('products.view', pid=pid))
        except sqlite3.IntegrityError:
            flash('Another product already uses this code or barcode.', 'error')
        except RuleError as e:
            flash(str(e), 'error')
    return render_template('products/edit.html', p=p, f=request.form, cats=categories(True),
                           units=UNITS)


@bp.route('/<int:pid>')
@requires('products', 'inventory')
def view(pid):
    db = get_db()
    p = db.execute('SELECT p.*, c.name AS cat FROM products p LEFT JOIN categories c '
                   'ON c.id=p.category_id WHERE p.id=?', (pid,)).fetchone()
    if not p:
        return fail('Product not found.', 'products.index')
    moves = db.execute('SELECT m.*, u.username FROM stock_moves m LEFT JOIN users u ON u.id=m.user_id '
                       'WHERE product_id=? ORDER BY m.id DESC LIMIT 50', (pid,)).fetchall()
    prices = db.execute('SELECT pc.*, u.username FROM price_changes pc LEFT JOIN users u '
                        'ON u.id=pc.user_id WHERE product_id=? ORDER BY pc.id DESC LIMIT 20',
                        (pid,)).fetchall()
    return render_template('products/view.html', p=p, moves=moves, prices=prices)


@bp.route('/<int:pid>/delete', methods=['POST'])
@requires('products')
def delete(pid):
    db = get_db()
    used = db.execute('SELECT 1 FROM stock_moves WHERE product_id=? UNION SELECT 1 FROM sale_items '
                      'WHERE product_id=? UNION SELECT 1 FROM purchase_items WHERE product_id=? '
                      'LIMIT 1', (pid, pid, pid)).fetchone()
    if used:
        return fail('This product has transaction history and cannot be deleted. '
                    'Deactivate it instead.', 'products.edit', pid=pid)
    with tx() as db:
        db.execute('DELETE FROM price_changes WHERE product_id=?', (pid,))
        db.execute('DELETE FROM products WHERE id=?', (pid,))
        audit('product_delete', str(pid), db)
    flash('Product deleted.', 'ok')
    return redirect(url_for('products.index'))


# ---------------------------------------------------------------- categories
@bp.route('/categories', methods=['GET', 'POST'])
@requires('products')
def cats():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        cid = request.form.get('id')
        if not name:
            flash('Category name is required.', 'error')
        else:
            try:
                with tx() as db:
                    if cid:
                        db.execute('UPDATE categories SET name=?, active=? WHERE id=?',
                                   (name, 1 if request.form.get('active') else 0, cid))
                    else:
                        db.execute('INSERT INTO categories(name) VALUES (?)', (name,))
                flash('Category saved.', 'ok')
            except sqlite3.IntegrityError:
                flash('A category with this name already exists.', 'error')
        return redirect(url_for('products.cats'))
    rows = get_db().execute('SELECT c.*, (SELECT COUNT(*) FROM products p WHERE p.category_id=c.id) '
                            'AS n FROM categories c ORDER BY name').fetchall()
    return render_template('products/categories.html', rows=rows)
