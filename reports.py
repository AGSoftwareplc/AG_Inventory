"""Dashboard and reports (on screen, print / PDF, and Excel export)."""
import io
import zipfile
from datetime import date, timedelta
from xml.sax.saxutils import escape

from flask import Blueprint, Response, g, render_template, request

import core
from core import ADMIN, branch_id, can, date_range, get_db, login_required, requires, setting, uses_starter_password
import rules

dash = Blueprint('dash', __name__)
bp = Blueprint('reports', __name__, url_prefix='/reports')


# ---------------------------------------------------------------- which branch the figures are for
def report_branch():
    """Branch chosen on the dashboard or report (?branch=), default the one in the top bar.
    None = all branches (administrators only)."""
    v = request.args.get('branch', '')
    if v == 'all' and g.user['role'] == ADMIN:
        return None
    mine = {b['id'] for b in g.branches}
    return int(v) if v.isdigit() and int(v) in mine else branch_id()


def rdb():
    """Read-only connection that sees only the chosen branch (see core.report_db)."""
    if 'rdb' not in g:
        g.rdb = core.report_db(report_branch())
    return g.rdb


def branch_choice():
    """Options for the branch box: [(value, label, selected)]; empty when there is only one branch."""
    if len(g.branches) < 2:
        return []
    cur = report_branch()
    opts = [(str(b['id']), b['name'], b['id'] == cur) for b in g.branches]
    if g.user['role'] == ADMIN:
        opts.insert(0, ('all', 'All branches', cur is None))
    return opts


def branch_label():
    cur = report_branch()
    if cur is None:
        return 'All branches'
    return next((b['name'] for b in g.branches if b['id'] == cur), '')


# ---------------------------------------------------------------- dashboard
@dash.route('/')
@login_required
def index():
    db = rdb()
    t = date.today().isoformat()
    start = request.args.get('start') or t
    end = request.args.get('end') or t
    d = {
        'products': db.execute('SELECT COUNT(*) FROM products WHERE active=1').fetchone()[0],
        'stock_value': db.execute('SELECT COALESCE(SUM(stock*avg_cost),0) FROM pv WHERE active=1 '
                                  'AND stock>0').fetchone()[0],
        'units': db.execute('SELECT COALESCE(SUM(stock),0) FROM pv WHERE active=1 AND stock>0').fetchone()[0],
        'purchases': db.execute("SELECT COALESCE(SUM(total),0) FROM purchases WHERE status NOT IN "
                                "('Draft','Cancelled') AND date BETWEEN ? AND ?", (start, end)).fetchone()[0],
    }
    p = rules.profit(db, start, end)
    low = db.execute('SELECT id, code, name, unit, stock, min_stock FROM pv WHERE active=1 AND '
                     'min_stock>0 AND in_branch AND stock<=min_stock ORDER BY stock - min_stock LIMIT 15').fetchall()
    best = db.execute("SELECT p.name, p.unit, SUM(i.qty-i.returned_qty) AS q, "
                      "SUM((i.qty-i.returned_qty)*i.price) AS v FROM sale_items i JOIN sales s ON "
                      "s.id=i.sale_id JOIN products p ON p.id=i.product_id WHERE s.status='Completed' "
                      "AND s.date BETWEEN ? AND ? GROUP BY p.id ORDER BY v DESC LIMIT 10",
                      (start, end)).fetchall()
    weak = uses_starter_password(g.user)
    money = can('reports')
    charts = {'trend': sales_trend(db, start, end, money)}
    if money:
        charts['pay'] = donut(db.execute(
            'SELECT sp.method AS k, SUM(sp.amount) AS v FROM sale_payments sp JOIN sales s ON s.id=sp.sale_id '
            "WHERE s.status='Completed' AND sp.date BETWEEN ? AND ? GROUP BY sp.method", (start, end)))
        charts['cats'] = donut(db.execute(
            "SELECT COALESCE(c.name, 'No category') AS k, SUM(p.stock*p.avg_cost) AS v FROM pv p "
            'LEFT JOIN categories c ON c.id=p.category_id WHERE p.active=1 AND p.stock>0 GROUP BY k'))
        top = max(p['net_sales'], p['cogs'], p['expenses'], abs(p['net_profit']), 1)
        charts['flow'] = [(label, v, 100 * abs(v) / top, css) for label, v, css in (
            ('Net sales', p['net_sales'], 's1'), ('Cost of goods sold', p['cogs'], 's2'),
            ('Expenses', p['expenses'], 's4'), ('Net profit', p['net_profit'], 'good' if p['net_profit'] >= 0 else 'bad'))]
    key = 'v' if money else 'q'
    top = max([r[key] for r in best] + [1])
    charts['best'] = [(r, 100 * r[key] / top) for r in best]
    if money and report_branch() is None:  # all branches: how each branch is doing
        charts['branches'] = by_branch(start, end)
    return render_template('dashboard.html', d=d, p=p, low=low, best=best, start=start, end=end, today=t, weak=weak,
                           show_money=money, charts=charts, short=short, branch_opts=branch_choice(),
                           branch_name=branch_label())


def by_branch(start, end):
    """Net sales, gross profit and stock value per branch (all branches)."""
    out = []
    for b in g.branches:
        con = core.report_db(b['id'])
        try:
            p = rules.profit(con, start, end)
            stock = con.execute('SELECT COALESCE(SUM(stock*avg_cost),0) FROM products WHERE active=1 AND stock>0').fetchone()[0]
        finally:
            con.close()
        out.append({'branch': b['name'], 'code': b['code'], 'sales_count': p['sales_count'], 'net_sales': p['net_sales'],
                    'gross_profit': p['gross_profit'], 'expenses': p['expenses'], 'net_profit': p['net_profit'],
                    'stock_value': stock})
    return out


# ---------------------------------------------------------------- dashboard charts (plain SVG/HTML, no library)
def short(v, money=True):
    """Compact axis label: 1234500 cents -> '12.3k'."""
    v = v / 100 if money else v
    for div, suffix in ((1e6, 'M'), (1e3, 'k')):
        if abs(v) >= div:
            return f'{v / div:.1f}'.rstrip('0').rstrip('.') + suffix
    return f'{v:.0f}'


def sales_trend(db, start, end, money):
    """Net sales (or number of sales) per day; the last 14 days when one day is chosen; by month over 62 days."""
    e = date.fromisoformat(end)
    s = date.fromisoformat(start)
    if s >= e:
        s = e - timedelta(days=13)
    monthly = (e - s).days > 62
    fmt = '%Y-%m' if monthly else '%Y-%m-%d'
    keys = []
    d = s
    while d <= e:
        k = d.strftime(fmt)
        if k not in keys:
            keys.append(k)
        d += timedelta(days=1)
    part = 'substr(date,1,7)' if monthly else 'date'
    vals = dict.fromkeys(keys, 0)
    rows = db.execute(f"SELECT {part} AS k, SUM(total) AS v, COUNT(*) AS n FROM sales WHERE status='Completed' "
                      f'AND date BETWEEN ? AND ? GROUP BY k', (s.isoformat(), end))
    for r in rows:
        vals[r['k']] = r['v'] if money else r['n']
    if money:
        for r in db.execute(f"SELECT {part} AS k, SUM(amount) AS v FROM returns WHERE kind='sale' "
                            f'AND date BETWEEN ? AND ? GROUP BY k', (s.isoformat(), end)):
            vals[r['k']] = vals.get(r['k'], 0) - r['v']
    top = max(list(vals.values()) + [0])
    step = _nice(top / 3) if top > 0 else (100 if money else 1)
    ymax = step * 3
    W, H, L, B = 1000, 230, 46, 200          # viewbox width/height, left axis, bottom of plot
    slot = (W - L - 4) / len(keys)
    bw = max(min(slot * .6, 44), 2)
    every = max(1, round(len(keys) / 10))   # at most ~10 date labels
    bars = []
    for i, k in enumerate(keys):
        v = vals[k]
        h = max(v, 0) / ymax * (B - 10)
        x = L + i * slot + (slot - bw) / 2
        day = date.fromisoformat(k + ('-01' if monthly else ''))
        label = day.strftime('%b %Y') if monthly else day.strftime('%a %d %b')
        tick = day.strftime('%b') if monthly else day.strftime('%d/%m')
        bars.append({'x': x, 'y': B - h, 'w': bw, 'h': h, 'v': v, 'label': label,
                     'tick': tick if i % every == 0 else '', 'cx': x + bw / 2})
    grid = [{'y': B - i * (B - 10) / 3, 'label': short(step * i, money)} for i in range(4)]
    return {'bars': bars, 'grid': grid, 'W': W, 'H': H, 'L': L, 'B': B, 'monthly': monthly,
            'total': sum(vals.values()), 'empty': top <= 0}


