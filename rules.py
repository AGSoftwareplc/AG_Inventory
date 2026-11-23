"""Business rules: every change to stock, cost and money goes through this file.

All functions expect to run inside core.tx() so each operation is all-or-nothing.
Money is in integer cents; quantities are floats rounded to 3 decimals.
"""
from core import RuleError, audit, next_no, rnd, setting, fmt_qty

EPS = 1e-9
OUT_TYPES = {'damaged': 'Damaged', 'expired': 'Expired', 'lost': 'Lost'}
MOVE_LABELS = {
    'purchase': 'Purchase receiving', 'sale': 'Sale', 'damaged': 'Damaged', 'expired': 'Expired',
    'lost': 'Lost', 'correction': 'Stock correction', 'sale_return': 'Customer return',
    'purchase_return': 'Supplier return', 'sale_cancel': 'Sale cancellation',
    'stocktake': 'Stocktake correction', 'opening': 'Opening stock',
}


def _q(x):
    return round(x, 3)


def product(db, pid):
    p = db.execute('SELECT * FROM products WHERE id=?', (pid,)).fetchone()
    if not p:
        raise RuleError('Product not found.')
    return p


# ---------------------------------------------------------------- core stock movement
def move_stock(db, pid, qty, move_type, uid, ref_type='', ref_id=None, ref_no='', reason='',
               in_cost=None):
    """Change stock of one product and record the movement.

    qty > 0 with in_cost: stock comes in at in_cost -> weighted average cost is recalculated.
    qty < 0 with in_cost: stock goes back to supplier at in_cost -> average recalculated on the rest.
    Otherwise stock moves at the current average cost and the average does not change.
    """
    p = product(db, pid)
    qty = _q(qty)
    old_stock, avg = p['stock'], p['avg_cost']
    new_stock = _q(old_stock + qty)
    if qty < 0 and new_stock < -EPS and setting('allow_negative') != '1':
        raise RuleError(f"Not enough stock for {p['name']}: available {fmt_qty(old_stock)} "
                        f"{p['unit']}, needed {fmt_qty(-qty)}.")
    if in_cost is not None and qty > 0:
        if old_stock <= EPS:
            avg = in_cost
        else:
            avg = (old_stock * avg + qty * in_cost) / new_stock
    elif in_cost is not None and qty < 0:
        if new_stock > EPS:
            avg = max(0.0, (old_stock * avg + qty * in_cost) / new_stock)
    unit_cost = in_cost if in_cost is not None else p['avg_cost']
    db.execute('UPDATE products SET stock=?, avg_cost=? WHERE id=?', (new_stock, avg, pid))
    db.execute('INSERT INTO stock_moves(product_id, qty, move_type, ref_type, ref_id, ref_no, '
               'unit_cost, balance, reason, user_id) VALUES (?,?,?,?,?,?,?,?,?,?)',
               (pid, qty, move_type, ref_type, ref_id, ref_no, unit_cost, new_stock, reason, uid))
    return new_stock


def adjust_stock(db, pid, kind, qty, reason, uid):
    """Manual adjustment. kind: damaged/expired/lost (remove) or correction (signed qty)."""
    if not (reason or '').strip():
        raise RuleError('A reason is required for every stock adjustment.')
    if kind in OUT_TYPES:
        if qty <= 0:
            raise RuleError('Quantity must be greater than zero.')
        qty = -qty
    elif kind != 'correction':
        raise RuleError('Unknown adjustment type.')
    if abs(qty) < EPS:
        raise RuleError('Quantity cannot be zero.')
    move_stock(db, pid, qty, kind, uid, ref_type='adjustment', reason=reason.strip())
    audit('stock_adjust', f'product {pid} {kind} {qty}: {reason}', db)


# ---------------------------------------------------------------- purchases
def purchase_gross(db, pur_id):
    r = db.execute('SELECT COALESCE(SUM(qty*unit_cost),0) AS g FROM purchase_items '
                   'WHERE purchase_id=?', (pur_id,)).fetchone()
    return r['g']


def net_unit_cost(item, purchase, gross):
    """Unit cost after the purchase discount, spread in proportion to line value."""
    if gross <= 0:
        return float(item['unit_cost'])
    return item['unit_cost'] * (purchase['total'] / gross)


