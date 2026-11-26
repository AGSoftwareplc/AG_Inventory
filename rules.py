"""Business rules: every change to stock, cost and money goes through this file.

All functions expect to run inside core.tx() so each operation is all-or-nothing.
Money is in integer cents; quantities are floats rounded to 3 decimals.
"""
from core import RuleError, audit, branch_id, next_no, products_select, rnd, setting, fmt_qty, today

EPS = 1e-9
OUT_TYPES = {'damaged': 'Damaged', 'expired': 'Expired', 'lost': 'Lost'}
MOVE_LABELS = {
    'purchase': 'Purchase receiving', 'sale': 'Sale', 'damaged': 'Damaged', 'expired': 'Expired',
    'lost': 'Lost', 'correction': 'Stock correction', 'sale_return': 'Customer return',
    'purchase_return': 'Supplier return', 'sale_cancel': 'Sale cancellation',
    'stocktake': 'Stocktake correction', 'opening': 'Opening stock',
    'transfer_out': 'Transfer sent', 'transfer_in': 'Transfer received',
    'transfer_cancel': 'Transfer cancelled', 'batch_open': 'Opening batch (FIFO start)',
    'disposal': 'Disposal (destroyed)',
}


def _q(x):
    return round(x, 3)


def product(db, pid, branch=None):
    """A product with the stock and average cost of one branch (default: the user's branch)."""
    p = db.execute(products_select(db, '?') + ' WHERE p.id=?', (branch or branch_id(), pid)).fetchone()
    if not p:
        raise RuleError('Product not found.')
    return p


# ---------------------------------------------------------------- FIFO batches
# Stock is kept in batches (one per delivery, opening stock, transfer, ...), each with its own cost.
# Going out, stock is taken from the batch that expires first, then from the oldest batch (FIFO).
FIFO_ORDER = "ORDER BY CASE WHEN expiry IS NULL OR expiry='' THEN 1 ELSE 0 END, expiry, received, id"


def open_batches(db, pid, branch):
    return db.execute('SELECT * FROM stock_batches WHERE product_id=? AND branch_id=? AND remaining>0.0005 '
                      + FIFO_ORDER, (pid, branch)).fetchall()


# ---------------------------------------------------------------- pharmacy module
# With Admin -> Settings -> Pharmacy on, a sale only takes stock from batches that may be sold:
# status Available and not past their expiry date (expired = from the day after the expiry date).
# The order is FEFO: the batch that expires first goes first.
SELLABLE = "status='Available' AND (expiry IS NULL OR expiry='' OR expiry>=?)"


def pharmacy():
    return setting('pharmacy') == '1'


def sellable_batches(db, pid, branch):
    return db.execute('SELECT * FROM stock_batches WHERE product_id=? AND branch_id=? AND remaining>0.0005 AND '
                      + SELLABLE + ' ' + FIFO_ORDER, (pid, branch, today())).fetchall()


def medicine(db, pid):
    """The medicine details of a product, or None for general goods."""
    return db.execute('SELECT * FROM medicines WHERE product_id=?', (pid,)).fetchone()


def check_registration(m, p, what='sold'):
    """Medicines with an expired EFDA registration may not be bought or sold (BR-026)."""
    if m and m['reg_expiry'] and m['reg_expiry'] < today():
        raise RuleError(f"{p['name']}: its EFDA registration expired on {m['reg_expiry']}, so it cannot be {what}. "
                        f"Update the registration on the product first.")


def _quarantine_return(db, mid):
    """Medicines a customer brings back are kept apart (Quarantined) until a pharmacist inspects them (BR-013).
    The returned quantity is split off its original batch into a new batch with the same details."""
    for bm in db.execute('SELECT bm.batch_id, bm.qty AS mqty, bm.unit_cost, b.product_id, b.branch_id, b.ref_id, '
                         'b.ref_no, b.received, b.expiry, b.batch_no FROM batch_moves bm JOIN stock_batches b '
                         'ON b.id=bm.batch_id WHERE bm.move_id=? AND bm.qty>0', (mid,)).fetchall():
        q = bm['mqty']
        db.execute('UPDATE stock_batches SET remaining=ROUND(remaining-?, 3) WHERE id=?', (q, bm['batch_id']))
        db.execute('INSERT INTO batch_moves(batch_id, move_id, qty, unit_cost) VALUES (?,?,?,?)',
                   (bm['batch_id'], mid, -q, bm['unit_cost']))
        nid = db.execute("INSERT INTO stock_batches(product_id, branch_id, source, ref_id, ref_no, received, expiry, qty, "
                         "remaining, unit_cost, batch_no, status) VALUES (?,?,'sale_return',?,?,?,?,?,?,?,?,'Quarantined')",
                         (bm['product_id'], bm['branch_id'], bm['ref_id'], bm['ref_no'], bm['received'], bm['expiry'],
                          q, q, bm['unit_cost'], bm['batch_no'])).lastrowid
        db.execute('INSERT INTO batch_moves(batch_id, move_id, qty, unit_cost) VALUES (?,?,?,?)',
                   (nid, mid, q, bm['unit_cost']))


def _take(db, mid, batches, need):
    """Take up to `need` from the batches in the order given. Returns [(qty, unit_cost)] taken."""
    used = []
    for b in batches:
        if need <= EPS:
            break
        q = _q(min(need, b['remaining']))
        if q <= EPS:
            continue
        db.execute('UPDATE stock_batches SET remaining=ROUND(remaining-?, 3) WHERE id=?', (q, b['id']))
        db.execute('INSERT INTO batch_moves(batch_id, move_id, qty, unit_cost) VALUES (?,?,?,?)',
                   (b['id'], mid, -q, b['unit_cost']))
        used.append((q, b['unit_cost']))
        need = _q(need - q)
    return used


