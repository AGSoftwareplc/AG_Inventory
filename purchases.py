"""Purchases: order -> confirm -> receive into stock -> pay -> supplier returns."""
from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from core import (RuleError, branch_id, claim_once, date_range, fail, get_db, parse_money, parse_qty,
                  payment_methods, requires, today, tx)
import rules

bp = Blueprint('purchases', __name__, url_prefix='/purchases')


@bp.route('/')
@requires('purchases', 'receive')
def index():
    start, end = date_range()
    status = request.args.get('status', '')
    sup = request.args.get('supplier', type=int)
    sql = ('SELECT p.*, s.name AS supplier FROM purchases p JOIN suppliers s ON s.id=p.supplier_id '
           'WHERE p.branch_id=? AND p.date BETWEEN ? AND ?')
    args = [branch_id(), start, end]
    if status == 'unpaid':
        sql += " AND p.status NOT IN ('Draft','Cancelled') AND p.total-p.returned_value-p.paid>0"
    elif status:
        sql += ' AND p.status=?'
        args.append(status)
    if sup:
        sql += ' AND p.supplier_id=?'
        args.append(sup)
    rows = get_db().execute(sql + ' ORDER BY p.id DESC', args).fetchall()
    sups = get_db().execute('SELECT id, name FROM suppliers ORDER BY name').fetchall()
    return render_template('purchases/index.html', rows=rows, start=start, end=end, status=status,
                           sup=sup, sups=sups, pay_status=rules.payment_status)


def _lines(form):
    pids, qtys, costs = form.getlist('pid'), form.getlist('qty'), form.getlist('cost')
    lines = []
    for pid, q, c in zip(pids, qtys, costs):
        if not pid:
            continue
        lines.append((int(pid), parse_qty(q), parse_money(c, 'Unit cost')))
    return lines


@bp.route('/new', methods=['GET', 'POST'])
@bp.route('/<int:pur_id>/edit', methods=['GET', 'POST'])
@requires('purchases')
def edit(pur_id=None):
    db = get_db()
    pur = db.execute('SELECT * FROM purchases WHERE id=?', (pur_id,)).fetchone() if pur_id else None
    if pur_id and (not pur or pur['status'] != 'Draft'):
        return fail('Only draft purchases can be edited.', 'purchases.index')
    if request.method == 'POST':
        try:
            lines = _lines(request.form)
            with tx() as db:
                claim_once(db)
                pur_id = rules.save_purchase(
                    db, pur_id, request.form.get('supplier_id', type=int), request.form.get('date') or today(),
                    parse_money(request.form.get('discount'), 'Discount'),
                    request.form.get('notes', '').strip(), lines, g.user['id'])
                if request.form.get('confirm'):
                    rules.confirm_purchase(db, pur_id, g.user['id'])
            flash('Purchase saved.', 'ok')
            return redirect(url_for('purchases.view', pur_id=pur_id))
        except RuleError as e:
            flash(str(e), 'error')
    items = db.execute('SELECT * FROM purchase_items WHERE purchase_id=?', (pur_id,)).fetchall() if pur else []
    products = db.execute('SELECT id, code, name, unit, avg_cost FROM pv WHERE active=1 '
                          'ORDER BY name').fetchall()
    sups = db.execute('SELECT id, name FROM suppliers WHERE active=1 ORDER BY name').fetchall()
    return render_template('purchases/edit.html', pur=pur, items=items, products=products, sups=sups,
                           today=today())