def save_purchase(db, pur_id, supplier_id, date, discount, notes, lines, uid):
    """Create or update a Draft purchase. lines = [(product_id, qty, unit_cost_cents)]."""
    if not lines:
        raise RuleError('Add at least one product line.')
    if not db.execute('SELECT 1 FROM suppliers WHERE id=?', (supplier_id,)).fetchone():
        raise RuleError('Choose a supplier.')
    gross = sum(rnd(q * c) for _, q, c in lines)
    if discount > gross:
        raise RuleError('Discount cannot be larger than the purchase value.')
    total = gross - discount
    if pur_id:
        pur = db.execute('SELECT * FROM purchases WHERE id=?', (pur_id,)).fetchone()
        if not pur or pur['status'] != 'Draft':
            raise RuleError('Only draft purchases can be edited.')
        db.execute('UPDATE purchases SET supplier_id=?, date=?, discount=?, total=?, notes=? '
                   'WHERE id=?', (supplier_id, date, discount, total, notes, pur_id))
        db.execute('DELETE FROM purchase_items WHERE purchase_id=?', (pur_id,))
    else:
        ref = next_no(db, 'P', 'purchases', 'ref_no')
        cur = db.execute('INSERT INTO purchases(ref_no, supplier_id, date, discount, total, notes, '
                         'user_id) VALUES (?,?,?,?,?,?,?)',
                         (ref, supplier_id, date, discount, total, notes, uid))
        pur_id = cur.lastrowid
    for pid, q, c in lines:
        product(db, pid)
        db.execute('INSERT INTO purchase_items(purchase_id, product_id, qty, unit_cost) '
                   'VALUES (?,?,?,?)', (pur_id, pid, q, c))
    return pur_id


def _purchase(db, pur_id):
    pur = db.execute('SELECT * FROM purchases WHERE id=?', (pur_id,)).fetchone()
    if not pur:
        raise RuleError('Purchase not found.')
    return pur


def confirm_purchase(db, pur_id, uid):
    pur = _purchase(db, pur_id)
    if pur['status'] != 'Draft':
        raise RuleError('Only draft purchases can be confirmed.')
    db.execute("UPDATE purchases SET status='Confirmed' WHERE id=?", (pur_id,))
    audit('purchase_confirm', pur['ref_no'], db)


def cancel_purchase(db, pur_id, reason, uid):
    pur = _purchase(db, pur_id)
    if pur['status'] not in ('Draft', 'Confirmed'):
        raise RuleError('A purchase can only be cancelled before anything is received. '
                        'Use a supplier return instead.')
    if pur['paid'] > 0:
        raise RuleError('This purchase has payments recorded; it cannot be cancelled.')
    if not (reason or '').strip():
        raise RuleError('A reason is required to cancel.')
    db.execute("UPDATE purchases SET status='Cancelled', cancel_reason=? WHERE id=?",
               (reason.strip(), pur_id))
    audit('purchase_cancel', f"{pur['ref_no']}: {reason}", db)


def _refresh_purchase_status(db, pur_id):
    items = db.execute('SELECT qty, received_qty FROM purchase_items WHERE purchase_id=?',
                       (pur_id,)).fetchall()
    got = sum(i['received_qty'] for i in items)
    full = all(i['received_qty'] >= i['qty'] - EPS for i in items)
    status = 'Received' if full else ('Partially Received' if got > EPS else 'Confirmed')
    db.execute('UPDATE purchases SET status=? WHERE id=?', (status, pur_id))


