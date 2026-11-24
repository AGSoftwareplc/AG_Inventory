"""Stock levels, adjustments, movement history and stocktaking."""
from datetime import date, timedelta

from flask import Blueprint, flash, g, jsonify, redirect, render_template, request, url_for

from core import (RuleError, branch_id, can, claim_once, date_range, fail, get_db, parse_qty, products_select, requires,
                  today, tx)
import rules

bp = Blueprint('inventory', __name__, url_prefix='/inventory')


@bp.route('/')
@requires('inventory')
def index():
    q = request.args.get('q', '').strip()
    cat = request.args.get('cat', '')
    low = request.args.get('low') == '1'
    sql = ('SELECT p.*, c.name AS cat FROM pv p LEFT JOIN categories c ON c.id=p.category_id '
           'WHERE p.active=1')
    args = []
    if q:
        sql += ' AND (p.name LIKE ? OR p.code LIKE ? OR p.barcode=?)'
        args += [f'%{q}%', f'%{q}%', q]
    if cat:
        sql += ' AND p.category_id=?'
        args.append(cat)
    if low:
        sql += ' AND p.min_stock>0 AND p.in_branch AND p.stock<=p.min_stock'
    rows = get_db().execute(sql + ' ORDER BY p.name LIMIT 2000', args).fetchall()
    value = get_db().execute('SELECT COALESCE(SUM(stock*avg_cost),0) FROM pv WHERE active=1 '
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
            mode = request.form.get('mode')
            kind = 'correction' if mode == 'add' else request.form.get('kind')
            mode = mode or ('add' if kind == 'correction' else 'remove')
            qty = parse_qty(request.form.get('qty'))  # always positive; the tab decides add or remove
            if kind == 'correction' and mode == 'remove':
                qty = -qty
            with tx() as db:
                rules.adjust_stock(db, int(request.form['product_id']), kind, qty,
                                   request.form.get('reason', ''), g.user['id'],
                                   request.form.get('batch_id', type=int))
            flash('Stock added.' if qty > 0 else 'Stock removed.', 'ok')
            return redirect(url_for('inventory.adjust', mode=mode))
        except (RuleError, ValueError, KeyError) as e:
            flash(str(e) if isinstance(e, RuleError) else 'Choose a product.', 'error')
    products = db.execute('SELECT id, code, name, unit, stock FROM pv WHERE active=1 '
                          'ORDER BY name').fetchall()
    recent = db.execute("SELECT m.*, p.name, p.unit, u.username FROM stock_moves m JOIN products p "
                        "ON p.id=m.product_id LEFT JOIN users u ON u.id=m.user_id WHERE m.ref_type="
                        "'adjustment' AND m.branch_id=? ORDER BY m.id DESC LIMIT 30", (branch_id(),)).fetchall()
    mode = request.values.get('mode') if request.values.get('mode') in ('add', 'remove') else 'remove'
    return render_template('inventory/adjust.html', products=products, recent=recent, mode=mode,
                           sel=request.args.get('pid', type=int), labels=rules.MOVE_LABELS)


@bp.route('/movements')
@requires('inventory')
def movements():
    start, end = date_range()
    mtype = request.args.get('type', '')
    pid = request.args.get('pid', type=int)
    sql = ('SELECT m.*, p.name, p.code, p.unit, u.username FROM stock_moves m JOIN products p '
           'ON p.id=m.product_id LEFT JOIN users u ON u.id=m.user_id WHERE m.branch_id=? AND date(m.at) BETWEEN ? AND ?')
    args = [branch_id(), start, end]
    if mtype:
        sql += ' AND m.move_type=?'
        args.append(mtype)
    if pid:
        sql += ' AND m.product_id=?'
        args.append(pid)
    rows = get_db().execute(sql + ' ORDER BY m.id DESC LIMIT 2000', args).fetchall()
    return render_template('inventory/movements.html', rows=rows, start=start, end=end, mtype=mtype,
                           labels=rules.MOVE_LABELS)


# ---------------------------------------------------------------- FIFO batches
@bp.route('/api/batches')
@requires('inventory', 'adjust')
def api_batches():
    """Open batches of one product in this branch, in the order they will be used (FIFO)."""
    pid = request.args.get('pid', type=int)
    rows = rules.open_batches(get_db(), pid, branch_id()) if pid else []
    return jsonify([{'id': b['id'], 'ref_no': b['ref_no'], 'received': b['received'], 'expiry': b['expiry'] or '',
                     'remaining': b['remaining']} for b in rows])


@bp.route('/batches')
@requires('inventory')
def batches():
    """Stock batches of this branch: what is in stock, from which delivery, at what cost, expiring when."""
    q = request.args.get('q', '').strip()
    show = request.args.get('show', 'open')
    days = request.args.get('days', type=int) or 30
    sql = ('SELECT b.*, p.code, p.name, p.unit FROM stock_batches b JOIN products p ON p.id=b.product_id '
           'WHERE b.branch_id=?')
    args = [branch_id()]
    if show != 'all':
        sql += ' AND b.remaining>0.0005'
    if show == 'expiring':
        sql += " AND b.expiry IS NOT NULL AND b.expiry<>'' AND b.expiry<=?"
        args.append((date.today() + timedelta(days=days)).isoformat())
    if q:
        sql += ' AND (p.name LIKE ? OR p.code LIKE ? OR b.ref_no LIKE ?)'
        args += [f'%{q}%', f'%{q}%', f'%{q}%']
    sql += (" ORDER BY p.name, CASE WHEN b.expiry IS NULL OR b.expiry='' THEN 1 ELSE 0 END, b.expiry, "
            "b.received, b.id LIMIT 3000")
    rows = get_db().execute(sql, args).fetchall()
    return render_template('inventory/batches.html', rows=rows, q=q, show=show, days=days, today=today())


@bp.route('/batches/<int:bid>', methods=['GET', 'POST'])
@requires('inventory')
def batch(bid):
    db = get_db()
    b = db.execute('SELECT b.*, p.code, p.name, p.unit FROM stock_batches b JOIN products p ON p.id=b.product_id '
                   'WHERE b.id=? AND b.branch_id=?', (bid, branch_id())).fetchone()
    if not b:
        return fail('Batch not found in this branch.', 'inventory.batches')
    if request.method == 'POST':
        if not can('adjust'):
            return fail('Your role cannot change batches.', 'inventory.batch', bid=bid)
        try:
            with tx() as db:
                rules.set_batch_expiry(db, bid, request.form.get('expiry', '').strip())
            flash('Expiry date saved.', 'ok')
        except RuleError as e:
            flash(str(e), 'error')
        return redirect(url_for('inventory.batch', bid=bid))
    moves = db.execute('SELECT bm.*, m.at, m.move_type, m.ref_no, m.ref_type, m.ref_id, m.reason, u.username '
                       'FROM batch_moves bm JOIN stock_moves m ON m.id=bm.move_id LEFT JOIN users u ON u.id=m.user_id '
                       'WHERE bm.batch_id=? ORDER BY bm.id', (bid,)).fetchall()
    return render_template('inventory/batch.html', b=b, moves=moves, labels=rules.MOVE_LABELS)


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
                      'WHERE s.branch_id=? ORDER BY s.id DESC', (branch_id(),)).fetchall()
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
                      f"JOIN ({products_select(db, '?')}) p ON p.id=i.product_id WHERE stocktake_id=? ORDER BY p.name",
                      (st['branch_id'], st_id)).fetchall()
    return render_template('inventory/stocktake.html', st=st, rows=rows)