def _restore(db, mid, out_moves, need):
    """Put stock back into the batches it was taken from (latest taken first). Returns [(qty, cost)]."""
    if not out_moves:
        return []
    rows = db.execute(f"SELECT bm.*, b.remaining FROM batch_moves bm JOIN stock_batches b ON b.id=bm.batch_id "
                      f"WHERE bm.move_id IN ({','.join('?' * len(out_moves))}) AND bm.qty<0 ORDER BY bm.id DESC",
                      list(out_moves)).fetchall()
    used = []
    for r in rows:
        if need <= EPS:
            break
        q = _q(min(need, -r['qty'] - r['back_qty']))
        if q <= EPS:
            continue
        db.execute('UPDATE stock_batches SET remaining=ROUND(remaining+?, 3) WHERE id=?', (q, r['batch_id']))
        db.execute('UPDATE batch_moves SET back_qty=ROUND(back_qty+?, 3) WHERE id=?', (q, r['id']))
        db.execute('INSERT INTO batch_moves(batch_id, move_id, qty, unit_cost) VALUES (?,?,?,?)',
                   (r['batch_id'], mid, q, r['unit_cost']))
        used.append((q, r['unit_cost']))
        need = _q(need - q)
    return used


def _new_batch(db, mid, pid, branch, qty, cost, source, ref_id, ref_no, received, expiry=None, item=None,
               batch_no='', status='Available'):
    bid = db.execute('INSERT INTO stock_batches(product_id, branch_id, source, ref_id, ref_no, purchase_item_id, '
                     'received, expiry, qty, remaining, unit_cost, batch_no, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                     (pid, branch, source, ref_id, ref_no, item, received, expiry or None, qty, qty, cost,
                      (batch_no or '').strip(), status or 'Available')).lastrowid
    db.execute('INSERT INTO batch_moves(batch_id, move_id, qty, unit_cost) VALUES (?,?,?,?)', (bid, mid, qty, cost))
    return bid


def _avg(used):
    q = sum(u[0] for u in used)
    return sum(u[0] * u[1] for u in used) / q if q > EPS else None


def batch_value(db, pid, branch):
    """(quantity, cost value) of the open batches of a product at a branch."""
    r = db.execute('SELECT COALESCE(SUM(remaining),0) AS q, COALESCE(SUM(remaining*unit_cost),0) AS v '
                   'FROM stock_batches WHERE product_id=? AND branch_id=? AND remaining>0.0005', (pid, branch)).fetchone()
    return r['q'], r['v']


class Move:
    """Result of move_stock: the movement id, the actual unit cost (FIFO) and the stock after it."""
    def __init__(self, mid, cost, stock):
        self.id, self.cost, self.stock = mid, cost, stock


# ---------------------------------------------------------------- serial numbers
# Products with serial_tracked = 1 have one serials row per item. Each serial is unique in the business,
# belongs to one product, and keeps its whole life in serial_events (purchased, sold, returned, ...).
def parse_serials(text):
    """Serial numbers typed or scanned: one per line or separated by commas. Duplicates are refused."""
    if isinstance(text, (list, tuple)):
        items = [str(x).strip() for x in text]
    else:
        items = [x.strip() for x in (text or '').replace(';', ',').replace('\n', ',').split(',')]
    out, seen = [], set()
    for x in items:
        if not x:
            continue
        if x.lower() in seen:
            raise RuleError(f'Serial number {x} is entered twice.')
        seen.add(x.lower())
        out.append(x)
    return out


def _whole(p, qty):
    if abs(qty - round(qty)) > EPS:
        raise RuleError(f"{p['name']} has serial numbers: the quantity must be a whole number.")


def serial_event(db, serial_id, event, uid, ref_type='', ref_id=None, ref_no='', branch=None):
    db.execute('INSERT INTO serial_events(serial_id, event, ref_type, ref_id, ref_no, branch_id, user_id) '
               'VALUES (?,?,?,?,?,?,?)', (serial_id, event, ref_type, ref_id, ref_no, branch or branch_id(), uid))


def register_serials(db, p, serials, uid, event, branch=None, ref_type='', ref_id=None, ref_no='',
                     batch_id=None, purchase_id=None, item_id=None):
    """New serial-numbered items in stock (from a purchase, or stock already on hand)."""
    branch = branch or branch_id()
    for sn in serials:
        old = db.execute('SELECT s.*, p.name FROM serials s JOIN products p ON p.id=s.product_id WHERE s.serial=?',
                         (sn,)).fetchone()
        if old:
            raise RuleError(f"Serial number {sn} is already registered ({old['name']}, {old['status'].lower()}).")
        sid = db.execute('INSERT INTO serials(product_id, serial, branch_id, status, batch_id, purchase_id, '
                         "purchase_item_id) VALUES (?,?,?,'In stock',?,?,?)",
                         (p['id'], sn, branch, batch_id, purchase_id, item_id)).lastrowid
        serial_event(db, sid, event, uid, ref_type, ref_id, ref_no, branch)


def serials_in_stock(db, pid, branch=None):
    return db.execute("SELECT * FROM serials WHERE product_id=? AND branch_id=? AND status='In stock' ORDER BY serial",
                      (pid, branch or branch_id())).fetchall()