def receive_purchase(db, pur_id, receipts, uid):
    """receipts = {item_id: qty_received_now}. Stock goes up at the net unit cost."""
    pur = _purchase(db, pur_id)
    if pur['status'] not in ('Confirmed', 'Partially Received'):
        raise RuleError('Only confirmed purchases can be received.')
    gross = purchase_gross(db, pur_id)
    done = 0
    for item_id, q in receipts.items():
        if q <= EPS:
            continue
        it = db.execute('SELECT * FROM purchase_items WHERE id=? AND purchase_id=?',
                        (item_id, pur_id)).fetchone()
        if not it:
            raise RuleError('Purchase line not found.')
        left = _q(it['qty'] - it['received_qty'])
        if q > left + EPS:
            p = product(db, it['product_id'])
            raise RuleError(f"{p['name']}: only {fmt_qty(left)} left to receive.")
        db.execute('UPDATE purchase_items SET received_qty=received_qty+? WHERE id=?', (q, item_id))
        move_stock(db, it['product_id'], q, 'purchase', uid, 'purchase', pur_id, pur['ref_no'],
                   in_cost=net_unit_cost(it, pur, gross))
        done += 1
    if not done:
        raise RuleError('Enter a quantity to receive.')
    _refresh_purchase_status(db, pur_id)


def purchase_return(db, pur_id, item_id, qty, reason, uid, date):
    pur = _purchase(db, pur_id)
    it = db.execute('SELECT * FROM purchase_items WHERE id=? AND purchase_id=?',
                    (item_id, pur_id)).fetchone()
    if not it:
        raise RuleError('Purchase line not found.')
    if not (reason or '').strip():
        raise RuleError('A reason is required for a return.')
    can_return = _q(it['received_qty'] - it['returned_qty'])
    if qty > can_return + EPS:
        raise RuleError(f'Only {fmt_qty(can_return)} can be returned on this line.')
    cost = net_unit_cost(it, pur, purchase_gross(db, pur_id))
    credit = rnd(qty * cost)
    move_stock(db, it['product_id'], -qty, 'purchase_return', uid, 'purchase', pur_id,
               pur['ref_no'], reason=reason.strip(), in_cost=cost)
    db.execute('UPDATE purchase_items SET returned_qty=returned_qty+? WHERE id=?', (qty, item_id))
    db.execute('UPDATE purchases SET returned_value=returned_value+? WHERE id=?', (credit, pur_id))
    db.execute('INSERT INTO returns(kind, doc_id, item_id, product_id, qty, amount, cost, reason, '
               'date, user_id) VALUES (?,?,?,?,?,?,?,?,?,?)',
               ('purchase', pur_id, item_id, it['product_id'], qty, credit, cost, reason.strip(),
                date, uid))
    audit('purchase_return', f"{pur['ref_no']} item {item_id} qty {qty}", db)
    return credit


def purchase_balance(pur):
    return pur['total'] - pur['returned_value'] - pur['paid']


def pay_purchase(db, pur_id, amount, method, date, uid):
    pur = _purchase(db, pur_id)
    if pur['status'] in ('Draft', 'Cancelled'):
        raise RuleError('Confirm the purchase before recording payments.')
    if amount <= 0:
        raise RuleError('Amount must be greater than zero.')
    if amount > purchase_balance(pur):
        raise RuleError('Payment is more than the amount owed.')
    db.execute('INSERT INTO purchase_payments(purchase_id, amount, method, date, user_id) '
               'VALUES (?,?,?,?,?)', (pur_id, amount, method, date, uid))
    db.execute('UPDATE purchases SET paid=paid+? WHERE id=?', (amount, pur_id))


def payment_status(total, paid):
    if total <= 0 or paid >= total:
        return 'Paid'
    return 'Partially paid' if paid > 0 else 'Unpaid'