@bp.route('/<int:pur_id>')
@requires('purchases', 'receive')
def view(pur_id):
    db = get_db()
    pur = db.execute('SELECT p.*, s.name AS supplier, s.phone, u.username FROM purchases p JOIN '
                     'suppliers s ON s.id=p.supplier_id LEFT JOIN users u ON u.id=p.user_id '
                     'WHERE p.id=?', (pur_id,)).fetchone()
    if not pur:
        return fail('Purchase not found.', 'purchases.index')
    gross = rules.purchase_gross(db, pur_id)
    items = [dict(i, net_cost=rules.net_unit_cost(i, pur, gross)) for i in db.execute(
        'SELECT i.*, p.name, p.code, p.unit FROM purchase_items i JOIN products p ON '
        'p.id=i.product_id WHERE purchase_id=?', (pur_id,)).fetchall()]
    pays = db.execute('SELECT pp.*, u.username FROM purchase_payments pp LEFT JOIN users u ON '
                      'u.id=pp.user_id WHERE purchase_id=? ORDER BY pp.id', (pur_id,)).fetchall()
    rets = db.execute("SELECT r.*, p.name, u.username FROM returns r JOIN products p ON p.id=r.product_id "
                      "LEFT JOIN users u ON u.id=r.user_id WHERE kind='purchase' AND doc_id=? ORDER BY r.id",
                      (pur_id,)).fetchall()
    batches = db.execute('SELECT b.*, p.name, p.unit FROM stock_batches b JOIN products p ON p.id=b.product_id '
                         'JOIN purchase_items i ON i.id=b.purchase_item_id WHERE i.purchase_id=? ORDER BY b.id',
                         (pur_id,)).fetchall()
    return render_template('purchases/view.html', pur=pur, items=items, pays=pays, rets=rets, batches=batches,
                           gross=gross, balance=rules.purchase_balance(pur), methods=payment_methods(),
                           pay_status=rules.payment_status(pur['total'] - pur['returned_value'], pur['paid']),
                           today=today())


def _act(pur_id, fn, ok_msg):
    try:
        with tx() as db:
            claim_once(db)
            fn(db)
        flash(ok_msg, 'ok')
    except RuleError as e:
        flash(str(e), 'error')
    return redirect(url_for('purchases.view', pur_id=pur_id))


@bp.route('/<int:pur_id>/confirm', methods=['POST'])
@requires('purchases')
def confirm(pur_id):
    return _act(pur_id, lambda db: rules.confirm_purchase(db, pur_id, g.user['id']), 'Purchase confirmed.')


@bp.route('/<int:pur_id>/cancel', methods=['POST'])
@requires('purchases')
def cancel(pur_id):
    return _act(pur_id, lambda db: rules.cancel_purchase(db, pur_id, request.form.get('reason'),
                                                          g.user['id']), 'Purchase cancelled.')


@bp.route('/<int:pur_id>/receive', methods=['POST'])
@requires('receive')
def receive(pur_id):
    try:
        got = {int(k[2:]): parse_qty(v, allow_zero=True)
               for k, v in request.form.items() if k.startswith('r_') and v.strip()}
        exp = {int(k[2:]): v.strip() for k, v in request.form.items() if k.startswith('x_') and v.strip()}
    except RuleError as e:
        return fail(e, 'purchases.view', pur_id=pur_id)
    return _act(pur_id, lambda db: rules.receive_purchase(db, pur_id, got, g.user['id'], exp),
                'Received into stock. Each line is now a stock batch (FIFO).')


@bp.route('/<int:pur_id>/return', methods=['POST'])
@requires('receive')
def ret(pur_id):
    try:
        qty = parse_qty(request.form.get('qty'))
        item_id = int(request.form['item_id'])
    except (RuleError, ValueError, KeyError) as e:
        return fail(e if isinstance(e, RuleError) else 'Choose a line.', 'purchases.view', pur_id=pur_id)
    return _act(pur_id, lambda db: rules.purchase_return(db, pur_id, item_id, qty, request.form.get('reason'),
                                                          g.user['id'], today()),
                'Returned to supplier. Stock and amount owed were reduced.')


@bp.route('/<int:pur_id>/pay', methods=['POST'])
@requires('payments', 'purchases')
def pay(pur_id):
    try:
        amount = parse_money(request.form.get('amount'), allow_zero=False)
    except RuleError as e:
        return fail(e, 'purchases.view', pur_id=pur_id)
    return _act(pur_id, lambda db: rules.pay_purchase(db, pur_id, amount, request.form.get('method', 'Cash'),
                                                       request.form.get('date') or today(), g.user['id'],
                                                       request.form.get('reference', '')),
                'Payment recorded.')