def serials_out(db, p, qty, serials, status, event, uid, ref_type='', ref_id=None, ref_no='', branch=None,
                strict=False, sale=None, item_id=None, purchase_item=None, transfer=None):
    """Serial-numbered items leaving stock (sold, returned to the supplier, damaged, sent to a branch, ...).

    strict: every item must be named (sales). Otherwise items not named may come from stock that has no
    serial number registered yet, but never from registered items."""
    if not p['serial_tracked']:
        return []
    branch = branch or branch_id()
    _whole(p, qty)
    if len(serials) > round(qty):
        raise RuleError(f"{p['name']}: {len(serials)} serial numbers entered for a quantity of {fmt_qty(qty)}.")
    if strict and len(serials) != round(qty):
        raise RuleError(f"{p['name']} needs a serial number for every item: {fmt_qty(qty)} needed, "
                        f"{len(serials)} entered.")
    rows = []
    for sn in serials:
        r = db.execute('SELECT * FROM serials WHERE serial=?', (sn,)).fetchone()
        if not r or r['product_id'] != p['id']:
            raise RuleError(f"Serial number {sn} is not registered for {p['name']}.")
        if r['status'] == 'Sold':
            raise RuleError(f'Serial number {sn} is already sold.')
        if r['status'] != 'In stock' or r['branch_id'] != branch:
            raise RuleError(f"Serial number {sn} is not in stock here ({r['status'].lower()}).")
        if purchase_item and r['purchase_item_id'] != purchase_item:
            raise RuleError(f'Serial number {sn} did not come with this purchase.')
        rows.append(r)
    if not strict and len(rows) < round(qty):
        unnamed = round(qty) - len(rows)
        free = _q(product(db, p['id'], branch)['stock'] - len(serials_in_stock(db, p['id'], branch)))
        if unnamed > free + EPS:
            raise RuleError(f"{p['name']}: enter the serial numbers of the items (only {fmt_qty(max(free, 0))} "
                            f"in stock have no serial number).")
    for r in rows:
        db.execute('UPDATE serials SET status=?, sale_id=COALESCE(?, sale_id), sale_item_id=COALESCE(?, sale_item_id), '
                   'transfer_id=COALESCE(?, transfer_id) WHERE id=?', (status, sale, item_id, transfer, r['id']))
        serial_event(db, r['id'], event, uid, ref_type, ref_id, ref_no, branch)
    return rows


def serials_back(db, rows, uid, event, branch, ref_type='', ref_id=None, ref_no='', keep_sale=False):
    """Serial-numbered items coming back into stock (customer return, cancelled sale or transfer)."""
    for r in rows:
        if keep_sale:
            db.execute("UPDATE serials SET status='In stock', branch_id=? WHERE id=?", (branch, r['id']))
        else:
            db.execute("UPDATE serials SET status='In stock', branch_id=?, sale_id=NULL, sale_item_id=NULL WHERE id=?",
                       (branch, r['id']))
        serial_event(db, r['id'], event, uid, ref_type, ref_id, ref_no, branch)