def _nice(x):
    """Round an axis step up to 1, 2, 2.5 or 5 x 10^n."""
    mag = 10 ** (len(str(int(x))) - 1) if x >= 1 else 1
    for m in (1, 2, 2.5, 5, 10):
        if x <= m * mag:
            return m * mag
    return 10 * mag


def donut(rows):
    """Shares for a ring chart: the 3 biggest plus 'Other' (fixed colour order, never more than 4)."""
    items = sorted(((r['k'], r['v'] or 0) for r in rows if (r['v'] or 0) > 0), key=lambda kv: -kv[1])
    if len(items) > 4:
        items = items[:3] + [('Other', sum(v for _k, v in items[3:]))]
    total = sum(v for _k, v in items)
    out, at = [], 0
    for i, (k, v) in enumerate(items):
        pct = 100 * v / total
        css = 'other' if k == 'Other' and i == 3 else f's{i + 1}'
        gap = 1 if len(items) > 1 else 0    # small white gap between slices
        out.append({'k': k, 'v': v, 'pct': pct, 'dash': max(pct - gap, .1), 'off': 25 - at, 'css': css})
        at += pct
    return {'parts': out, 'total': total}


# ---------------------------------------------------------------- report definitions
# Each report returns (columns, rows). Column = (label, key, kind) with kind text|money|qty|num.
M, Q, T, N = 'money', 'qty', 'text', 'num'
S = 'signed'          # money shown with + / − and green / red (profit, price differences)
MONEY = (M, S)


def _stock(db, f, low=False):
    sql = ('SELECT p.code, p.name, c.name AS cat, p.unit, p.stock, p.min_stock, p.avg_cost, p.price, '
           'p.stock*p.avg_cost AS value FROM pv p LEFT JOIN categories c ON c.id=p.category_id '
           'WHERE p.active=1')
    args = []
    if f['category']:
        sql += ' AND p.category_id=?'
        args.append(f['category'])
    if low:
        sql += ' AND p.min_stock>0 AND p.in_branch AND p.stock<=p.min_stock'
    cols = [('Code', 'code', T), ('Product', 'name', T), ('Category', 'cat', T), ('Unit', 'unit', T),
            ('Stock', 'stock', Q), ('Min', 'min_stock', Q), ('Avg cost', 'avg_cost', M),
            ('Price', 'price', M), ('Stock value', 'value', M)]
    return cols, db.execute(sql + ' ORDER BY p.name', args).fetchall()


def r_stock(db, s, e, f):
    return _stock(db, f)


def r_low(db, s, e, f):
    return _stock(db, f, low=True)


def r_moves(db, s, e, f, types=None):
    sql = ('SELECT m.at, p.code, p.name, m.move_type, m.qty, m.unit_cost, m.qty*m.unit_cost AS value, '
           'm.balance, m.ref_no, m.reason, u.username FROM stock_moves m JOIN products p ON '
           'p.id=m.product_id LEFT JOIN users u ON u.id=m.user_id WHERE date(m.at) BETWEEN ? AND ?')
    args = [s, e]
    if types:
        sql += f" AND m.move_type IN ({','.join('?' * len(types))})"
        args += types
    if f['product']:
        sql += ' AND m.product_id=?'
        args.append(f['product'])
    rows = [dict(r, move_type=rules.MOVE_LABELS.get(r['move_type'], r['move_type']))
            for r in db.execute(sql + ' ORDER BY m.id', args)]
    cols = [('Date/time', 'at', T), ('Code', 'code', T), ('Product', 'name', T), ('Type', 'move_type', T),
            ('Qty', 'qty', Q), ('Unit cost', 'unit_cost', M), ('Value', 'value', M),
            ('Balance', 'balance', Q), ('Reference', 'ref_no', T), ('Reason', 'reason', T),
            ('User', 'username', T)]
    return cols, rows


def r_writeoffs(db, s, e, f):
    return r_moves(db, s, e, f, ['damaged', 'expired', 'lost'])


def r_stocktake(db, s, e, f):
    rows = db.execute("SELECT st.date, 'ST-'||st.id AS ref, p.code, p.name, i.system_qty, i.counted_qty, "
                      "i.counted_qty-i.system_qty AS diff, (i.counted_qty-i.system_qty)*p.avg_cost AS value "
                      "FROM stocktake_items i JOIN stocktakes st ON st.id=i.stocktake_id JOIN products p ON "
                      "p.id=i.product_id WHERE st.date BETWEEN ? AND ? AND i.counted_qty IS NOT NULL AND "
                      "ABS(i.counted_qty-i.system_qty)>0.0005 ORDER BY st.id, p.name", (s, e)).fetchall()
    return [('Date', 'date', T), ('Stocktake', 'ref', T), ('Code', 'code', T), ('Product', 'name', T),
            ('Recorded', 'system_qty', Q), ('Counted', 'counted_qty', Q), ('Difference', 'diff', Q),
            ('Value (approx.)', 'value', M)], rows


def r_sales_period(db, s, e, f):
    fmt = {'day': '%Y-%m-%d', 'week': '%Y-W%W', 'month': '%Y-%m'}.get(f['group'], '%Y-%m-%d')
    rows = db.execute(f"SELECT strftime('{fmt}', date) AS period, COUNT(*) AS n, SUM(subtotal) AS sub, "
                      f"SUM(discount) AS disc, SUM(total) AS total, SUM(refunded) AS refunded, "
                      f"SUM(total-refunded) AS net FROM sales WHERE status='Completed' AND date BETWEEN ? "
                      f"AND ? GROUP BY period ORDER BY period", (s, e)).fetchall()
    return [('Period', 'period', T), ('Sales', 'n', N), ('Subtotal', 'sub', M), ('Discounts', 'disc', M),
            ('Total', 'total', M), ('Returns', 'refunded', M), ('Net sales', 'net', M)], rows


def _sales_group(db, s, e, f, key, label):
    sql = (f"SELECT {key} AS grp, SUM(i.qty-i.returned_qty) AS qty, "
           "SUM((i.qty-i.returned_qty)*i.price*1.0*s.total/s.subtotal) AS revenue, "
           "SUM((i.qty-i.returned_qty)*i.cost) AS cost FROM sale_items i JOIN sales s ON s.id=i.sale_id "
           "JOIN products p ON p.id=i.product_id LEFT JOIN categories c ON c.id=p.category_id "
           "LEFT JOIN users u ON u.id=s.user_id WHERE s.status='Completed' AND s.subtotal>0 "
           "AND s.date BETWEEN ? AND ?")
    args = [s, e]
    if f['category']:
        sql += ' AND p.category_id=?'
        args.append(f['category'])
    if f['product']:
        sql += ' AND p.id=?'
        args.append(f['product'])
    rows = [dict(r, profit=(r['revenue'] or 0) - (r['cost'] or 0))
            for r in db.execute(sql + ' GROUP BY grp ORDER BY revenue DESC', args)]
    return [(label, 'grp', T), ('Qty sold (net)', 'qty', Q), ('Net revenue', 'revenue', M),
            ('Cost of goods', 'cost', M), ('Gross profit', 'profit', S)], rows


