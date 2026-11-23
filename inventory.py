"""Stock levels, adjustments, movement history and stocktaking."""
from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from core import RuleError, date_range, fail, get_db, parse_qty, requires, today, tx
import rules

bp = Blueprint('inventory', __name__, url_prefix='/inventory')


@bp.route('/')
@requires('inventory')
def index():
    q = request.args.get('q', '').strip()
    cat = request.args.get('cat', '')
    low = request.args.get('low') == '1'
    sql = ('SELECT p.*, c.name AS cat FROM products p LEFT JOIN categories c ON c.id=p.category_id '
           'WHERE p.active=1')
    args = []
    if q:
        sql += ' AND (p.name LIKE ? OR p.code LIKE ? OR p.barcode=?)'
        args += [f'%{q}%', f'%{q}%', q]
    if cat:
        sql += ' AND p.category_id=?'
        args.append(cat)
    if low:
        sql += ' AND p.min_stock>0 AND p.stock<=p.min_stock'
    rows = get_db().execute(sql + ' ORDER BY p.name LIMIT 2000', args).fetchall()
    value = get_db().execute('SELECT COALESCE(SUM(stock*avg_cost),0) FROM products WHERE active=1 '
                             'AND stock>0').fetchone()[0]
    cats = get_db().execute('SELECT * FROM categories ORDER BY name').fetchall()
    return render_template('inventory/index.html', rows=rows, value=value, cats=cats, q=q, cat=cat,
                           low=low)


@bp.route('/adjust', methods=['GET', 'POST'])
@requires('adjust')
def adjust():
    db = get_db()
    if request.method == 'POST':
        try:
            kind = request.form.get('kind')
            qty = parse_qty(request.form.get('qty'), allow_negative=(kind == 'correction'))
            with tx() as db:
                rules.adjust_stock(db, int(request.form['product_id']), kind, qty,
                                   request.form.get('reason', ''), g.user['id'])
            flash('Stock adjusted.', 'ok')
            return redirect(url_for('inventory.adjust'))
        except (RuleError, ValueError, KeyError) as e:
            flash(str(e) if isinstance(e, RuleError) else 'Choose a product.', 'error')
    products = db.execute('SELECT id, code, name, unit, stock FROM products WHERE active=1 '
                          'ORDER BY name').fetchall()
    recent = db.execute("SELECT m.*, p.name, p.unit, u.username FROM stock_moves m JOIN products p "
                        "ON p.id=m.product_id LEFT JOIN users u ON u.id=m.user_id WHERE m.ref_type="
                        "'adjustment' ORDER BY m.id DESC LIMIT 30").fetchall()
    return render_template('inventory/adjust.html', products=products, recent=recent,
                           sel=request.args.get('pid', type=int), labels=rules.MOVE_LABELS)


@bp.route('/movements')
@requires('inventory')
def movements():
    start, end = date_range()
    mtype = request.args.get('type', '')
    pid = request.args.get('pid', type=int)
    sql = ('SELECT m.*, p.name, p.code, p.unit, u.username FROM stock_moves m JOIN products p '
           'ON p.id=m.product_id LEFT JOIN users u ON u.id=m.user_id WHERE date(m.at) BETWEEN ? AND ?')
    args = [start, end]
    if mtype:
        sql += ' AND m.move_type=?'
        args.append(mtype)
    if pid:
        sql += ' AND m.product_id=?'
        args.append(pid)
    rows = get_db().execute(sql + ' ORDER BY m.id DESC LIMIT 2000', args).fetchall()
    return render_template('inventory/movements.html', rows=rows, start=start, end=end, mtype=mtype,
                           labels=rules.MOVE_LABELS)


# ---------------------------------------------------------------- stocktake
@bp.route('/stocktakes', methods=['GET', 'POST'])
@requires('adjust')
def stocktakes():
    db = get_db()
    if request.method == 'POST':
        try:
            with tx() as db:
                st = rules.start_stocktake(db, request.form.get('notes', '').strip(),
                                           request.form.get('category_id') or None, g.user['id'], today())
            return redirect(url_for('inventory.stocktake', st_id=st))
        except RuleError as e:
            flash(str(e), 'error')
    rows = db.execute('SELECT s.*, u.username, (SELECT COUNT(*) FROM stocktake_items i WHERE '
                      'i.stocktake_id=s.id) AS n FROM stocktakes s LEFT JOIN users u ON u.id=s.user_id '
                      'ORDER BY s.id DESC').fetchall()
    cats = db.execute('SELECT * FROM categories WHERE active=1 ORDER BY name').fetchall()
    return render_template('inventory/stocktakes.html', rows=rows, cats=cats)


@bp.route('/stocktakes/<int:st_id>', methods=['GET', 'POST'])
@requires('adjust')
def stocktake(st_id):
    db = get_db()
    st = db.execute('SELECT * FROM stocktakes WHERE id=?', (st_id,)).fetchone()
    if not st:
        return fail('Stocktake not found.', 'inventory.stocktakes')
    if request.method == 'POST' and st['status'] == 'Open':
        try:
            with tx() as db:
                for key, val in request.form.items():
                    if key.startswith('c_'):
                        val = val.strip()
                        counted = parse_qty(val, 'Counted quantity', allow_zero=True) if val else None
                        db.execute('UPDATE stocktake_items SET counted_qty=? WHERE stocktake_id=? '
                                   'AND product_id=?', (counted, st_id, int(key[2:])))
                if request.form.get('post'):
                    rules.post_stocktake(db, st_id, g.user['id'])
            flash('Stocktake posted. Differences were applied to stock.' if request.form.get('post')
                  else 'Counts saved.', 'ok')
            return redirect(url_for('inventory.stocktake', st_id=st_id))
        except RuleError as e:
            flash(str(e), 'error')
    rows = db.execute('SELECT i.*, p.name, p.code, p.unit, p.stock, p.avg_cost FROM stocktake_items i '
                      'JOIN products p ON p.id=i.product_id WHERE stocktake_id=? ORDER BY p.name',
                      (st_id,)).fetchall()
    return render_template('inventory/stocktake.html', st=st, rows=rows)