# ---------------------------------------------------------------- core stock movement
def move_stock(db, pid, qty, move_type, uid, ref_type='', ref_id=None, ref_no='', reason='',
               in_cost=None, branch=None, batches=None, restore=None, layers=None, expiry=None,
               purchase_item=None, received=None, batch_no=''):
    """Change stock of one product at one branch (default: the user's branch) and record the movement.

    Stock going out (qty < 0) is taken from the FIFO batches (or only from `batches`, if given) and
    its unit cost is the cost of the batches it came from.
    Stock coming in (qty > 0):
      restore = ids of earlier outgoing movements: it goes back into those same batches (returns, cancels);
      layers = [(qty, cost, received, expiry[, batch_no, status])]: new batches like these (transfers keep
               the original batches);
      otherwise one new batch at in_cost (default: the current average cost).
    The average cost kept on the product is the value of its open batches divided by their quantity.
    """
    branch = branch or branch_id()
    p = product(db, pid, branch)
    qty = _q(qty)
    old_stock, avg = p['stock'], p['avg_cost']
    new_stock = _q(old_stock + qty)
    if qty < 0 and new_stock < -EPS and setting('allow_negative') != '1':
        raise RuleError(f"Not enough stock for {p['name']}: available {fmt_qty(old_stock)} "
                        f"{p['unit']}, needed {fmt_qty(-qty)}.")
    mid = db.execute('INSERT INTO stock_moves(product_id, qty, move_type, ref_type, ref_id, ref_no, '
                     'unit_cost, balance, reason, user_id, branch_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                     (pid, qty, move_type, ref_type, ref_id, ref_no, avg, new_stock, reason, uid, branch)).lastrowid
    day = received or today()
    if qty < 0:
        need = -qty
        if batches is not None:
            pool = batches
        elif move_type == 'sale' and pharmacy():  # never sell expired, recalled or quarantined stock
            pool = sellable_batches(db, pid, branch)
            ok = _q(sum(b['remaining'] for b in pool))
            if ok < need - EPS:
                raise RuleError(f"{p['name']}: only {fmt_qty(ok)} {p['unit']} can be sold. The rest of the stock is "
                                f"expired, recalled, quarantined or damaged (Pharmacy → Expiry & batches).")
        else:
            pool = open_batches(db, pid, branch)
        used = _take(db, mid, pool, need)
        short = _q(need - sum(u[0] for u in used))
        if short > EPS:
            if batches is not None:
                raise RuleError(f"{p['name']}: only {fmt_qty(need - short)} {p['unit']} of this batch is still "
                                f"in stock.")
            used.append((short, avg))  # selling below zero (allowed in settings): at the average cost
        unit_cost = _avg(used)
    else:
        # stock that only fills a negative balance does not go into a batch
        add = _q(max(0.0, new_stock) - max(0.0, old_stock))
        used = _restore(db, mid, restore, add) if restore else []
        left = _q(add - sum(u[0] for u in used))
        for layer in (layers or []):
            lq, lc, lr, le = layer[:4]
            if left <= EPS:
                break
            q = _q(min(lq, left))
            _new_batch(db, mid, pid, branch, q, lc, move_type, ref_id, ref_no, lr or day, le, None,
                       *(tuple(layer[4:6])))
            used.append((q, lc))
            left = _q(left - q)
        if left > EPS:
            cost = in_cost if in_cost is not None else (_avg(used) if used else avg)
            _new_batch(db, mid, pid, branch, left, cost, move_type, ref_id, ref_no, day, expiry, purchase_item,
                       batch_no)
            used.append((left, cost))
        unit_cost = _avg(used)
        if unit_cost is None:
            unit_cost = in_cost if in_cost is not None else avg
    bq, bv = batch_value(db, pid, branch)
    if bq > EPS:
        avg = bv / bq
    elif qty > 0:
        avg = unit_cost
    db.execute('UPDATE stock_moves SET unit_cost=? WHERE id=?', (unit_cost, mid))
    db.execute('INSERT INTO product_stock(product_id, branch_id, stock, avg_cost) VALUES (?,?,?,?) '
               'ON CONFLICT(product_id, branch_id) DO UPDATE SET stock=excluded.stock, avg_cost=excluded.avg_cost',
               (pid, branch, new_stock, avg))
    # products keeps the total over all branches, and the latest cost for branches without stock yet
    db.execute('UPDATE products SET stock=ROUND(stock+?, 3), avg_cost=? WHERE id=?', (qty, avg, pid))
    return Move(mid, unit_cost, new_stock)


def doc_moves(db, ref_type, ref_id, pid, move_type):
    """Ids of the stock movements of one document line (e.g. the sale of one product)."""
    return [r['id'] for r in db.execute('SELECT id FROM stock_moves WHERE ref_type=? AND ref_id=? AND product_id=? '
                                        'AND move_type=?', (ref_type, ref_id, pid, move_type))]


def _batch(db, batch_id, pid, branch):
    b = db.execute('SELECT * FROM stock_batches WHERE id=? AND product_id=? AND branch_id=?',
                   (batch_id, pid, branch)).fetchone()
    if not b:
        raise RuleError('Batch not found for this product in this branch.')
    return b


def set_batch_expiry(db, batch_id, expiry):
    b = db.execute('SELECT * FROM stock_batches WHERE id=? AND branch_id=?', (batch_id, branch_id())).fetchone()
    if not b:
        raise RuleError('Batch not found.')
    db.execute('UPDATE stock_batches SET expiry=? WHERE id=?', (expiry or None, batch_id))
    audit('batch_expiry', f"batch {batch_id} expiry {expiry or '-'}", db)


def adjust_stock(db, pid, kind, qty, reason, uid, batch_id=None, serials=None):
    """Manual adjustment. kind: damaged/expired/lost (remove) or correction (signed qty).
    batch_id: the batch it concerns (e.g. the expired one); default FIFO / a new batch.
    serials: serial numbers of the items (serial-numbered products)."""
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
    kw = {}
    if batch_id:
        b = _batch(db, batch_id, pid, branch_id())
        if qty < 0:
            kw['batches'] = [b]
        else:  # found again: a batch like that one (same cost and dates)
            kw['layers'] = [(qty, b['unit_cost'], b['received'], b['expiry'], b['batch_no'], b['status'])]
            kw['in_cost'] = b['unit_cost']
    p = product(db, pid)
    serials = parse_serials(serials)
    if qty < 0:
        serials_out(db, p, -qty, serials, OUT_TYPES.get(kind, 'Removed'), MOVE_LABELS[kind], uid, 'adjustment',
                    ref_no=reason.strip())
    m = move_stock(db, pid, qty, kind, uid, ref_type='adjustment', reason=reason.strip(), **kw)
    if qty > 0 and p['serial_tracked']:
        _whole(p, qty)
        if len(serials) > round(qty):
            raise RuleError(f'{len(serials)} serial numbers entered for a quantity of {fmt_qty(qty)}.')
        bid = db.execute('SELECT batch_id FROM batch_moves WHERE move_id=? ORDER BY id DESC', (m.id,)).fetchone()
        register_serials(db, p, serials, uid, 'Added by stock adjustment', ref_type='adjustment', ref_no=reason.strip(),
                         batch_id=bid['batch_id'] if bid else None)
    audit('stock_adjust', f'product {pid} {kind} {qty}' + (f' batch {batch_id}' if batch_id else '') + f': {reason}', db)


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
    """Create or update a Draft purchase.
    lines = [(product_id, qty, unit_cost_cents)] or
            [(product_id, qty, unit_cost_cents, expiry, serials_text[, batch_no])]."""
    if not lines:
        raise RuleError('Add at least one product line.')
    if not db.execute('SELECT 1 FROM suppliers WHERE id=?', (supplier_id,)).fetchone():
        raise RuleError('Choose a supplier.')
    lines = [tuple(l) + (None, '', '')[len(l) - 3:] for l in lines]
    gross = sum(rnd(q * c) for _, q, c, _e, _s, _b in lines)
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
                         'user_id, branch_id) VALUES (?,?,?,?,?,?,?,?)',
                         (ref, supplier_id, date, discount, total, notes, uid, branch_id()))
        pur_id = cur.lastrowid
    for pid, q, c, exp, sers, bno in lines:
        p = product(db, pid)
        sers = parse_serials(sers)
        if p['serial_tracked']:
            _whole(p, q)
        db.execute('INSERT INTO purchase_items(purchase_id, product_id, qty, unit_cost, expiry, serials, batch_no) '
                   'VALUES (?,?,?,?,?,?,?)', (pur_id, pid, q, c, exp or None, '\n'.join(sers), (bno or '').strip()))
    return pur_id


def _purchase(db, pur_id):
    pur = db.execute('SELECT * FROM purchases WHERE id=?', (pur_id,)).fetchone()
    if not pur:
        raise RuleError('Purchase not found.')
    return pur


def confirm_purchase(db, pur_id, uid):
    """Confirming a purchase ends it: everything on it goes into stock now, each line as its own FIFO batch
    (with its expiry date and serial numbers). There is no separate receiving step."""
    pur = _purchase(db, pur_id)
    if pur['status'] != 'Draft':
        raise RuleError('Only draft purchases can be confirmed.')
    db.execute("UPDATE purchases SET status='Confirmed' WHERE id=?", (pur_id,))
    items = db.execute('SELECT * FROM purchase_items WHERE purchase_id=?', (pur_id,)).fetchall()
    receive_purchase(db, pur_id, {i['id']: i['qty'] for i in items}, uid,
                     {i['id']: i['expiry'] for i in items if i['expiry']},
                     {i['id']: i['serials'] for i in items}, {i['id']: i['batch_no'] for i in items})
    audit('purchase_confirm', pur['ref_no'], db)


def cancel_purchase(db, pur_id, reason, uid):
    pur = _purchase(db, pur_id)
    if pur['status'] not in ('Draft', 'Confirmed') or db.execute(
            'SELECT 1 FROM purchase_items WHERE purchase_id=? AND received_qty>0', (pur_id,)).fetchone():
        raise RuleError('This purchase is already in stock, so it cannot be cancelled. '
                        'Record a supplier return instead.')
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