# ---------------------------------------------------------------- sales
def create_sale(db, lines, discount, tendered, method, customer_id, uid, date,
                allow_discount=False):
    """lines = [(product_id, qty)]. Prices and costs are taken from the database, not the browser."""
    if not lines:
        raise RuleError('The cart is empty.')
    merged = {}
    for pid, q in lines:
        if q <= EPS:
            raise RuleError('Quantities must be greater than zero.')
        merged[pid] = _q(merged.get(pid, 0) + q)
    if discount and not allow_discount:
        raise RuleError('You are not allowed to give discounts.')
    if customer_id and not db.execute('SELECT 1 FROM customers WHERE id=?', (customer_id,)).fetchone():
        raise RuleError('Customer not found.')
    items, subtotal = [], 0
    for pid, q in merged.items():
        p = product(db, pid)
        if not p['active']:
            raise RuleError(f"{p['name']} is not active.")
        if q > p['stock'] + EPS and setting('allow_negative') != '1':
            raise RuleError(f"Not enough stock for {p['name']}: available {fmt_qty(p['stock'])} "
                            f"{p['unit']}.")
        subtotal += rnd(q * p['price'])
        items.append((p, q))
    if discount > subtotal:
        raise RuleError('Discount cannot be larger than the subtotal.')
    total = subtotal - discount
    paid = min(tendered, total)
    if paid < total and not customer_id:
        raise RuleError('Amount paid is less than the total. Choose a registered customer to '
                        'sell on credit.')
    change = max(0, tendered - total)
    receipt = next_no(db, 'S', 'sales', 'receipt_no')
    sid = db.execute('INSERT INTO sales(receipt_no, customer_id, date, subtotal, discount, total, '
                     'paid, tendered, change_due, method, user_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                     (receipt, customer_id, date, subtotal, discount, total, paid, tendered,
                      change, method, uid)).lastrowid
    for p, q in items:
        db.execute('INSERT INTO sale_items(sale_id, product_id, qty, price, cost) VALUES (?,?,?,?,?)',
                   (sid, p['id'], q, p['price'], p['avg_cost']))
        move_stock(db, p['id'], -q, 'sale', uid, 'sale', sid, receipt)
    if paid > 0:
        db.execute('INSERT INTO sale_payments(sale_id, amount, method, date, user_id) '
                   'VALUES (?,?,?,?,?)', (sid, paid, method, date, uid))
    return sid


def _sale(db, sid):
    s = db.execute('SELECT * FROM sales WHERE id=?', (sid,)).fetchone()
    if not s:
        raise RuleError('Sale not found.')
    return s


def sale_balance(s):
    """Amount the customer still owes (negative = shop owes a refund)."""
    return s['total'] - s['refunded'] - s['paid']


def sale_return(db, sid, item_id, qty, reason, uid, date):
    """Customer return: stock back at the original cost, refund = value after discount share."""
    s = _sale(db, sid)
    if s['status'] != 'Completed':
        raise RuleError('Returns are only possible on completed sales.')
    if not (reason or '').strip():
        raise RuleError('A reason is required for a return.')
    it = db.execute('SELECT * FROM sale_items WHERE id=? AND sale_id=?', (item_id, sid)).fetchone()
    if not it:
        raise RuleError('Sale line not found.')
    left = _q(it['qty'] - it['returned_qty'])
    if qty > left + EPS:
        raise RuleError(f'Only {fmt_qty(left)} can be returned on this line.')
    factor = s['total'] / s['subtotal'] if s['subtotal'] else 1
    refund = rnd(qty * it['price'] * factor)
    move_stock(db, it['product_id'], qty, 'sale_return', uid, 'sale', sid, s['receipt_no'],
               reason=reason.strip(), in_cost=it['cost'])
    db.execute('UPDATE sale_items SET returned_qty=returned_qty+? WHERE id=?', (qty, item_id))
    db.execute('UPDATE sales SET refunded=refunded+? WHERE id=?', (refund, sid))
    db.execute('INSERT INTO returns(kind, doc_id, item_id, product_id, qty, amount, cost, reason, '
               'date, user_id) VALUES (?,?,?,?,?,?,?,?,?,?)',
               ('sale', sid, item_id, it['product_id'], qty, refund, it['cost'], reason.strip(),
                date, uid))
    audit('sale_return', f"{s['receipt_no']} item {item_id} qty {qty} refund {refund}", db)
    # cash to hand back = the part of this refund the customer had already paid
    before = s['total'] - s['refunded'] - s['paid']
    after = before - refund
    return max(0, -after) - max(0, -before)


def cancel_sale(db, sid, reason, uid):
    s = _sale(db, sid)
    if s['status'] != 'Completed':
        raise RuleError('This sale is already cancelled.')
    if s['refunded'] > 0:
        raise RuleError('This sale has returns; it cannot be cancelled. Return the remaining items instead.')
    if not (reason or '').strip():
        raise RuleError('A reason is required to cancel a sale.')
    for it in db.execute('SELECT * FROM sale_items WHERE sale_id=?', (sid,)).fetchall():
        move_stock(db, it['product_id'], it['qty'], 'sale_cancel', uid, 'sale', sid,
                   s['receipt_no'], reason=reason.strip(), in_cost=it['cost'])
    db.execute("UPDATE sales SET status='Cancelled', cancel_reason=? WHERE id=?",
               (reason.strip(), sid))
    audit('sale_cancel', f"{s['receipt_no']}: {reason}", db)


