"""Point of sale, manual sales, sales history, receipts, customer payments, returns and cancellations."""
import json

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
                           allow_discount=can('discount'), allow_credit=can('credit'), allow_price=can('price_change'))


@bp.route('/api/products')
@requires('pos')
def api_products():
    """Search for the POS: exact barcode/code first, then name matches."""
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify([])
    db = get_db()
    cols = 'id, code, barcode, name, unit, price, stock, serial_tracked'
    # a scanned serial number finds its product, with that serial already filled in
    sn = db.execute("SELECT s.serial, s.product_id FROM serials s WHERE s.serial=? AND s.status='In stock' "
                    'AND s.branch_id=?', (q, branch_id())).fetchone()
    if sn:
        rows = db.execute(f'SELECT {cols} FROM pv WHERE active=1 AND id=?', (sn['product_id'],)).fetchall()
        return jsonify({'exact': True, 'items': [dict(r, serial=sn['serial']) for r in rows]})
    rows = db.execute(f'SELECT {cols} FROM pv WHERE active=1 AND (barcode=? OR code=?)', (q, q)).fetchall()
    exact = bool(rows)
    if not rows:
        rows = db.execute(f'SELECT {cols} FROM pv WHERE active=1 AND (name LIKE ? OR code LIKE ?) ORDER BY name '
                          'LIMIT 15', (f'%{q}%', f'{q}%')).fetchall()
    return jsonify({'exact': exact, 'items': [dict(r) for r in rows]})


@bp.route('/api/serials')
@requires('pos', 'manual_sales')
def api_serials():
    """Serial numbers of one product still in stock in this branch (to pick from at the POS)."""
    pid = request.args.get('pid', type=int)
    return jsonify([r['serial'] for r in rules.serials_in_stock(get_db(), pid)] if pid else [])


def _cart(text):
    """Cart lines from the form: JSON [{id, qty, price, serials}] (POS) or 'pid:qty,pid:qty' (older form).
    price is in cents; None means the default price."""
    text = (text or '').strip()
    lines = []
    if text.startswith('['):
        for l in json.loads(text):
            price = l.get('price')
            lines.append((int(l['id']), parse_qty(str(l.get('qty'))),
                          None if price in (None, '') else int(price), l.get('serials') or []))
        return lines
    for part in text.split(','):
        if part.strip():
            pid, q = part.split(':')[:2]
            lines.append((int(pid), parse_qty(q)))
    return lines


def _discount(f, lines):
    disc_val = f.get('discount', '').strip()
    if not disc_val:
        return 0
    if f.get('discount_type') == 'pct':
        pct = parse_qty(disc_val, 'Discount %', allow_zero=True)
        if pct > 100:
            raise RuleError('Discount cannot be more than 100%.')
        # % is applied to the server-side subtotal (default prices unless a price was changed)
        db = get_db()
        sub = sum(rnd(l[1] * (l[2] if len(l) > 2 and l[2] is not None else
                              db.execute('SELECT price FROM products WHERE id=?', (l[0],)).fetchone()['price']))
                  for l in lines)
        return rnd(sub * pct / 100)
    return parse_money(disc_val, 'Discount')


@bp.route('/pos', methods=['POST'])
@requires('pos')
def checkout():
    f = request.form
    try:
        lines = _cart(f.get('items'))
        discount = _discount(f, lines)
        tendered = parse_money(f.get('tendered'), 'Amount paid')
        with tx() as db:
            claim_once(db)
            sid = rules.create_sale(db, lines, discount, tendered, f.get('method') or 'Cash',
                                    f.get('customer_id', type=int), g.user['id'], today(),
                                    allow_discount=can('discount'), allow_credit=can('credit'),
                                    reference=f.get('reference', ''), allow_price=can('price_change'))
        return redirect(url_for('sales.receipt', sid=sid, new=1))
    except (RuleError, ValueError, TypeError, KeyError) as e:
        flash(str(e) if isinstance(e, RuleError) else 'The cart could not be read. Please try again.',
              'error')
        return redirect(url_for('sales.pos'))