def receive_purchase(db, pur_id, receipts, uid, expiries=None, serials=None, batch_nos=None):
    """receipts = {item_id: qty_received_now}; expiries = {item_id: 'YYYY-MM-DD'} (optional);
    serials = {item_id: serial numbers} (needed for serial-numbered products).
    Each line received becomes a new FIFO batch at the net unit cost.
    Used by confirm_purchase, and for older purchases that were confirmed before receiving was automatic."""
    expiries, serials, batch_nos = expiries or {}, serials or {}, batch_nos or {}
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
        p = product(db, it['product_id'], pur['branch_id'])
        med = medicine(db, p['id']) if pharmacy() else None
        if med:  # every batch of a medicine has a batch number and an expiry date (BR-003)
            check_registration(med, p, 'purchased')
            if not (batch_nos.get(item_id) or '').strip() or not expiries.get(item_id):
                raise RuleError(f"{p['name']}: enter the batch number and the expiry date of this medicine.")
            if expiries[item_id] < today():
                raise RuleError(f"{p['name']}: batch {batch_nos[item_id]} expired on {expiries[item_id]}. "
                                f"Expired medicines cannot be received.")
        sers = parse_serials(serials.get(item_id))
        if p['serial_tracked']:
            _whole(p, q)
            if len(sers) != round(q):
                raise RuleError(f"{p['name']} has serial numbers: enter one serial number per item "
                                f"({fmt_qty(q)} needed, {len(sers)} entered).")
        db.execute('UPDATE purchase_items SET received_qty=received_qty+? WHERE id=?', (q, item_id))
        m = move_stock(db, it['product_id'], q, 'purchase', uid, 'purchase', pur_id, pur['ref_no'],
                       in_cost=net_unit_cost(it, pur, gross), branch=pur['branch_id'],
                       expiry=expiries.get(item_id), purchase_item=item_id, batch_no=batch_nos.get(item_id) or '')
        if p['serial_tracked']:
            bid = db.execute('SELECT batch_id FROM batch_moves WHERE move_id=? ORDER BY id DESC', (m.id,)).fetchone()
            register_serials(db, p, sers, uid, 'Purchased', pur['branch_id'], 'purchase', pur_id, pur['ref_no'],
                             bid['batch_id'] if bid else None, pur_id, item_id)
        done += 1
    if not done:
        raise RuleError('Enter a quantity to receive.')
    _refresh_purchase_status(db, pur_id)


def purchase_return_doc(db, pur_id, lines, uid, date, notes=''):
    """A return to the supplier as its own transaction, tied to the original (confirmed) purchase.
    lines = [(item_id, qty, reason, serials_text)]. Returns (return id, credit)."""
    pur = _purchase(db, pur_id)
    if pur['status'] not in ('Partially Received', 'Received'):
        raise RuleError('Goods can only be returned on a confirmed purchase that is in stock.')
    lines = [l for l in lines if l[1] > EPS]
    if not lines:
        raise RuleError('Enter the quantity to return on at least one line.')
    ref = next_no(db, 'PR', 'purchase_returns', 'ref_no')
    rid = db.execute('INSERT INTO purchase_returns(ref_no, purchase_id, date, notes, user_id, branch_id) '
                     'VALUES (?,?,?,?,?,?)', (ref, pur_id, date, notes, uid, pur['branch_id'])).lastrowid
    credit = 0
    for item_id, qty, reason, sers in lines:
        credit += purchase_return(db, pur_id, item_id, qty, reason, uid, date, sers, rid, ref)
    db.execute('UPDATE purchase_returns SET amount=? WHERE id=?', (credit, rid))
    audit('purchase_return_doc', f"{ref} for {pur['ref_no']}: {credit}", db)
    return rid, credit


def purchase_return(db, pur_id, item_id, qty, reason, uid, date, serials=None, return_id=None, return_no=''):
    pur = _purchase(db, pur_id)
    if pur['status'] not in ('Partially Received', 'Received'):
        raise RuleError('Goods can only be returned on a confirmed purchase that is in stock.')
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
    # the goods go back out of the batches this purchase line brought in (older data: FIFO)
    own = db.execute('SELECT * FROM stock_batches WHERE purchase_item_id=? AND remaining>0.0005 ORDER BY id',
                     (item_id,)).fetchall()
    had = db.execute('SELECT 1 FROM stock_batches WHERE purchase_item_id=? LIMIT 1', (item_id,)).fetchone()
    p = product(db, it['product_id'], pur['branch_id'])
    serials_out(db, p, qty, parse_serials(serials), 'Returned to supplier', 'Returned to supplier', uid, 'purchase',
                pur_id, return_no or pur['ref_no'], pur['branch_id'], purchase_item=item_id)
    move_stock(db, it['product_id'], -qty, 'purchase_return', uid, 'purchase', pur_id,
               pur['ref_no'], reason=(f'{return_no}: ' if return_no else '') + reason.strip(), branch=pur['branch_id'],
               batches=own if had else None)
    db.execute('UPDATE purchase_items SET returned_qty=returned_qty+? WHERE id=?', (qty, item_id))
    db.execute('UPDATE purchases SET returned_value=returned_value+? WHERE id=?', (credit, pur_id))
    db.execute('INSERT INTO returns(kind, doc_id, item_id, product_id, qty, amount, cost, reason, '
               'date, user_id, branch_id, return_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
               ('purchase', pur_id, item_id, it['product_id'], qty, credit, cost, reason.strip(),
                date, uid, pur['branch_id'], return_id))
    audit('purchase_return', f"{pur['ref_no']} item {item_id} qty {qty}", db)
    return credit


def purchase_balance(pur):
    return pur['total'] - pur['returned_value'] - pur['paid']


