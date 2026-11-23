"""Point of sale, sales history, receipts, customer payments, returns and cancellations."""
from flask import Blueprint, flash, g, jsonify, redirect, render_template, request, url_for

from core import (RuleError, branch_id, can, claim_once, date_range, fail, get_db, parse_money, parse_qty,
                  payment_methods, requires, rnd, today, tx)
import rules

bp = Blueprint('sales', __name__, url_prefix='/sales')


@bp.route('/pos')
@requires('pos')
def pos():
    customers = get_db().execute('SELECT id, name, phone FROM customers ORDER BY name').fetchall()
    return render_template('sales/pos.html', customers=customers, methods=payment_methods(),
                           allow_discount=can('discount'), allow_credit=can('credit'))


@bp.route('/api/products')
@requires('pos')
def api_products():
    """Search for the POS: exact barcode/code first, then name matches."""
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify([])
    db = get_db()
    rows = db.execute('SELECT id, code, barcode, name, unit, price, stock FROM pv WHERE active=1 '
                      'AND (barcode=? OR code=?)', (q, q)).fetchall()
    exact = bool(rows)
    if not rows:
        rows = db.execute('SELECT id, code, barcode, name, unit, price, stock FROM pv WHERE '
                          'active=1 AND (name LIKE ? OR code LIKE ?) ORDER BY name LIMIT 15',
                          (f'%{q}%', f'{q}%')).fetchall()
    return jsonify({'exact': exact, 'items': [dict(r) for r in rows]})


@bp.route('/pos', methods=['POST'])
@requires('pos')
def checkout():
    f = request.form
    try:
        lines = []
        for part in (f.get('items') or '').split(','):
            if part.strip():
                pid, q = part.split(':')
                lines.append((int(pid), parse_qty(q)))
        disc_val = f.get('discount', '').strip()
        discount = 0
        if disc_val:
            if f.get('discount_type') == 'pct':
                pct = parse_qty(disc_val, 'Discount %', allow_zero=True)
                if pct > 100:
                    raise RuleError('Discount cannot be more than 100%.')
                # % is applied to the server-side subtotal
                sub = sum(rnd(q * get_db().execute('SELECT price FROM products WHERE id=?', (pid,))
                              .fetchone()['price']) for pid, q in lines)
                discount = rnd(sub * pct / 100)
            else:
                discount = parse_money(disc_val, 'Discount')
        tendered = parse_money(f.get('tendered'), 'Amount paid')
        with tx() as db:
            claim_once(db)
            sid = rules.create_sale(db, lines, discount, tendered, f.get('method') or 'Cash',
                                    f.get('customer_id', type=int), g.user['id'], today(),
                                    allow_discount=can('discount'), allow_credit=can('credit'),
                                    reference=f.get('reference', ''))
        return redirect(url_for('sales.receipt', sid=sid, new=1))
    except (RuleError, ValueError, TypeError) as e:
        flash(str(e) if isinstance(e, RuleError) else 'The cart could not be read. Please try again.',
              'error')
        return redirect(url_for('sales.pos'))


def _sale(sid):
    db = get_db()
    s = db.execute('SELECT s.*, c.name AS customer, c.phone AS cphone, u.full_name AS cashier, (SELECT '
                   "group_concat(reference, ', ') FROM sale_payments sp WHERE sp.sale_id=s.id AND reference<>'') AS refs FROM "
                   'sales s LEFT JOIN customers c ON c.id=s.customer_id LEFT JOIN users u ON '
                   'u.id=s.user_id WHERE s.id=?', (sid,)).fetchone()
    items = db.execute('SELECT i.*, p.name, p.code, p.unit FROM sale_items i JOIN products p ON '
                       'p.id=i.product_id WHERE sale_id=? ORDER BY i.id', (sid,)).fetchall()
    return s, items


@bp.route('/<int:sid>/receipt')
@requires('pos', 'sales_view')
def receipt(sid):
    s, items = _sale(sid)
    if not s:
        return fail('Sale not found.', 'sales.index')
    return render_template('sales/receipt.html', s=s, items=items, new=request.args.get('new'))