# ---------------------------------------------------------------- transfers between branches
@bp.route('/transfers')
@requires('adjust')
def transfers():
    rows = get_db().execute(
        'SELECT t.*, f.name AS from_name, d.name AS to_name, u.username, '
        '(SELECT COUNT(*) FROM transfer_items i WHERE i.transfer_id=t.id) AS n, '
        '(SELECT COALESCE(SUM(i.qty*i.unit_cost),0) FROM transfer_items i WHERE i.transfer_id=t.id) AS value '
        'FROM transfers t JOIN branches f ON f.id=t.from_branch JOIN branches d ON d.id=t.to_branch '
        'LEFT JOIN users u ON u.id=t.user_id WHERE ? IN (t.from_branch, t.to_branch) ORDER BY t.id DESC LIMIT 500',
        (branch_id(),)).fetchall()
    return render_template('inventory/transfers.html', rows=rows)


@bp.route('/transfers/new', methods=['GET', 'POST'])
@requires('adjust')
def transfer_new():
    db = get_db()
    if request.method == 'POST':
        try:
            lines = [(int(p), parse_qty(q)) for p, q in zip(request.form.getlist('pid'), request.form.getlist('qty'))
                     if p]
            with tx() as db:
                claim_once(db)
                tid = rules.send_transfer(db, request.form.get('to_branch', type=int), lines,
                                          request.form.get('notes', '').strip(), g.user['id'], today())
            flash('Transfer sent. The stock is now in transit until the other branch receives it.', 'ok')
            return redirect(url_for('inventory.transfer', tid=tid))
        except RuleError as e:
            flash(str(e), 'error')
    dests = db.execute('SELECT * FROM branches WHERE active=1 AND id<>? ORDER BY id', (branch_id(),)).fetchall()
    products = db.execute('SELECT id, code, name, unit, stock FROM pv WHERE active=1 AND stock>0 '
                          'ORDER BY name').fetchall()
    return render_template('inventory/transfer_new.html', dests=dests, products=products)


@bp.route('/transfers/<int:tid>', methods=['GET', 'POST'])
@requires('adjust')
def transfer(tid):
    db = get_db()
    t = db.execute('SELECT t.*, f.name AS from_name, d.name AS to_name, u.username, r.username AS receiver '
                   'FROM transfers t JOIN branches f ON f.id=t.from_branch JOIN branches d ON d.id=t.to_branch '
                   'LEFT JOIN users u ON u.id=t.user_id LEFT JOIN users r ON r.id=t.received_by WHERE t.id=?',
                   (tid,)).fetchone()
    if not t or branch_id() not in (t['from_branch'], t['to_branch']):
        return fail('Transfer not found.', 'inventory.transfers')
    if request.method == 'POST':
        try:
            with tx() as db:
                if request.form.get('receive') and branch_id() == t['to_branch']:
                    got = {int(k[2:]): parse_qty(v or '0', 'Received quantity', allow_zero=True)
                           for k, v in request.form.items() if k.startswith('r_')}
                    rules.receive_transfer(db, tid, got, g.user['id'])
                    msg = 'Transfer received. The stock is now in this branch.'
                elif request.form.get('cancel') and branch_id() == t['from_branch']:
                    rules.cancel_transfer(db, tid, request.form.get('reason', ''), g.user['id'])
                    msg = 'Transfer cancelled. The stock is back in this branch.'
                else:
                    raise RuleError('Switch to the right branch first.')
            flash(msg, 'ok')
            return redirect(url_for('inventory.transfer', tid=tid))
        except RuleError as e:
            flash(str(e), 'error')
    items = db.execute('SELECT i.*, p.name, p.code, p.unit FROM transfer_items i JOIN products p '
                       'ON p.id=i.product_id WHERE transfer_id=? ORDER BY i.id', (tid,)).fetchall()
    return render_template('inventory/transfer.html', t=t, items=items)