def pay_purchase(db, pur_id, amount, method, date, uid, reference=''):
    pur = _purchase(db, pur_id)
    if pur['status'] in ('Draft', 'Cancelled'):
        raise RuleError('Confirm the purchase before recording payments.')
    if amount <= 0:
        raise RuleError('Amount must be greater than zero.')
    if amount > purchase_balance(pur):
        raise RuleError('Payment is more than the amount owed.')
    db.execute('INSERT INTO purchase_payments(purchase_id, amount, method, date, user_id, reference) '
               'VALUES (?,?,?,?,?,?)', (pur_id, amount, method, date, uid, (reference or '').strip()))
    db.execute('UPDATE purchases SET paid=paid+? WHERE id=?', (amount, pur_id))
    audit('purchase_payment', f"{pur['ref_no']} {amount} {method} {reference or ''}".strip(), db)


def payment_status(total, paid):
    if total <= 0 or paid >= total:
        return 'Paid'
    return 'Partially paid' if paid > 0 else 'Unpaid'


# ---------------------------------------------------------------- sales
def create_sale(db, lines, discount, tendered, method, customer_id, uid, date,
                allow_discount=False, allow_credit=True, reference='', allow_price=False,
                manual=False, seller_id=None, deduct=True, notes='', prescription=None):
    """lines = [(product_id, qty)] or [(product_id, qty, price_cents_or_None, serials)].
    Default prices and costs come from the database, not the browser. A different price is only taken
    from users allowed to change prices; the default price is kept beside it so the difference is known.
    Serial-numbered products need one valid, unsold serial number per item.
    Stock leaves when the sale is confirmed, whether it is fully paid, partly paid or unpaid.
    The cost of each line is the cost of the FIFO batches it was taken from.

    manual = a sale made outside the app, recorded later: seller_id is the actual seller, uid the user who
    records it. deduct=False when the stock was already taken out (no second deduction).

    Pharmacy module: prescription-only and controlled medicines are only sold through a verified prescription
    (prescription = {'id': prescription id, 'products': {product ids on it}}, set by pharmacy.py)."""
    if not lines:
        raise RuleError('The cart is empty.')
    merged = {}
    for line in lines:
        pid, q = line[0], line[1]
        price = line[2] if len(line) > 2 else None
        sers = parse_serials(line[3] if len(line) > 3 else None)
        if q <= EPS:
            raise RuleError('Quantities must be greater than zero.')
        if price is not None and price < 0:
            raise RuleError('A selling price cannot be negative.')
        key = (pid, price)
        old = merged.get(key, (0, []))
        merged[key] = (_q(old[0] + q), old[1] + sers)
    if discount and not allow_discount:
        raise RuleError('You are not allowed to give discounts.')
    if customer_id and not db.execute('SELECT 1 FROM customers WHERE id=?', (customer_id,)).fetchone():
        raise RuleError('Customer not found.')
    if manual:
        if not seller_id or not db.execute('SELECT 1 FROM users WHERE id=?', (seller_id,)).fetchone():
            raise RuleError('Choose the person who actually made the sale.')
        if date > today():
            raise RuleError('The sale date cannot be in the future.')
    seller = seller_id if manual else uid
    items, subtotal, need = [], 0, {}
    for (pid, price), (q, sers) in merged.items():
        p = product(db, pid)
        if not p['active']:
            raise RuleError(f"{p['name']} is not active.")
        med = medicine(db, pid) if pharmacy() else None
        if med:
            check_registration(med, p)
            if med['rx_class'] != 'OTC' and not (prescription and pid in prescription['products']):
                raise RuleError(f"{p['name']} is {'a controlled medicine' if med['rx_class'] in ('Narcotic', 'Psychotropic') else 'prescription-only'} "
                                f"({med['rx_class']}): it can only be sold from a verified prescription "
                                f"(Pharmacy → Prescriptions).")
        need[pid] = _q(need.get(pid, 0) + q)
        if deduct and need[pid] > p['stock'] + EPS and setting('allow_negative') != '1':
            raise RuleError(f"Not enough stock for {p['name']}: available {fmt_qty(p['stock'])} "
                            f"{p['unit']}.")
        if price is None or price == p['price']:
            price, changed = p['price'], False
        elif not allow_price:
            raise RuleError(f"You are not allowed to change the selling price ({p['name']}).")
        else:
            changed = True
        if p['serial_tracked']:
            _whole(p, q)
        subtotal += rnd(q * price)
        items.append((p, q, price, changed, sers))
    if discount > subtotal:
        raise RuleError('Discount cannot be larger than the subtotal.')
    total = subtotal - discount
    paid = min(tendered, total)
    if paid < total and not customer_id:
        raise RuleError('Amount paid is less than the total. Choose a registered customer to '
                        'sell on credit.')
    if paid < total and not allow_credit:
        raise RuleError('You are not allowed to sell on credit. The full amount must be paid.')
    change = max(0, tendered - total)
    receipt = next_no(db, 'M' if manual else 'S', 'sales', 'receipt_no')
    sid = db.execute('INSERT INTO sales(receipt_no, customer_id, date, subtotal, discount, total, paid, tendered, '
                     'change_due, method, user_id, branch_id, manual, entered_by, no_stock, notes, prescription_id) '
                     'VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                     (receipt, customer_id, date, subtotal, discount, total, paid, tendered, change, method,
                      seller, branch_id(), 1 if manual else 0, uid, 0 if deduct else 1, (notes or '').strip(),
                      prescription['id'] if prescription else None)).lastrowid
    for p, q, price, changed, sers in items:
        iid = db.execute('INSERT INTO sale_items(sale_id, product_id, qty, price, cost, default_price, price_by) '
                         'VALUES (?,?,?,?,0,?,?)', (sid, p['id'], q, price, p['price'],
                                                    uid if changed else None)).lastrowid
        if deduct:
            serials_out(db, p, q, sers, 'Sold', 'Manual sale' if manual else 'Sold', uid, 'sale', sid, receipt,
                        strict=True, sale=sid, item_id=iid)
            cost = move_stock(db, p['id'], -q, 'sale', uid, 'sale', sid, receipt,
                              reason='Manual sale' if manual else '').cost
        else:  # stock already taken out earlier: record the sale at today's average cost, move nothing
            cost = p['avg_cost']
        db.execute('UPDATE sale_items SET cost=? WHERE id=?', (cost, iid))
        if changed:
            audit('sale_price_change', f"{receipt} {p['code']}: {p['price']} -> {price}", db)
    if paid > 0:
        db.execute('INSERT INTO sale_payments(sale_id, amount, method, date, user_id, reference) '
                   'VALUES (?,?,?,?,?,?)', (sid, paid, method, date, uid, (reference or '').strip()))
    if manual:
        audit('manual_sale', f"{receipt} seller {seller_id}" + ('' if deduct else ' (stock already deducted)'), db)
    return sid