@bp.route('/manual', methods=['GET', 'POST'])
@requires('manual_sales')
def manual():
    """Record a sale that was made outside the app (e.g. while the PC was off), with its real date and seller."""
    db = get_db()
    f = request.form
    if request.method == 'POST':
        try:
            lines = []
            for pid, q, pr, sn in zip(f.getlist('pid'), f.getlist('qty'), f.getlist('price'), f.getlist('serials')):
                if pid:
                    lines.append((int(pid), parse_qty(q), parse_money(pr, 'Selling price') if pr.strip() else None, sn))
            if f.get('stock') not in ('deduct', 'already'):
                raise RuleError('Say whether the stock must be deducted now or was already deducted.')
            discount = parse_money(f.get('discount'), 'Discount')
            with tx() as db:
                claim_once(db)
                sid = rules.create_sale(db, lines, discount, parse_money(f.get('tendered'), 'Amount paid'),
                                        f.get('method') or 'Cash', f.get('customer_id', type=int), g.user['id'],
                                        f.get('date') or today(), allow_discount=True, allow_credit=can('credit'),
                                        reference=f.get('reference', ''), allow_price=True, manual=True,
                                        seller_id=f.get('seller_id', type=int), deduct=f.get('stock') == 'deduct',
                                        notes=f.get('notes', ''))
            flash('Manual sale recorded.' + (' Stock was deducted (FIFO).' if f.get('stock') == 'deduct'
                                             else ' Stock was not changed (it was already deducted).'), 'ok')
            return redirect(url_for('sales.view', sid=sid))
        except (RuleError, ValueError) as e:
            flash(str(e) if isinstance(e, RuleError) else 'Check the lines.', 'error')
    products = db.execute('SELECT id, code, name, unit, price, stock, serial_tracked FROM pv WHERE active=1 '
                          'ORDER BY name').fetchall()
    sellers = db.execute('SELECT id, full_name, username FROM users WHERE active=1 ORDER BY full_name').fetchall()
    customers = db.execute('SELECT id, name, phone FROM customers ORDER BY name').fetchall()
    return render_template('sales/manual.html', products=products, sellers=sellers, customers=customers,
                           methods=payment_methods(), today=today(), f=f)


def _sale(sid):
    db = get_db()
    s = db.execute('SELECT s.*, c.name AS customer, c.phone AS cphone, u.full_name AS cashier, e.full_name AS recorder, '
                   "(SELECT group_concat(reference, ', ') FROM sale_payments sp WHERE sp.sale_id=s.id AND reference<>'') AS refs "
                   'FROM sales s LEFT JOIN customers c ON c.id=s.customer_id LEFT JOIN users u ON u.id=s.user_id '
                   'LEFT JOIN users e ON e.id=s.entered_by WHERE s.id=?', (sid,)).fetchone()
    items = db.execute("SELECT i.*, p.name, p.code, p.unit, p.serial_tracked, pu.username AS price_user, (SELECT "
                       "group_concat(serial, ', ') FROM serials sr WHERE sr.sale_item_id=i.id) AS serials FROM sale_items i "
                       'JOIN products p ON p.id=i.product_id LEFT JOIN users pu ON pu.id=i.price_by WHERE sale_id=? '
                       'ORDER BY i.id', (sid,)).fetchall()
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
    source = request.args.get('source', '')
    sql = ("SELECT s.*, c.name AS customer, u.username, (SELECT group_concat(reference, ', ') FROM sale_payments sp "
           "WHERE sp.sale_id=s.id AND reference<>'') AS refs FROM sales s LEFT JOIN customers c ON "
           'c.id=s.customer_id LEFT JOIN users u ON u.id=s.user_id WHERE s.branch_id=? AND s.date BETWEEN ? AND ?')
    args = [branch_id(), start, end]
    if source in ('pos', 'manual'):
        sql += ' AND s.manual=?'
        args.append(1 if source == 'manual' else 0)
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
    return render_template('sales/index.html', rows=rows, start=start, end=end, status=status, q=q, source=source,
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
                                                  g.user['id'], today(), request.form.get('serials')),
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