def r_sales_product(db, s, e, f):
    return _sales_group(db, s, e, f, "p.code||' - '||p.name", 'Product')


def r_sales_category(db, s, e, f):
    return _sales_group(db, s, e, f, "COALESCE(c.name,'(none)')", 'Category')


def r_sales_user(db, s, e, f):
    return _sales_group(db, s, e, f, 'u.username', 'User')


def r_returns(db, s, e, f):
    rows = db.execute("SELECT r.date, CASE r.kind WHEN 'sale' THEN 'Customer' ELSE 'Supplier' END AS kind, "
                      "COALESCE(sa.receipt_no, pu.ref_no) AS ref, p.name, r.qty, r.amount, r.reason, u.username "
                      "FROM returns r JOIN products p ON p.id=r.product_id LEFT JOIN sales sa ON "
                      "r.kind='sale' AND sa.id=r.doc_id LEFT JOIN purchases pu ON r.kind='purchase' AND "
                      "pu.id=r.doc_id LEFT JOIN users u ON u.id=r.user_id WHERE r.date BETWEEN ? AND ? "
                      "ORDER BY r.id", (s, e)).fetchall()
    return [('Date', 'date', T), ('Type', 'kind', T), ('Document', 'ref', T), ('Product', 'name', T),
            ('Qty', 'qty', Q), ('Amount', 'amount', M), ('Reason', 'reason', T), ('User', 'username', T)], rows


def r_cancelled(db, s, e, f):
    rows = db.execute("SELECT s.date, s.receipt_no, s.total, s.cancel_reason, u.username FROM sales s LEFT "
                      "JOIN users u ON u.id=s.user_id WHERE s.status='Cancelled' AND s.date BETWEEN ? AND ? "
                      "ORDER BY s.id", (s, e)).fetchall()
    return [('Date', 'date', T), ('Receipt', 'receipt_no', T), ('Total', 'total', M),
            ('Reason', 'cancel_reason', T), ('Sold by', 'username', T)], rows


def _purch_where(f):
    sql, args = " AND p.status NOT IN ('Draft','Cancelled')", []
    if f['supplier']:
        sql += ' AND p.supplier_id=?'
        args.append(f['supplier'])
    return sql, args


def r_purch_history(db, s, e, f):
    w, a = _purch_where(f)
    rows = [dict(r, pay=rules.payment_status(r['total'] - r['returned_value'], r['paid']),
                 balance=r['total'] - r['returned_value'] - r['paid'])
            for r in db.execute('SELECT p.*, s.name AS supplier FROM purchases p JOIN suppliers s ON '
                                's.id=p.supplier_id WHERE p.date BETWEEN ? AND ?' + w + ' ORDER BY p.id',
                                [s, e] + a)]
    return [('Date', 'date', T), ('Reference', 'ref_no', T), ('Supplier', 'supplier', T),
            ('Status', 'status', T), ('Total', 'total', M), ('Returned', 'returned_value', M),
            ('Paid', 'paid', M), ('Balance', 'balance', M), ('Payment', 'pay', T)], rows


def r_purch_supplier(db, s, e, f):
    w, a = _purch_where(f)
    rows = db.execute('SELECT s.name AS grp, COUNT(*) AS n, SUM(p.total) AS total, SUM(p.returned_value) AS ret, '
                      'SUM(p.paid) AS paid, SUM(p.total-p.returned_value-p.paid) AS bal FROM purchases p JOIN '
                      'suppliers s ON s.id=p.supplier_id WHERE p.date BETWEEN ? AND ?' + w +
                      ' GROUP BY s.id ORDER BY total DESC', [s, e] + a).fetchall()
    return [('Supplier', 'grp', T), ('Purchases', 'n', N), ('Total', 'total', M), ('Returned', 'ret', M),
            ('Paid', 'paid', M), ('Owed', 'bal', M)], rows


def r_purch_product(db, s, e, f):
    w, a = _purch_where(f)
    sql = ('SELECT pr.code||\' - \'||pr.name AS grp, SUM(i.qty) AS ordered, SUM(i.received_qty) AS received, '
           'SUM(i.returned_qty) AS returned, SUM(i.qty*i.unit_cost) AS value FROM purchase_items i JOIN '
           'purchases p ON p.id=i.purchase_id JOIN products pr ON pr.id=i.product_id WHERE p.date BETWEEN ? '
           'AND ?' + w)
    if f['product']:
        sql += ' AND pr.id=?'
        a = a + [f['product']]
    rows = db.execute(sql + ' GROUP BY pr.id ORDER BY value DESC', [s, e] + a).fetchall()
    return [('Product', 'grp', T), ('Ordered', 'ordered', Q), ('Received', 'received', Q),
            ('Returned', 'returned', Q), ('Value (before discount)', 'value', M)], rows


def r_outstanding(db, s, e, f):
    w, a = _purch_where(f)
    rows = db.execute('SELECT p.date, p.ref_no, s.name AS supplier, s.phone, p.total-p.returned_value AS due, '
                      'p.paid, p.total-p.returned_value-p.paid AS bal FROM purchases p JOIN suppliers s ON '
                      's.id=p.supplier_id WHERE p.total-p.returned_value-p.paid>0' + w + ' ORDER BY p.date',
                      a).fetchall()
    return [('Date', 'date', T), ('Reference', 'ref_no', T), ('Supplier', 'supplier', T), ('Phone', 'phone', T),
            ('Amount due', 'due', M), ('Paid', 'paid', M), ('Balance', 'bal', M)], rows


def r_customer_balances(db, s, e, f):
    rows = db.execute("SELECT c.name, c.phone, COUNT(s.id) AS n, SUM(CASE WHEN s.paid<=0 THEN 1 ELSE 0 END) AS unpaid, "
                      "SUM(s.total-s.refunded) AS due, SUM(s.paid) AS paid, SUM(s.total-s.refunded-s.paid) AS bal, "
                      "MIN(s.date) AS oldest FROM sales s JOIN customers c ON c.id=s.customer_id WHERE s.status='Completed' "
                      "AND s.total-s.refunded-s.paid>0 GROUP BY c.id ORDER BY bal DESC").fetchall()
    return [('Customer', 'name', T), ('Phone', 'phone', T), ('Sales owing', 'n', N), ('of which unpaid', 'unpaid', N),
            ('Amount due', 'due', M), ('Paid so far', 'paid', M), ('Owes', 'bal', M), ('Oldest sale', 'oldest', T)], rows


def r_sales_payment(db, s, e, f):
    rows = [dict(r, pay=rules.payment_status(r['total'] - r['refunded'], r['paid']), bal=r['total'] - r['refunded'] - r['paid'])
            for r in db.execute("SELECT s.date, s.receipt_no, c.name AS customer, s.total, s.refunded, s.paid, u.username, "
                                "(SELECT group_concat(reference, ', ') FROM sale_payments sp WHERE sp.sale_id=s.id AND "
                                "reference<>'') AS refs FROM sales s LEFT JOIN customers c ON c.id=s.customer_id LEFT JOIN "
                                "users u ON u.id=s.user_id WHERE s.status='Completed' AND s.date BETWEEN ? AND ? ORDER BY s.id",
                                (s, e))]
    return [('Date', 'date', T), ('Receipt', 'receipt_no', T), ('Customer', 'customer', T), ('Total', 'total', M),
            ('Returns', 'refunded', M), ('Paid', 'paid', M), ('Balance', 'bal', M), ('Payment', 'pay', T),
            ('TT No. / reference', 'refs', T), ('Sold by', 'username', T)], rows