def _sale(db, sid):
    s = db.execute('SELECT * FROM sales WHERE id=?', (sid,)).fetchone()
    if not s:
        raise RuleError('Sale not found.')
    return s


def sale_balance(s):
    """Amount the customer still owes (negative = shop owes a refund)."""
    return s['total'] - s['refunded'] - s['paid']


def sale_return(db, sid, item_id, qty, reason, uid, date, serials=None):
    """Customer return: stock back at the original cost, refund = value after discount share.
    Serial-numbered items: the serial numbers that come back (sold on this sale)."""
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
    p = product(db, it['product_id'], s['branch_id'])
    back = []
    if p['serial_tracked'] and not s['no_stock']:
        _whole(p, qty)
        names = parse_serials(serials)
        sold = {r['serial'].lower(): r for r in db.execute("SELECT * FROM serials WHERE sale_item_id=? AND status='Sold'",
                                                            (item_id,))}
        if sold and len(names) != round(qty):
            raise RuleError(f"Enter the serial number of each returned item ({fmt_qty(qty)} needed).")
        for n in names:
            if n.lower() not in sold:
                raise RuleError(f'Serial number {n} was not sold on this sale.')
            back.append(sold[n.lower()])
    # back into the batches the sale took them from, at their cost
    m = move_stock(db, it['product_id'], qty, 'sale_return', uid, 'sale', sid, s['receipt_no'],
                   reason=reason.strip(), in_cost=it['cost'], branch=s['branch_id'],
                   restore=doc_moves(db, 'sale', sid, it['product_id'], 'sale'))
    if pharmacy() and medicine(db, it['product_id']):
        _quarantine_return(db, m.id)
    serials_back(db, back, uid, 'Customer return', s['branch_id'], 'sale', sid, s['receipt_no'])
    db.execute('UPDATE sale_items SET returned_qty=returned_qty+? WHERE id=?', (qty, item_id))
    db.execute('UPDATE sales SET refunded=refunded+? WHERE id=?', (refund, sid))
    db.execute('INSERT INTO returns(kind, doc_id, item_id, product_id, qty, amount, cost, reason, '
               'date, user_id, branch_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
               ('sale', sid, item_id, it['product_id'], qty, refund, m.cost, reason.strip(),
                date, uid, s['branch_id']))
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
        left = _q(it['qty'] - it['returned_qty'])
        if s['no_stock']:  # this sale never took stock out, so cancelling it puts nothing back
            continue
        serials_back(db, db.execute("SELECT * FROM serials WHERE sale_item_id=? AND status='Sold'", (it['id'],)).fetchall(),
                     uid, 'Sale cancelled', s['branch_id'], 'sale', sid, s['receipt_no'])
        if left > EPS:
            move_stock(db, it['product_id'], left, 'sale_cancel', uid, 'sale', sid,
                       s['receipt_no'], reason=reason.strip(), in_cost=it['cost'], branch=s['branch_id'],
                       restore=doc_moves(db, 'sale', sid, it['product_id'], 'sale'))
    db.execute("UPDATE sales SET status='Cancelled', cancel_reason=? WHERE id=?",
               (reason.strip(), sid))
    audit('sale_cancel', f"{s['receipt_no']}: {reason}", db)


def pay_sale(db, sid, amount, method, date, uid, reference=''):
    """A further payment against what the customer still owes (no new sale, no stock change)."""
    s = _sale(db, sid)
    if s['status'] != 'Completed':
        raise RuleError('Cannot add payments to a cancelled sale.')
    if amount <= 0:
        raise RuleError('Amount must be greater than zero.')
    if amount > sale_balance(s):
        raise RuleError('Payment is more than the balance owed.')
    db.execute('INSERT INTO sale_payments(sale_id, amount, method, date, user_id, reference) VALUES (?,?,?,?,?,?)',
               (sid, amount, method, date, uid, (reference or '').strip()))
    db.execute('UPDATE sales SET paid=paid+? WHERE id=?', (amount, sid))
    audit('sale_payment', f"{s['receipt_no']} {amount} {method} {reference or ''}".strip(), db)


# ---------------------------------------------------------------- stocktake
def start_stocktake(db, notes, category_id, uid, date):
    st = db.execute('INSERT INTO stocktakes(date, notes, user_id, branch_id) VALUES (?,?,?,?)',
                    (date, notes, uid, branch_id())).lastrowid
    sql = 'SELECT id, stock FROM pv WHERE active=1'
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
    rows = db.execute('SELECT si.*, COALESCE(ps.stock, 0) AS stock FROM stocktake_items si LEFT JOIN product_stock ps '
                      'ON ps.product_id=si.product_id AND ps.branch_id=? WHERE stocktake_id=? AND counted_qty IS NOT NULL',
                      (st['branch_id'], st_id)).fetchall()
    for r in rows:
        # difference against stock NOW, so sales made during counting are respected
        diff = _q(r['counted_qty'] - r['stock'])
        if abs(diff) > EPS:
            move_stock(db, r['product_id'], diff, 'stocktake', uid, 'stocktake', st_id,
                       f'ST-{st_id}', reason='Physical count', branch=st['branch_id'])
    db.execute("UPDATE stocktakes SET status='Posted' WHERE id=?", (st_id,))
    audit('stocktake_post', f'ST-{st_id}', db)