@bp.route('/')
@requires('sales_view')
def index():
    start, end = date_range()
    status = request.args.get('status', '')
    q = request.args.get('q', '').strip()
    sql = ('SELECT s.*, c.name AS customer, u.username FROM sales s LEFT JOIN customers c ON '
           'c.id=s.customer_id LEFT JOIN users u ON u.id=s.user_id WHERE s.branch_id=? AND s.date BETWEEN ? AND ?')
    args = [branch_id(), start, end]
    if status == 'credit':
        sql += " AND s.status='Completed' AND s.total-s.refunded-s.paid>0"
    elif status == 'paid':
        sql += " AND s.status='Completed' AND s.total-s.refunded-s.paid<=0"
    elif status == 'partial':
        sql += " AND s.status='Completed' AND s.total-s.refunded-s.paid>0 AND s.paid>0"
    elif status == 'unpaid':
        sql += " AND s.status='Completed' AND s.total-s.refunded-s.paid>0 AND s.paid<=0"
    elif status:
        sql += ' AND s.status=?'
        args.append(status)
    if q:
        sql += (' AND (s.receipt_no LIKE ? OR c.name LIKE ? OR c.phone LIKE ? OR EXISTS (SELECT 1 FROM '
                'sale_payments sp WHERE sp.sale_id=s.id AND sp.reference LIKE ?))')
        args += [f'%{q}%'] * 4
    rows = get_db().execute(sql + ' ORDER BY s.id DESC LIMIT 2000', args).fetchall()
    done = [r for r in rows if r['status'] == 'Completed']
    totals = {'n': len(done), 'total': sum(r['total'] - r['refunded'] for r in done)}
    return render_template('sales/index.html', rows=rows, start=start, end=end, status=status, q=q,
                           totals=totals, pay_status=rules.payment_status)


@bp.route('/<int:sid>')
@requires('sales_view')
def view(sid):
    s, items = _sale(sid)
    if not s:
        return fail('Sale not found.', 'sales.index')
    db = get_db()
    pays = db.execute('SELECT sp.*, u.username FROM sale_payments sp LEFT JOIN users u ON u.id=sp.user_id '
                      'WHERE sale_id=? ORDER BY sp.id', (sid,)).fetchall()
    rets = db.execute("SELECT r.*, p.name, u.username FROM returns r JOIN products p ON p.id=r.product_id "
                      "LEFT JOIN users u ON u.id=r.user_id WHERE kind='sale' AND doc_id=? ORDER BY r.id",
                      (sid,)).fetchall()
    batches = db.execute("SELECT m.product_id, bm.batch_id, -bm.qty AS qty, bm.unit_cost, bm.back_qty, b.received, "
                         "b.expiry, b.ref_no FROM stock_moves m JOIN batch_moves bm ON bm.move_id=m.id JOIN stock_batches b "
                         "ON b.id=bm.batch_id WHERE m.ref_type='sale' AND m.ref_id=? AND m.move_type='sale' ORDER BY bm.id",
                         (sid,)).fetchall()
    return render_template('sales/view.html', s=s, items=items, pays=pays, rets=rets, batches=batches,
                           balance=rules.sale_balance(s), methods=payment_methods(), today=today(),
                           pay_status=rules.payment_status(s['total'] - s['refunded'], s['paid']))


def _act(sid, fn, ok):
    try:
        with tx() as db:
            claim_once(db)
            res = fn(db)
        flash(ok(res) if callable(ok) else ok, 'ok')
    except RuleError as e:
        flash(str(e), 'error')
    return redirect(url_for('sales.view', sid=sid))


@bp.route('/<int:sid>/return', methods=['POST'])
@requires('returns')
def ret(sid):
    from core import fmt_money
    try:
        qty = parse_qty(request.form.get('qty'))
        item_id = int(request.form['item_id'])
    except (RuleError, ValueError, KeyError) as e:
        return fail(e if isinstance(e, RuleError) else 'Choose a line.', 'sales.view', sid=sid)
    return _act(sid, lambda db: rules.sale_return(db, sid, item_id, qty, request.form.get('reason'),
                                                  g.user['id'], today()),
                lambda cash: 'Return recorded and items put back in stock.' +
                (f' Give the customer {fmt_money(cash)} back.' if cash > 0 else ''))


@bp.route('/<int:sid>/cancel', methods=['POST'])
@requires('sales_cancel')
def cancel(sid):
    return _act(sid, lambda db: rules.cancel_sale(db, sid, request.form.get('reason'), g.user['id']),
                'Sale cancelled and stock restored.')


@bp.route('/<int:sid>/pay', methods=['POST'])
@requires('payments')
def pay(sid):
    try:
        amount = parse_money(request.form.get('amount'), allow_zero=False)
    except RuleError as e:
        return fail(e, 'sales.view', sid=sid)
    return _act(sid, lambda db: rules.pay_sale(db, sid, amount, request.form.get('method', 'Cash'),
                                               request.form.get('date') or today(), g.user['id'],
                                               request.form.get('reference', '')), 'Payment recorded.')