def r_customer_payments(db, s, e, f):
    rows = db.execute("SELECT sp.date, s.receipt_no, c.name AS customer, sp.method, sp.reference, sp.amount, u.username "
                      "FROM sale_payments sp JOIN sales s ON s.id=sp.sale_id LEFT JOIN customers c ON c.id=s.customer_id "
                      "LEFT JOIN users u ON u.id=sp.user_id WHERE s.status='Completed' AND sp.date BETWEEN ? AND ? "
                      "ORDER BY sp.id", (s, e)).fetchall()
    return [('Date', 'date', T), ('Receipt', 'receipt_no', T), ('Customer', 'customer', T), ('Method', 'method', T),
            ('TT No. / reference', 'reference', T), ('Amount', 'amount', M), ('Received by', 'username', T)], rows


def r_supplier_balances(db, s, e, f):
    w, a = _purch_where(f)
    rows = db.execute('SELECT s.name, s.phone, COUNT(*) AS n, SUM(p.total-p.returned_value) AS due, SUM(p.paid) AS paid, '
                      'SUM(p.total-p.returned_value-p.paid) AS bal, MIN(p.date) AS oldest FROM purchases p JOIN suppliers s '
                      'ON s.id=p.supplier_id WHERE p.total-p.returned_value-p.paid>0' + w +
                      ' GROUP BY s.id ORDER BY bal DESC', a).fetchall()
    return [('Supplier', 'name', T), ('Phone', 'phone', T), ('Purchases owing', 'n', N), ('Amount due', 'due', M),
            ('Paid so far', 'paid', M), ('We owe', 'bal', M), ('Oldest purchase', 'oldest', T)], rows


def r_supplier_payments(db, s, e, f):
    w, a = _purch_where(f)
    rows = db.execute('SELECT pp.date, p.ref_no, s.name AS supplier, pp.method, pp.reference, pp.amount, u.username '
                      'FROM purchase_payments pp JOIN purchases p ON p.id=pp.purchase_id JOIN suppliers s ON '
                      's.id=p.supplier_id LEFT JOIN users u ON u.id=pp.user_id WHERE pp.date BETWEEN ? AND ?' + w +
                      ' ORDER BY pp.id', [s, e] + a).fetchall()
    return [('Date', 'date', T), ('Purchase', 'ref_no', T), ('Supplier', 'supplier', T), ('Method', 'method', T),
            ('TT No. / reference', 'reference', T), ('Amount', 'amount', M), ('By', 'username', T)], rows


def r_batches(db, s, e, f):
    sql = ("SELECT p.code, p.name, b.id AS batch, b.ref_no, b.received, b.expiry, b.qty, b.remaining, b.unit_cost, "
           "b.remaining*b.unit_cost AS value FROM stock_batches b JOIN products p ON p.id=b.product_id "
           "WHERE b.remaining>0.0005")
    args = []
    if f['category']:
        sql += ' AND p.category_id=?'
        args.append(f['category'])
    if f['product']:
        sql += ' AND p.id=?'
        args.append(f['product'])
    rows = db.execute(sql + " ORDER BY p.name, CASE WHEN b.expiry IS NULL OR b.expiry='' THEN 1 ELSE 0 END, b.expiry, "
                      "b.received, b.id", args).fetchall()
    return [('Code', 'code', T), ('Product', 'name', T), ('Batch', 'batch', T), ('From', 'ref_no', T),
            ('Received', 'received', T), ('Expiry', 'expiry', T), ('Qty in', 'qty', Q), ('In stock', 'remaining', Q),
            ('Unit cost', 'unit_cost', M), ('Value', 'value', M)], rows


def r_batch_cogs(db, s, e, f):
    """Cost of goods sold, batch by batch (FIFO): which delivery each sale was taken from."""
    sql = ("SELECT date(m.at) AS day, m.ref_no, p.code, p.name, b.id AS batch, b.ref_no AS bref, b.received, "
           "-bm.qty-bm.back_qty AS qty, bm.unit_cost, (-bm.qty-bm.back_qty)*bm.unit_cost AS cost FROM batch_moves bm "
           "JOIN stock_moves m ON m.id=bm.move_id JOIN stock_batches b ON b.id=bm.batch_id JOIN products p ON "
           "p.id=m.product_id JOIN sales sa ON m.ref_type='sale' AND sa.id=m.ref_id WHERE m.move_type='sale' AND "
           "bm.qty<0 AND sa.status='Completed' AND sa.date BETWEEN ? AND ?")
    args = [s, e]
    if f['product']:
        sql += ' AND p.id=?'
        args.append(f['product'])
    rows = db.execute(sql + ' ORDER BY bm.id', args).fetchall()
    return [('Date', 'day', T), ('Receipt', 'ref_no', T), ('Code', 'code', T), ('Product', 'name', T),
            ('Batch', 'batch', T), ('Batch from', 'bref', T), ('Received', 'received', T), ('Qty (net of returns)', 'qty', Q),
            ('Unit cost', 'unit_cost', M), ('Cost of goods', 'cost', M)], rows


def r_profit(db, s, e, f):
    p = rules.profit(db, s, e)
    rows = [{'item': 'Sales (after discounts)', 'amount': p['gross_sales']},
            {'item': 'Less: customer returns', 'amount': -p['returns']},
            {'item': 'Net sales revenue', 'amount': p['net_sales']},
            {'item': 'Less: cost of goods sold', 'amount': -p['cogs']},
            {'item': 'Gross profit', 'amount': p['gross_profit']},
            {'item': 'Less: operating expenses', 'amount': -p['expenses']},
            {'item': 'Estimated net operating profit', 'amount': p['net_profit']}]
    wo = db.execute("SELECT COALESCE(SUM(-qty*unit_cost),0) FROM stock_moves WHERE move_type IN "
                    "('damaged','expired','lost') AND date(at) BETWEEN ? AND ?", (s, e)).fetchone()[0]
    rows.append({'item': 'Note: stock written off (damaged/expired/lost, not included above)', 'amount': wo})
    return [('Item', 'item', T), ('Amount', 'amount', S)], rows


def r_expenses(db, s, e, f):
    rows = db.execute('SELECT c.name AS grp, COUNT(*) AS n, SUM(e.amount) AS total FROM expenses e JOIN '
                      'expense_categories c ON c.id=e.category_id WHERE e.date BETWEEN ? AND ? GROUP BY c.id '
                      'ORDER BY total DESC', (s, e)).fetchall()
    return [('Category', 'grp', T), ('Entries', 'n', N), ('Total', 'total', M)], rows


def r_expense_list(db, s, e, f):
    rows = db.execute('SELECT e.date, e.ref_no, c.name AS cat, e.description, e.method, e.amount, u.username '
                      'FROM expenses e JOIN expense_categories c ON c.id=e.category_id LEFT JOIN users u ON '
                      'u.id=e.user_id WHERE e.date BETWEEN ? AND ? ORDER BY e.date, e.id', (s, e)).fetchall()
    return [('Date', 'date', T), ('Reference', 'ref_no', T), ('Category', 'cat', T),
            ('Description', 'description', T), ('Method', 'method', T), ('Amount', 'amount', M),
            ('User', 'username', T)], rows


def r_collections(db, s, e, f):
    rows = db.execute("SELECT 'Customer payments' AS dir, sp.method AS method, COUNT(*) AS n, SUM(sp.amount) AS total "
                      "FROM sale_payments sp JOIN sales s ON s.id=sp.sale_id WHERE s.status='Completed' AND "
                      "sp.date BETWEEN ? AND ? GROUP BY sp.method UNION ALL SELECT 'Supplier payments', method, "
                      "COUNT(*), SUM(amount) FROM purchase_payments WHERE date BETWEEN ? AND ? GROUP BY method "
                      "UNION ALL SELECT 'Expenses', method, COUNT(*), SUM(amount) FROM expenses WHERE date "
                      "BETWEEN ? AND ? GROUP BY method", (s, e, s, e, s, e)).fetchall()
    return [('Type', 'dir', T), ('Method', 'method', T), ('Count', 'n', N), ('Total', 'total', M)], rows