# ---------------------------------------------------------------- transfers between branches
def send_transfer(db, to_branch, lines, notes, uid, date):
    """Send stock from the user's branch: it leaves now at its average cost and is In transit."""
    src = branch_id()
    dest = db.execute('SELECT * FROM branches WHERE id=? AND active=1', (to_branch,)).fetchone()
    if not dest or dest['id'] == src:
        raise RuleError('Choose another open branch to send to.')
    merged, sers = {}, {}
    for line in lines:
        pid, q = line[0], line[1]
        if q <= EPS:
            raise RuleError('Quantities must be greater than zero.')
        merged[pid] = _q(merged.get(pid, 0) + q)
        sers[pid] = sers.get(pid, []) + parse_serials(line[2] if len(line) > 2 else None)
    if not merged:
        raise RuleError('Add at least one product line.')
    ref = next_no(db, 'T', 'transfers', 'ref_no')
    tid = db.execute('INSERT INTO transfers(ref_no, from_branch, to_branch, date, notes, user_id) '
                     'VALUES (?,?,?,?,?,?)', (ref, src, dest['id'], date, notes, uid)).lastrowid
    for pid, q in merged.items():
        p = product(db, pid, src)
        if p['stock'] < q - EPS:  # never send what the branch does not have
            raise RuleError(f"Not enough stock for {p['name']}: available {fmt_qty(p['stock'])} {p['unit']}.")
        serials_out(db, p, q, sers[pid], 'In transit', f"Sent to {dest['name']}", uid, 'transfer', tid, ref, src,
                    transfer=tid)
        m = move_stock(db, pid, -q, 'transfer_out', uid, 'transfer', tid, ref, reason=f"To {dest['name']}",
                       branch=src)
        db.execute('INSERT INTO transfer_items(transfer_id, product_id, qty, unit_cost) VALUES (?,?,?,?)',
                   (tid, pid, q, m.cost))
    audit('transfer_send', f"{ref} to {dest['code']}", db)
    return tid


def _transfer(db, tid):
    t = db.execute('SELECT * FROM transfers WHERE id=?', (tid,)).fetchone()
    if not t:
        raise RuleError('Transfer not found.')
    if t['status'] != 'In transit':
        raise RuleError('This transfer is already finished.')
    return t


def receive_transfer(db, tid, received, uid):
    """received = {item_id: qty that arrived}. Stock comes in at the sending branch's cost.
    Anything that did not arrive is recorded on the transfer as lost in transit."""
    t = _transfer(db, tid)
    lost = []
    for it in db.execute('SELECT * FROM transfer_items WHERE transfer_id=?', (tid,)).fetchall():
        q = _q(received.get(it['id'], it['qty']))
        if q < -EPS or q > it['qty'] + EPS:
            raise RuleError(f'Received quantity must be between 0 and {fmt_qty(it["qty"])}.')
        moving = db.execute("SELECT * FROM serials WHERE transfer_id=? AND product_id=? AND status='In transit'",
                            (tid, it['product_id'])).fetchall()
        if moving and abs(q - it['qty']) > EPS:
            raise RuleError('Serial-numbered items: receive the full quantity, then record any missing item as '
                            'Lost with its serial number in Adjust stock.')
        serials_back(db, moving, uid, 'Transfer received', t['to_branch'], 'transfer', tid, t['ref_no'])
        if q > EPS:
            # the receiving branch gets the same batches (cost, received and expiry dates) that left
            out = db.execute('SELECT bm.qty, bm.unit_cost, b.received, b.expiry, b.batch_no, b.status FROM batch_moves bm '
                             'JOIN stock_batches b '
                             'ON b.id=bm.batch_id JOIN stock_moves m ON m.id=bm.move_id WHERE m.ref_type=\'transfer\' '
                             'AND m.ref_id=? AND m.product_id=? AND m.move_type=\'transfer_out\' AND bm.qty<0 ORDER BY bm.id',
                             (tid, it['product_id'])).fetchall()
            move_stock(db, it['product_id'], q, 'transfer_in', uid, 'transfer', tid, t['ref_no'],
                       in_cost=it['unit_cost'], branch=t['to_branch'],
                       layers=[(-o['qty'], o['unit_cost'], o['received'], o['expiry'], o['batch_no'], o['status'])
                               for o in out])
        if it['qty'] - q > EPS:
            lost.append(f"product {it['product_id']} {fmt_qty(it['qty'] - q)}")
        db.execute('UPDATE transfer_items SET received_qty=? WHERE id=?', (q, it['id']))
    db.execute("UPDATE transfers SET status='Received', received_by=?, received_at=datetime('now','localtime') "
               "WHERE id=?", (uid, tid))
    audit('transfer_receive', t['ref_no'] + (f"; lost in transit: {', '.join(lost)}" if lost else ''), db)


def cancel_transfer(db, tid, reason, uid):
    """Stop a transfer that has not arrived: the stock goes back to the sending branch at the same cost."""
    t = _transfer(db, tid)
    if not (reason or '').strip():
        raise RuleError('A reason is required to cancel.')
    serials_back(db, db.execute("SELECT * FROM serials WHERE transfer_id=? AND status='In transit'", (tid,)).fetchall(),
                 uid, 'Transfer cancelled', t['from_branch'], 'transfer', tid, t['ref_no'])
    for it in db.execute('SELECT * FROM transfer_items WHERE transfer_id=?', (tid,)).fetchall():
        move_stock(db, it['product_id'], it['qty'], 'transfer_cancel', uid, 'transfer', tid, t['ref_no'],
                   reason=reason.strip(), in_cost=it['unit_cost'], branch=t['from_branch'],
                   restore=doc_moves(db, 'transfer', tid, it['product_id'], 'transfer_out'))
    db.execute("UPDATE transfers SET status='Cancelled', cancel_reason=? WHERE id=?", (reason.strip(), tid))
    audit('transfer_cancel', f"{t['ref_no']}: {reason}", db)


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
