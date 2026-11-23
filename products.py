"""Products and categories."""
import sqlite3

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from core import RuleError, audit, branch_id, claim_once, fail, get_db, parse_money, parse_qty, requires, tx
import core
from rules import move_stock

bp = Blueprint('products', __name__, url_prefix='/products')



def categories(active_only=False):
    sql = 'SELECT * FROM categories' + (' WHERE active=1' if active_only else '') + ' ORDER BY name'
    return get_db().execute(sql).fetchall()


@bp.route('/')
@requires('products', 'inventory', 'pos')
def index():
    q = request.args.get('q', '').strip()
    cat = request.args.get('cat', '')
    status = request.args.get('status', 'active')
    sql = ('SELECT p.*, c.name AS cat FROM pv p LEFT JOIN categories c ON c.id=p.category_id '
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
        sql += ' AND p.active=1 AND p.min_stock>0 AND p.in_branch AND p.stock<=p.min_stock'
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


def _create(db, v):
    """Insert one new product (inside tx) with its opening stock; returns the new id."""
    pid = db.execute('INSERT INTO products(code, name, category_id, description, unit, barcode, '
                     'price, avg_cost, min_stock) VALUES (?,?,?,?,?,?,?,?,?)',
                     (v['code'], v['name'], v['category_id'], v['description'], v['unit'],
                      v['barcode'], v['price'], v['cost'], v['min_stock'])).lastrowid
    # the branch that registers a product carries it (other branches start carrying it when stock arrives)
    db.execute('INSERT INTO product_stock(product_id, branch_id, stock, avg_cost) VALUES (?,?,0,?)',
               (pid, branch_id(), v['cost']))
    if v['opening'] > 0:
        move_stock(db, pid, v['opening'], 'opening', g.user['id'], 'opening',
                   reason='Opening stock', in_cost=v['cost'])
    audit('product_create', v['code'], db)
    return pid


BULK_FIELDS = ['code', 'name', 'category_id', 'unit', 'barcode', 'price', 'cost', 'opening', 'min_stock']


@bp.route('/bulk', methods=['GET', 'POST'])
@requires('products')
def bulk():
    """Several new products in one grid: add rows as needed, then save them all at once.
    Nothing is saved while any row has an error; the rows keep what was typed."""
    rows, errors = [], {}
    if request.method == 'POST':
        cols = {k: request.form.getlist(k) for k in BULK_FIELDS}
        n = max(len(c) for c in cols.values())
        rows = [{k: (cols[k][i] if i < len(cols[k]) else '').strip() for k in BULK_FIELDS} for i in range(n)]
        rows = [r for r in rows if any(r[k] for k in ('code', 'name', 'barcode', 'price', 'cost', 'opening'))]
        db = get_db()
        seen_codes, seen_barcodes, values = {}, {}, []
        for i, r in enumerate(rows):
            try:
                v = _form_values(r, True)
                if not r['price']:
                    raise RuleError('Selling price is required.')
                code, bc = v['code'].lower(), v['barcode']
                if code in seen_codes:
                    raise RuleError(f"Code {v['code']} is also used in row {seen_codes[code] + 1}.")
                if bc and bc in seen_barcodes:
                    raise RuleError(f'Barcode {bc} is also used in row {seen_barcodes[bc] + 1}.')
                if db.execute('SELECT 1 FROM products WHERE code=?', (v['code'],)).fetchone():
                    raise RuleError(f"Code {v['code']} already belongs to another product.")
                if bc and db.execute('SELECT 1 FROM products WHERE barcode=?', (bc,)).fetchone():
                    raise RuleError(f'Barcode {bc} already belongs to another product.')
                seen_codes[code] = i
                if bc:
                    seen_barcodes[bc] = i
                values.append(v)
            except (RuleError, ValueError) as e:
                errors[i] = str(e) if isinstance(e, RuleError) else 'Check the values in this row.'
        if not rows:
            flash('Fill in at least one row.', 'error')
        elif errors:
            flash(f'Nothing was saved yet: {len(errors)} row(s) need a correction (shown in red).', 'error')
        else:
            try:
                with tx() as db:
                    claim_once(db)
                    for v in values:
                        _create(db, v)
                flash(f'{len(values)} product(s) saved.', 'ok')
                return redirect(url_for('products.index'))
            except sqlite3.IntegrityError:
                flash('Another product already uses one of these codes or barcodes.', 'error')
            except RuleError as e:
                flash(str(e), 'error')
    return render_template('products/bulk.html', rows=rows, errors=errors, cats=categories(True), units=core.units())


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
                    pid = _create(db, v)
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
                           units=core.units(p['unit'] if p else None))


@bp.route('/<int:pid>')
@requires('products', 'inventory')
def view(pid):
    db = get_db()
    p = db.execute('SELECT p.*, c.name AS cat FROM pv p LEFT JOIN categories c '
                   'ON c.id=p.category_id WHERE p.id=?', (pid,)).fetchone()
    if not p:
        return fail('Product not found.', 'products.index')
    moves = db.execute('SELECT m.*, u.username FROM stock_moves m LEFT JOIN users u ON u.id=m.user_id '
                       'WHERE product_id=? AND m.branch_id=? ORDER BY m.id DESC LIMIT 50',
                       (pid, branch_id())).fetchall()
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
        db.execute('DELETE FROM product_stock WHERE product_id=?', (pid,))
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