def r_branch_sales(db, s, e, f):
    return [('Branch', 'branch', T), ('Sales', 'sales_count', N), ('Net sales', 'net_sales', M),
            ('Gross profit', 'gross_profit', S), ('Expenses', 'expenses', M), ('Est. net profit', 'net_profit', S)], \
        by_branch(s, e)


def r_branch_stock(db, s, e, f):
    rows = []
    for b in g.branches:
        con = core.report_db(b['id'])
        try:
            sql = 'SELECT p.code, p.name, p.unit, p.stock, p.avg_cost, p.stock*p.avg_cost AS value FROM products p ' \
                  'WHERE p.active=1 AND p.stock<>0'
            args = []
            if f['category']:
                sql += ' AND p.category_id=?'
                args.append(f['category'])
            for r in con.execute(sql + ' ORDER BY p.name', args):
                rows.append(dict(r, branch=b['name']))
        finally:
            con.close()
    return [('Branch', 'branch', T), ('Code', 'code', T), ('Product', 'name', T), ('Unit', 'unit', T),
            ('Stock', 'stock', Q), ('Avg cost', 'avg_cost', M), ('Stock value', 'value', M)], rows


# ---------------------------------------------------------------- selling price variations (update section 5)
PRICE_DIFF = 'i.price - COALESCE(i.default_price, i.price)'


def r_price_variation(db, s, e, f):
    """Every sale line sold at another price than the product's default price, and who changed it."""
    sql = (f"SELECT sa.date, sa.receipt_no, sa.id AS sid, p.code, p.name, i.qty, i.default_price, i.price, "
           f"{PRICE_DIFF} AS diff, ({PRICE_DIFF}) * i.qty AS total_diff, CASE WHEN {PRICE_DIFF} > 0 THEN 'Above' "
           f"ELSE 'Below' END AS dir, u.username AS seller, pu.username AS changed_by, "
           f"CASE sa.manual WHEN 1 THEN 'Manual' ELSE 'POS' END AS source FROM sale_items i JOIN sales sa ON "
           f"sa.id=i.sale_id JOIN products p ON p.id=i.product_id LEFT JOIN users u ON u.id=sa.user_id LEFT JOIN users "
           f"pu ON pu.id=i.price_by WHERE sa.status='Completed' AND {PRICE_DIFF} <> 0 AND sa.date BETWEEN ? AND ?")
    args = [s, e]
    if f['product']:
        sql += ' AND p.id=?'
        args.append(f['product'])
    rows = [dict(r, _url=f"/sales/{r['sid']}") for r in db.execute(sql + ' ORDER BY sa.id', args)]
    return [('Date', 'date', T), ('Receipt', 'receipt_no', T), ('Code', 'code', T), ('Product', 'name', T),
            ('Qty', 'qty', Q), ('Default price', 'default_price', M), ('Price sold', 'price', M),
            ('Difference each', 'diff', S), ('Total difference', 'total_diff', S), ('Above / below', 'dir', T),
            ('Seller', 'seller', T), ('Price changed by', 'changed_by', T), ('Recorded', 'source', T)], rows


def r_price_variation_product(db, s, e, f):
    """Per product: sales at default prices against what was actually charged, and the effect on sales."""
    sql = (f"SELECT p.code||' - '||p.name AS grp, SUM(i.qty) AS qty, SUM(CASE WHEN {PRICE_DIFF} > 0 THEN i.qty ELSE 0 END) "
           f"AS above_q, SUM(CASE WHEN {PRICE_DIFF} < 0 THEN i.qty ELSE 0 END) AS below_q, "
           f"SUM(i.qty * COALESCE(i.default_price, i.price)) AS at_default, SUM(i.qty * i.price) AS actual, "
           f"SUM(({PRICE_DIFF}) * i.qty) AS diff FROM sale_items i JOIN sales sa ON sa.id=i.sale_id JOIN products p ON "
           f"p.id=i.product_id WHERE sa.status='Completed' AND sa.date BETWEEN ? AND ?")
    args = [s, e]
    if f['product']:
        sql += ' AND p.id=?'
        args.append(f['product'])
    rows = []
    for r in db.execute(sql + f' GROUP BY p.id HAVING SUM(ABS({PRICE_DIFF})) > 0 ORDER BY diff', args):
        pct = 100 * r['diff'] / r['at_default'] if r['at_default'] else 0
        rows.append(dict(r, pct=f"{'+' if pct > 0 else ('−' if pct < 0 else '')}{abs(pct):.1f}%"))
    return [('Product', 'grp', T), ('Qty sold', 'qty', Q), ('Qty above default', 'above_q', Q),
            ('Qty below default', 'below_q', Q), ('Sales at default prices', 'at_default', M),
            ('Actual sales', 'actual', M), ('Price variation', 'diff', S), ('Effect on sales', 'pct', T)], rows


# ---------------------------------------------------------------- POS and manual sales (update section 6)
def r_sales_source(db, s, e, f):
    rows = db.execute("SELECT CASE manual WHEN 1 THEN 'Manual sales (recorded later)' ELSE 'POS sales' END AS grp, "
                      "COUNT(*) AS n, SUM(total) AS total, SUM(refunded) AS refunded, SUM(total-refunded) AS net, "
                      "SUM(paid) AS paid, SUM(total-refunded-paid) AS owed FROM sales WHERE status='Completed' AND "
                      "date BETWEEN ? AND ? GROUP BY manual ORDER BY manual", (s, e)).fetchall()
    return [('Recorded', 'grp', T), ('Sales', 'n', N), ('Total', 'total', M), ('Returns', 'refunded', M),
            ('Net sales', 'net', M), ('Paid', 'paid', M), ('Owed', 'owed', M)], rows


def r_manual_sales(db, s, e, f):
    rows = [dict(r, _url=f"/sales/{r['id']}") for r in db.execute(
        "SELECT s.id, s.date, s.receipt_no, u.full_name AS seller, en.full_name AS recorder, s.created_at, "
        "CASE s.no_stock WHEN 1 THEN 'Already deducted' ELSE 'Deducted (FIFO)' END AS stock, s.total, s.paid, s.status, "
        "s.notes FROM sales s LEFT JOIN users u ON u.id=s.user_id LEFT JOIN users en ON en.id=s.entered_by "
        "WHERE s.manual=1 AND s.date BETWEEN ? AND ? ORDER BY s.date, s.id", (s, e))]
    return [('Sale date', 'date', T), ('Receipt', 'receipt_no', T), ('Sold by', 'seller', T),
            ('Recorded by', 'recorder', T), ('Recorded on', 'created_at', T), ('Stock', 'stock', T),
            ('Total', 'total', M), ('Paid', 'paid', M), ('Status', 'status', T), ('Notes', 'notes', T)], rows


def r_purchase_returns(db, s, e, f):
    rows = [dict(r, _url=f"/purchases/{r['purchase_id']}") for r in db.execute(
        "SELECT pr.date, pr.ref_no, pr.purchase_id, pu.ref_no AS purchase, sp.name AS supplier, p.name, r.qty, r.amount, "
        "r.reason, u.username FROM returns r JOIN purchase_returns pr ON pr.id=r.return_id JOIN purchases pu ON "
        "pu.id=pr.purchase_id JOIN suppliers sp ON sp.id=pu.supplier_id JOIN products p ON p.id=r.product_id "
        "LEFT JOIN users u ON u.id=r.user_id WHERE pr.date BETWEEN ? AND ? ORDER BY pr.id, r.id", (s, e))]
    return [('Date', 'date', T), ('Return no.', 'ref_no', T), ('Purchase', 'purchase', T), ('Supplier', 'supplier', T),
            ('Product', 'name', T), ('Qty', 'qty', Q), ('Credit', 'amount', M), ('Reason', 'reason', T),
            ('By', 'username', T)], rows