def pay_sale(db, sid, amount, method, date, uid):
    s = _sale(db, sid)
    if s['status'] != 'Completed':
        raise RuleError('Cannot add payments to a cancelled sale.')
    if amount <= 0:
        raise RuleError('Amount must be greater than zero.')
    if amount > sale_balance(s):
        raise RuleError('Payment is more than the balance owed.')
    db.execute('INSERT INTO sale_payments(sale_id, amount, method, date, user_id) VALUES (?,?,?,?,?)',
               (sid, amount, method, date, uid))
    db.execute('UPDATE sales SET paid=paid+? WHERE id=?', (amount, sid))


# ---------------------------------------------------------------- stocktake
def start_stocktake(db, notes, category_id, uid, date):
    st = db.execute('INSERT INTO stocktakes(date, notes, user_id) VALUES (?,?,?)',
                    (date, notes, uid)).lastrowid
    sql = 'SELECT id, stock FROM products WHERE active=1'
    args = ()
    if category_id:
        sql += ' AND category_id=?'
        args = (category_id,)
    rows = db.execute(sql, args).fetchall()
    if not rows:
        raise RuleError('No products to count.')
    db.executemany('INSERT INTO stocktake_items(stocktake_id, product_id, system_qty) VALUES (?,?,?)',
                   [(st, r['id'], r['stock']) for r in rows])
    return st


def post_stocktake(db, st_id, uid):
    st = db.execute('SELECT * FROM stocktakes WHERE id=?', (st_id,)).fetchone()
    if not st or st['status'] != 'Open':
        raise RuleError('This stocktake is already posted.')
    rows = db.execute('SELECT si.*, p.stock FROM stocktake_items si JOIN products p '
                      'ON p.id=si.product_id WHERE stocktake_id=? AND counted_qty IS NOT NULL',
                      (st_id,)).fetchall()
    for r in rows:
        # difference against stock NOW, so sales made during counting are respected
        diff = _q(r['counted_qty'] - r['stock'])
        if abs(diff) > EPS:
            move_stock(db, r['product_id'], diff, 'stocktake', uid, 'stocktake', st_id,
                       f'ST-{st_id}', reason='Physical count')
    db.execute("UPDATE stocktakes SET status='Posted' WHERE id=?", (st_id,))
    audit('stocktake_post', f'ST-{st_id}', db)


# ---------------------------------------------------------------- profit
def profit(db, start, end):
    """Gross profit and estimated net operating profit for a date range (inclusive)."""
    s = db.execute("SELECT COALESCE(SUM(total),0) AS rev, COUNT(*) AS n FROM sales "
                   "WHERE status='Completed' AND date BETWEEN ? AND ?", (start, end)).fetchone()
    cogs = db.execute("SELECT COALESCE(SUM(si.qty*si.cost),0) AS c FROM sale_items si JOIN sales s "
                      "ON s.id=si.sale_id WHERE s.status='Completed' AND s.date BETWEEN ? AND ?",
                      (start, end)).fetchone()['c']
    r = db.execute("SELECT COALESCE(SUM(amount),0) AS refund, COALESCE(SUM(qty*cost),0) AS cost "
                   "FROM returns WHERE kind='sale' AND date BETWEEN ? AND ?", (start, end)).fetchone()
    exp = db.execute('SELECT COALESCE(SUM(amount),0) AS e FROM expenses WHERE date BETWEEN ? AND ?',
                     (start, end)).fetchone()['e']
    net_sales = s['rev'] - r['refund']
    net_cogs = rnd(cogs - r['cost'])
    gross = net_sales - net_cogs
    return {'sales_count': s['n'], 'gross_sales': s['rev'], 'returns': r['refund'],
            'net_sales': net_sales, 'cogs': net_cogs, 'gross_profit': gross,
            'expenses': exp, 'net_profit': gross - exp}