def r_serials(db, s, e, f):
    sql = ("SELECT p.code, p.name, sr.serial, sr.status, b.name AS branch, sa.receipt_no, sr.created_at FROM serials sr "
           "JOIN products p ON p.id=sr.product_id LEFT JOIN branches b ON b.id=sr.branch_id LEFT JOIN sales sa ON "
           "sa.id=sr.sale_id WHERE 1=1")
    args = []
    if f['product']:
        sql += ' AND p.id=?'
        args.append(f['product'])
    rows = db.execute(sql + ' ORDER BY p.name, sr.serial', args).fetchall()
    return [('Code', 'code', T), ('Product', 'name', T), ('Serial number', 'serial', T), ('Status', 'status', T),
            ('Branch', 'branch', T), ('Sold on', 'receipt_no', T), ('Registered', 'created_at', T)], rows


REPORTS = {
    'Inventory': [('stock', 'Current stock and valuation', r_stock), ('low', 'Low stock', r_low),
                  ('batches', 'Stock batches (FIFO) and expiry', r_batches),
                  ('serials', 'Serial numbers and their status', r_serials),
                  ('moves', 'Stock movements', r_moves),
                  ('writeoffs', 'Damaged / expired / lost', r_writeoffs),
                  ('stocktake', 'Stocktaking differences', r_stocktake)],
    'Sales': [('sales_period', 'Sales by day / week / month', r_sales_period),
              ('sales_product', 'Sales by product', r_sales_product),
              ('sales_category', 'Sales by category', r_sales_category),
              ('sales_user', 'Sales by user', r_sales_user), ('returns', 'Returns', r_returns),
              ('cancelled', 'Cancelled sales', r_cancelled),
              ('sales_payment', 'Sales by payment status (paid / partly / unpaid)', r_sales_payment),
              ('customer_payments', 'Customer payments (with TT No.)', r_customer_payments),
              ('customer_balances', 'Outstanding customer balances', r_customer_balances),
              ('batch_cogs', 'Cost of goods sold by batch (FIFO)', r_batch_cogs),
              ('price_variation', 'Sold above / below the default price', r_price_variation),
              ('price_variation_product', 'Price variation by product', r_price_variation_product),
              ('sales_source', 'POS sales and manual sales', r_sales_source),
              ('manual_sales', 'Manual sales (seller and who recorded them)', r_manual_sales)],
    'Purchases': [('purch_history', 'Purchase history', r_purch_history),
                  ('purch_supplier', 'Purchases by supplier', r_purch_supplier),
                  ('purch_product', 'Purchases by product', r_purch_product),
                  ('outstanding', 'Outstanding supplier payments', r_outstanding),
                  ('supplier_balances', 'Outstanding supplier balances', r_supplier_balances),
                  ('supplier_payments', 'Supplier payments (with TT No.)', r_supplier_payments),
                  ('purchase_returns', 'Returns to suppliers', r_purchase_returns)],
    'Financial': [('profit', 'Profit summary', r_profit), ('expenses', 'Expenses by category', r_expenses),
                  ('expense_list', 'Expense list', r_expense_list),
                  ('collections', 'Payment collection', r_collections)],
    'Branches': [('branch_sales', 'Sales and profit by branch', r_branch_sales),
                 ('branch_stock', 'Stock by branch', r_branch_stock)],
}
BY_BRANCH = {'branch_sales', 'branch_stock'}  # these compare all branches; the others follow the branch box
ALL = {k: (title, fn) for group in REPORTS.values() for k, title, fn in group}


def totals(cols, rows):
    """Sum money/qty columns, except balances and per-unit values."""
    skip = {'balance', 'avg_cost', 'price', 'unit_cost', 'min_stock', 'system_qty', 'counted_qty', 'batch'}
    out = {}
    for _l, k, kind in cols:
        if kind in (M, S, Q, N) and k not in skip:
            out[k] = sum((r[k] or 0) for r in rows)
    return out


@bp.route('/')
@requires('reports')
def index():
    db = rdb()
    key = request.args.get('r', 'stock')
    if key not in ALL or (key in BY_BRANCH and g.user['role'] != ADMIN):
        key = 'stock'
    start, end = date_range()
    f = {'category': request.args.get('category', type=int), 'product': request.args.get('product', type=int),
         'supplier': request.args.get('supplier', type=int), 'group': request.args.get('group', 'day')}
    title, fn = ALL[key]
    cols, rows = fn(db, start, end, f)
    tot = totals(cols, rows) if key != 'profit' else {}
    where = 'All branches' if key in BY_BRANCH else branch_label()
    if request.args.get('format') == 'xlsx':
        return xlsx_response(title, start, end, cols, rows, tot, where)
    lists = {
        'cats': db.execute('SELECT id, name FROM categories ORDER BY name').fetchall(),
        'products': db.execute('SELECT id, code, name FROM products ORDER BY name').fetchall(),
        'sups': db.execute('SELECT id, name FROM suppliers ORDER BY name').fetchall(),
    }
    return render_template('reports.html', groups=REPORTS, key=key, title=title, cols=cols, rows=rows,
                           tot=tot, start=start, end=end, f=f, branch_opts=[] if key in BY_BRANCH else branch_choice(),
                           where=where if len(g.branches) > 1 else '', by_branch=BY_BRANCH,
                           show_branch_reports=g.user['role'] == ADMIN and len(g.branches) > 1, **lists)


# ---------------------------------------------------------------- daily transaction report (update section 8)
@bp.route('/daily')
@requires('reports')
def daily():
    """Everything that happened on a day (or a few days): sales, purchases, stock, expenses and the money summary.
    Seller, POS / manual and payment method filter the sales part. Excel and Print / PDF like the other reports."""
    db = rdb()
    t = date.today().isoformat()
    start = request.args.get('start') or request.args.get('date') or t
    end = request.args.get('end') or start
    if end < start:
        start, end = end, start
    fl = {'seller': request.args.get('seller', type=int), 'type': request.args.get('type', ''),
          'method': request.args.get('method', '')}
    blocks = daily_blocks(db, start, end, fl)
    title = 'Daily transaction report'
    where = branch_label()
    if request.args.get('format') == 'xlsx':
        sub = f"{setting('shop_name')} | {where} | {start}{' to ' + end if end != start else ''} | amounts in {setting('currency')}"
        data = xlsx_blocks(title, sub, [(f'{sec} - {h}' if h else sec, c, r, tt) for sec, h, c, r, tt in blocks],
                           sheet_name='Daily report')
        return Response(data, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                        headers={'Content-Disposition': f'attachment; filename="daily-report-{start}-{end}.xlsx"'})
    sellers = db.execute('SELECT id, full_name FROM users ORDER BY full_name').fetchall()
    sections = []
    for sec, h, c, r, tt in blocks:
        if not sections or sections[-1][0] != sec:
            sections.append((sec, []))
        sections[-1][1].append((h, c, r, tt))
    return render_template('daily.html', sections=sections, start=start, end=end, fl=fl, sellers=sellers,
                           methods=core.payment_methods(), branch_opts=branch_choice(),
                           where=where if len(g.branches) > 1 else '')


def daily_blocks(db, s, e, fl):
    """[(section, heading, cols, rows, totals)] for the daily report."""
    out = []
    # ---- 8.1 sales
    w, a = " WHERE s.status='Completed' AND s.date BETWEEN ? AND ?", [s, e]
    if fl['seller']:
        w += ' AND s.user_id=?'
        a.append(fl['seller'])
    if fl['type'] in ('pos', 'manual'):
        w += ' AND s.manual=?'
        a.append(1 if fl['type'] == 'manual' else 0)
    if fl['method']:
        w += ' AND s.method=?'
        a.append(fl['method'])
    sales = [dict(r) for r in db.execute(
        "SELECT s.*, u.full_name AS seller, c.name AS customer, (SELECT group_concat(reference, ', ') FROM sale_payments sp "
        "WHERE sp.sale_id=s.id AND reference<>'') AS refs, (SELECT COALESCE(SUM((i.price-COALESCE(i.default_price, i.price))"
        "*i.qty),0) FROM sale_items i WHERE i.sale_id=s.id) AS pvar FROM sales s LEFT JOIN users u ON u.id=s.user_id "
        "LEFT JOIN customers c ON c.id=s.customer_id" + w + ' ORDER BY s.id', a)]
    for r in sales:
        r['net'] = r['total'] - r['refunded']
        r['owed'] = max(0, r['net'] - r['paid'])
        r['pay'] = rules.payment_status(r['net'], r['paid'])
        r['source'] = 'Manual' if r['manual'] else 'POS'
        r['_url'] = f"/sales/{r['id']}"

    def line(item, rows, amount='net'):
        return {'item': item, 'n': len(rows), 'amount': sum(r[amount] for r in rows)}
    up = sum(r['pvar'] for r in sales if r['pvar'] > 0)
    down = sum(r['pvar'] for r in sales if r['pvar'] < 0)
    summary = [line('Total sales', sales), line('Cash sales', [r for r in sales if r['method'].lower() == 'cash']),
               line('Bank and mobile banking sales', [r for r in sales if r['method'].lower() != 'cash'])]
    for m in sorted({r['method'] for r in sales}):
        summary.append(line(f'· paid by {m}', [r for r in sales if r['method'] == m]))
    summary += [line('Fully paid sales', [r for r in sales if r['pay'] == 'Paid']),
                line('Partially paid sales', [r for r in sales if r['pay'] == 'Partially paid']),
                line('Unpaid sales', [r for r in sales if r['pay'] == 'Unpaid']),
                line('Still owed on these sales', [r for r in sales if r['owed'] > 0], 'owed'),
                line('POS sales', [r for r in sales if not r['manual']]),
                line('Manually recorded sales', [r for r in sales if r['manual']])]
    out.append(('Sales', 'Summary', [('Item', 'item', T), ('Count', 'n', N), ('Amount', 'amount', M)], summary, {}))
    out.append(('Sales', 'Selling price variations',
                [('Item', 'item', T), ('Amount', 'amount', S)],
                [{'item': 'Sold above the default price', 'amount': up},
                 {'item': 'Sold below the default price', 'amount': down},
                 {'item': 'Total price variation', 'amount': up + down}], {}))
    by = {}
    for r in sales:
        b = by.setdefault(r['seller'] or '?', {'grp': r['seller'] or '?', 'n': 0, 'pos': 0, 'manual': 0, 'net': 0,
                                                'paid': 0, 'pvar': 0})
        b['n'] += 1
        b['manual' if r['manual'] else 'pos'] += 1
        b['net'] += r['net']
        b['paid'] += r['paid']
        b['pvar'] += r['pvar']
    seller_cols = [('Seller', 'grp', T), ('Sales', 'n', N), ('POS', 'pos', N), ('Manual', 'manual', N),
                   ('Net sales', 'net', M), ('Paid', 'paid', M), ('Price variation', 'pvar', S)]
    rows = sorted(by.values(), key=lambda x: -x['net'])
    out.append(('Sales', 'Sales by seller', seller_cols, rows, totals(seller_cols, rows)))
    sale_cols = [('Receipt', 'receipt_no', T), ('Recorded', 'source', T), ('Seller', 'seller', T),
                 ('Customer', 'customer', T), ('Method', 'method', T), ('TT No. / ref.', 'refs', T),
                 ('Net total', 'net', M), ('Paid', 'paid', M), ('Owed', 'owed', M), ('Payment', 'pay', T),
                 ('Price variation', 'pvar', S)]
    out.append(('Sales', 'Sales transactions', sale_cols, sales, totals(sale_cols, sales)))

    # ---- 8.2 purchases
    purch = [dict(r, _url=f"/purchases/{r['id']}", bal=r['total'] - r['returned_value'] - r['paid'])
             for r in db.execute("SELECT p.*, sp.name AS supplier FROM purchases p JOIN suppliers sp ON sp.id=p.supplier_id "
                                 "WHERE p.status NOT IN ('Draft','Cancelled') AND p.date BETWEEN ? AND ? ORDER BY p.id",
                                 (s, e))]
    pret = db.execute('SELECT COUNT(DISTINCT r.return_id) AS n, COUNT(*) AS lines, COALESCE(SUM(r.qty),0) AS q, '
                      "COALESCE(SUM(r.amount),0) AS v FROM returns r WHERE r.kind='purchase' AND r.date BETWEEN ? AND ?",
                      (s, e)).fetchone()
    paid_sup = db.execute('SELECT COALESCE(SUM(amount),0) FROM purchase_payments WHERE date BETWEEN ? AND ?',
                          (s, e)).fetchone()[0]
    owe_sup = db.execute("SELECT COALESCE(SUM(total-returned_value-paid),0) FROM purchases WHERE status NOT IN "
                         "('Draft','Cancelled') AND total-returned_value-paid>0").fetchone()[0]
    out.append(('Purchases', 'Summary', [('Item', 'item', T), ('Count', 'n', N), ('Amount', 'amount', M)], [
        {'item': 'Purchase transactions (confirmed)', 'n': len(purch), 'amount': sum(r['total'] for r in purch)},
        {'item': 'Returns to suppliers', 'n': pret['lines'], 'amount': pret['v']},
        {'item': 'Paid to suppliers', 'n': None, 'amount': paid_sup},
        {'item': 'Outstanding supplier balances (all, now)', 'n': None, 'amount': owe_sup}], {}))
    pp_cols = [('Product', 'grp', T), ('Qty', 'qty', Q), ('Value (after discount)', 'value', M)]
    pp = db.execute("SELECT pr.code||' - '||pr.name AS grp, SUM(i.qty) AS qty, SUM(i.qty*i.unit_cost*1.0*"
                    "CASE WHEN g.gross>0 THEN p.total*1.0/g.gross ELSE 1 END) AS value FROM purchase_items i JOIN purchases p "
                    "ON p.id=i.purchase_id JOIN products pr ON pr.id=i.product_id JOIN (SELECT purchase_id, SUM(qty*unit_cost) "
                    "AS gross FROM purchase_items GROUP BY purchase_id) g ON g.purchase_id=p.id WHERE p.status NOT IN "
                    "('Draft','Cancelled') AND p.date BETWEEN ? AND ? GROUP BY pr.id ORDER BY value DESC", (s, e)).fetchall()
    out.append(('Purchases', 'Products purchased', pp_cols, pp, totals(pp_cols, pp)))
    pcols = [('Reference', 'ref_no', T), ('Supplier', 'supplier', T), ('Total', 'total', M),
             ('Returned', 'returned_value', M), ('Paid', 'paid', M), ('Balance', 'bal', M)]
    out.append(('Purchases', 'Purchase transactions', pcols, purch, totals(pcols, purch)))
    rc, rr = r_purchase_returns(db, s, e, {})
    if rr:
        out.append(('Purchases', 'Returns to suppliers', rc, rr, totals(rc, rr)))

    # ---- 8.3 inventory
    def q(types, sign=1):
        return f"SUM(CASE WHEN m.move_type IN ({','.join(repr(x) for x in types)}) THEN {sign}*m.qty ELSE 0 END)"
    inv = [dict(r) for r in db.execute(
        f"SELECT p.code||' - '||p.name AS grp, {q(('purchase', 'opening', 'transfer_in'))} AS received, "
        f"{q(('sale',), -1)} AS sold, {q(('sale_return', 'sale_cancel'))} AS cust_ret, "
        f"{q(('purchase_return',), -1)} AS sup_ret, {q(('damaged', 'expired', 'lost'), -1)} AS written_off, "
        f"{q(('correction', 'stocktake', 'transfer_out', 'transfer_cancel'))} AS adjusted, p.stock FROM stock_moves m "
        "JOIN products p ON p.id=m.product_id WHERE date(m.at) BETWEEN ? AND ? AND m.move_type<>'batch_open' "
        'GROUP BY p.id ORDER BY p.name', (s, e))]
    inv_cols = [('Product', 'grp', T), ('Received', 'received', Q), ('Sold', 'sold', Q),
                ('Customer returns / cancels', 'cust_ret', Q), ('Returned to supplier', 'sup_ret', Q),
                ('Damaged / expired / lost', 'written_off', Q), ('Other adjustments (+/−)', 'adjusted', Q),
                ('Stock now', 'stock', Q)]
    stock_now = db.execute('SELECT COALESCE(SUM(stock),0) AS q, COALESCE(SUM(stock*avg_cost),0) AS v FROM products '
                           'WHERE active=1 AND stock>0').fetchone()
    tot = totals(inv_cols, inv)
    out.append(('Inventory', 'Summary', [('Item', 'item', T), ('Quantity', 'q', Q), ('Value', 'v', M)], [
        {'item': 'Stock received', 'q': tot.get('received', 0), 'v': None},
        {'item': 'Stock sold', 'q': tot.get('sold', 0), 'v': None},
        {'item': 'Stock returned by customers / cancelled sales', 'q': tot.get('cust_ret', 0), 'v': None},
        {'item': 'Stock returned to suppliers', 'q': tot.get('sup_ret', 0), 'v': None},
        {'item': 'Damaged, expired and lost', 'q': tot.get('written_off', 0), 'v': db.execute(
            "SELECT COALESCE(SUM(-qty*unit_cost),0) FROM stock_moves WHERE move_type IN ('damaged','expired','lost') "
            'AND date(at) BETWEEN ? AND ?', (s, e)).fetchone()[0]},
        {'item': 'Stock adjustments, stocktakes and transfers (+/−)', 'q': tot.get('adjusted', 0), 'v': None},
        {'item': 'Remaining stock now (all products)', 'q': stock_now['q'], 'v': stock_now['v']}], {}))
    out.append(('Inventory', 'Stock movement by product', inv_cols, inv, {k: v for k, v in tot.items() if k != 'stock'}))

    # ---- 8.4 expenses and financial summary
    p = rules.profit(db, s, e)
    owe_cust = db.execute("SELECT COALESCE(SUM(total-refunded-paid),0) FROM sales WHERE status='Completed' AND "
                          'total-refunded-paid>0').fetchone()[0]
    got = db.execute("SELECT COALESCE(SUM(sp.amount),0) FROM sale_payments sp JOIN sales sa ON sa.id=sp.sale_id WHERE "
                     "sa.status='Completed' AND sp.date BETWEEN ? AND ?", (s, e)).fetchone()[0]
    out.append(('Expenses and financial summary', 'Summary', [('Item', 'item', T), ('Amount', 'amount', S)], [
        {'item': 'Total sales revenue (after discounts and returns)', 'amount': p['net_sales']},
        {'item': 'Cost of goods sold (FIFO batches)', 'amount': -p['cogs']},
        {'item': 'Gross profit', 'amount': p['gross_profit']},
        {'item': 'Total expenses', 'amount': -p['expenses']},
        {'item': 'Estimated net operating profit', 'amount': p['net_profit']},
        {'item': 'Money received from customers', 'amount': got},
        {'item': 'Outstanding customer balances (all, now)', 'amount': owe_cust},
        {'item': 'Outstanding supplier balances (all, now)', 'amount': -owe_sup}], {}))
    ec, er = r_expense_list(db, s, e, {})
    out.append(('Expenses and financial summary', 'Expenses', ec, er, totals(ec, er)))
    return out


# ---------------------------------------------------------------- minimal .xlsx writer
def _col(i):
    s = ''
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _cell(ref, v, style=0):
    if v is None or v == '':
        return ''
    st = f' s="{style}"' if style else ''
    if isinstance(v, (int, float)):
        return f'<c r="{ref}"{st}><v>{v}</v></c>'
    return f'<c r="{ref}" t="inlineStr"{st}><is><t xml:space="preserve">{escape(str(v))}</t></is></c>'


def xlsx_bytes(title, subtitle, cols, rows, tot):
    return xlsx_blocks(title, subtitle, [(None, cols, rows, tot)])


def xlsx_blocks(title, subtitle, blocks, sheet_name='Report'):
    """One worksheet with a title and one or more tables: blocks = [(heading or None, cols, rows, totals)]."""
    lines = []
    r = 1

    def row(vals, style_for=lambda i: 0):
        nonlocal r
        cells = ''.join(_cell(f'{_col(i)}{r}', v, style_for(i)) for i, v in enumerate(vals))
        lines.append(f'<row r="{r}">{cells}</row>')
        r += 1

    def conv(v, kind):
        if v is None:
            return None
        if kind in MONEY:
            return round((v or 0) / 100, 2)
        if kind == Q:
            return round(v or 0, 3)
        return v

    if title:
        row([title], lambda i: 1)
    if subtitle:
        row([subtitle])
    if title or subtitle:
        r += 1
    widest = {}
    for heading, cols, rows, tot in blocks:
        if heading:
            row([heading], lambda i: 1)
        row([c[0] for c in cols], lambda i: 1)
        for rr in rows:
            row([conv(rr[k], kind) for _l, k, kind in cols], lambda i, cols=cols: 2 if cols[i][2] in MONEY else 0)
        if tot:
            row(['Total'] + [conv(tot[k], kind) if k in tot else '' for _l, k, kind in cols[1:]],
                lambda i, cols=cols: 3 if cols[i][2] in MONEY else 1)
        if len(blocks) > 1:
            r += 1
        for i, c in enumerate(cols):
            widest[i] = max(widest.get(i, 0), len(c[0]))
    widths = ''.join(f'<col min="{i + 1}" max="{i + 1}" width="{min(45, max(10, w + 4))}" customWidth="1"/>'
                     for i, w in sorted(widest.items()))
    sheet = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
             '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             f'<cols>{widths}</cols><sheetData>{"".join(lines)}</sheetData></worksheet>')
    styles = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
              '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
              '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font>'
              '<font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
              '<fills count="2"><fill><patternFill patternType="none"/></fill>'
              '<fill><patternFill patternType="gray125"/></fill></fills>'
              '<borders count="1"><border/></borders><cellStyleXfs count="1"><xf/></cellStyleXfs>'
              '<cellXfs count="4"><xf/><xf fontId="1" applyFont="1"/>'
              '<xf numFmtId="4" applyNumberFormat="1"/><xf numFmtId="4" fontId="1" applyNumberFormat="1" applyFont="1"/>'
              '</cellXfs></styleSheet>')
    files = {
        '[Content_Types].xml': '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        '</Types>',
        '_rels/.rels': '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '</Relationships>',
        'xl/workbook.xml': '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{escape(sheet_name)}" sheetId="1" r:id="rId1"/></sheets></workbook>',
        'xl/_rels/workbook.xml.rels': '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        '</Relationships>',
        'xl/worksheets/sheet1.xml': sheet,
        'xl/styles.xml': styles,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def xlsx_response(title, start, end, cols, rows, tot, where=''):
    sub = f"{setting('shop_name')}{' | ' + where if where else ''} | {start} to {end} | amounts in {setting('currency')}"
    data = xlsx_bytes(title, sub, cols, rows, tot)
    fname = title.lower().replace(' ', '-').replace('/', '-') + f'-{start}-{end}.xlsx'
    return Response(data, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': f'attachment; filename="{fname}"'})
