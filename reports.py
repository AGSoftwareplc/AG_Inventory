"""Dashboard and reports (on screen, print / PDF, and Excel export)."""
import io
import zipfile
from datetime import date
from xml.sax.saxutils import escape

from flask import Blueprint, Response, render_template, request

from core import can, date_range, get_db, login_required, requires, setting
import rules

dash = Blueprint('dash', __name__)
bp = Blueprint('reports', __name__, url_prefix='/reports')


# ---------------------------------------------------------------- dashboard
@dash.route('/')
@login_required
def index():
    db = get_db()
    t = date.today().isoformat()
    start = request.args.get('start') or t
    end = request.args.get('end') or t
    d = {
        'products': db.execute('SELECT COUNT(*) FROM products WHERE active=1').fetchone()[0],
        'stock_value': db.execute('SELECT COALESCE(SUM(stock*avg_cost),0) FROM products WHERE active=1 '
                                  'AND stock>0').fetchone()[0],
        'units': db.execute('SELECT COALESCE(SUM(stock),0) FROM products WHERE active=1 AND stock>0').fetchone()[0],
        'purchases': db.execute("SELECT COALESCE(SUM(total),0) FROM purchases WHERE status NOT IN "
                                "('Draft','Cancelled') AND date BETWEEN ? AND ?", (start, end)).fetchone()[0],
    }
    p = rules.profit(db, start, end)
    low = db.execute('SELECT id, code, name, unit, stock, min_stock FROM products WHERE active=1 AND '
                     'min_stock>0 AND stock<=min_stock ORDER BY stock - min_stock LIMIT 15').fetchall()
    best = db.execute("SELECT p.name, p.unit, SUM(i.qty-i.returned_qty) AS q, "
                      "SUM((i.qty-i.returned_qty)*i.price) AS v FROM sale_items i JOIN sales s ON "
                      "s.id=i.sale_id JOIN products p ON p.id=i.product_id WHERE s.status='Completed' "
                      "AND s.date BETWEEN ? AND ? GROUP BY p.id ORDER BY v DESC LIMIT 10",
                      (start, end)).fetchall()
    return render_template('dashboard.html', d=d, p=p, low=low, best=best, start=start, end=end, today=t,
                           show_money=can('reports'))


# ---------------------------------------------------------------- report definitions
# Each report returns (columns, rows). Column = (label, key, kind) with kind text|money|qty|num.
M, Q, T, N = 'money', 'qty', 'text', 'num'


def _stock(db, f, low=False):
    sql = ('SELECT p.code, p.name, c.name AS cat, p.unit, p.stock, p.min_stock, p.avg_cost, p.price, '
           'p.stock*p.avg_cost AS value FROM products p LEFT JOIN categories c ON c.id=p.category_id '
           'WHERE p.active=1')
    args = []
    if f['category']:
        sql += ' AND p.category_id=?'
        args.append(f['category'])
    if low:
        sql += ' AND p.min_stock>0 AND p.stock<=p.min_stock'
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
            ('Cost of goods', 'cost', M), ('Gross profit', 'profit', M)], rows


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
    rows = db.execute("SELECT c.name, c.phone, COUNT(s.id) AS n, SUM(s.total-s.refunded-s.paid) AS bal FROM "
                      "sales s JOIN customers c ON c.id=s.customer_id WHERE s.status='Completed' AND "
                      "s.total-s.refunded-s.paid>0 GROUP BY c.id ORDER BY bal DESC").fetchall()
    return [('Customer', 'name', T), ('Phone', 'phone', T), ('Unpaid sales', 'n', N), ('Owes', 'bal', M)], rows


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
    return [('Item', 'item', T), ('Amount', 'amount', M)], rows


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


REPORTS = {
    'Inventory': [('stock', 'Current stock and valuation', r_stock), ('low', 'Low stock', r_low),
                  ('moves', 'Stock movements', r_moves),
                  ('writeoffs', 'Damaged / expired / lost', r_writeoffs),
                  ('stocktake', 'Stocktaking differences', r_stocktake)],
    'Sales': [('sales_period', 'Sales by day / week / month', r_sales_period),
              ('sales_product', 'Sales by product', r_sales_product),
              ('sales_category', 'Sales by category', r_sales_category),
              ('sales_user', 'Sales by user', r_sales_user), ('returns', 'Returns', r_returns),
              ('cancelled', 'Cancelled sales', r_cancelled),
              ('customer_balances', 'Customer balances (credit)', r_customer_balances)],
    'Purchases': [('purch_history', 'Purchase history', r_purch_history),
                  ('purch_supplier', 'Purchases by supplier', r_purch_supplier),
                  ('purch_product', 'Purchases by product', r_purch_product),
                  ('outstanding', 'Outstanding supplier payments', r_outstanding)],
    'Financial': [('profit', 'Profit summary', r_profit), ('expenses', 'Expenses by category', r_expenses),
                  ('expense_list', 'Expense list', r_expense_list),
                  ('collections', 'Payment collection', r_collections)],
}
ALL = {k: (title, fn) for group in REPORTS.values() for k, title, fn in group}


def totals(cols, rows):
    """Sum money/qty columns, except balances and per-unit values."""
    skip = {'balance', 'avg_cost', 'price', 'unit_cost', 'min_stock', 'system_qty', 'counted_qty'}
    out = {}
    for _l, k, kind in cols:
        if kind in (M, Q, N) and k not in skip:
            out[k] = sum((r[k] or 0) for r in rows)
    return out


@bp.route('/')
@requires('reports')
def index():
    db = get_db()
    key = request.args.get('r', 'stock')
    if key not in ALL:
        key = 'stock'
    start, end = date_range()
    f = {'category': request.args.get('category', type=int), 'product': request.args.get('product', type=int),
         'supplier': request.args.get('supplier', type=int), 'group': request.args.get('group', 'day')}
    title, fn = ALL[key]
    cols, rows = fn(db, start, end, f)
    tot = totals(cols, rows) if key != 'profit' else {}
    if request.args.get('format') == 'xlsx':
        return xlsx_response(title, start, end, cols, rows, tot)
    lists = {
        'cats': db.execute('SELECT id, name FROM categories ORDER BY name').fetchall(),
        'products': db.execute('SELECT id, code, name FROM products ORDER BY name').fetchall(),
        'sups': db.execute('SELECT id, name FROM suppliers ORDER BY name').fetchall(),
    }
    return render_template('reports.html', groups=REPORTS, key=key, title=title, cols=cols, rows=rows,
                           tot=tot, start=start, end=end, f=f, **lists)


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
    lines = []
    r = 1

    def row(vals, style_for=lambda i: 0):
        nonlocal r
        cells = ''.join(_cell(f'{_col(i)}{r}', v, style_for(i)) for i, v in enumerate(vals))
        lines.append(f'<row r="{r}">{cells}</row>')
        r += 1

    def conv(v, kind):
        if kind == M:
            return round((v or 0) / 100, 2)
        if kind == Q:
            return round(v or 0, 3)
        return v

    row([title], lambda i: 1)
    row([subtitle])
    r += 1
    row([c[0] for c in cols], lambda i: 1)
    for rr in rows:
        row([conv(rr[k], kind) for _l, k, kind in cols], lambda i: 2 if cols[i][2] == M else 0)
    if tot:
        row(['Total'] + [conv(tot[k], kind) if k in tot else '' for _l, k, kind in cols[1:]],
            lambda i: 3 if cols[i][2] == M else 1)
    widths = ''.join(f'<col min="{i + 1}" max="{i + 1}" width="{min(45, max(10, len(c[0]) + 4))}" customWidth="1"/>'
                     for i, c in enumerate(cols))
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
        '<sheets><sheet name="Report" sheetId="1" r:id="rId1"/></sheets></workbook>',
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


def xlsx_response(title, start, end, cols, rows, tot):
    sub = f"{setting('shop_name')} | {start} to {end} | amounts in {setting('currency')}"
    data = xlsx_bytes(title, sub, cols, rows, tot)
    fname = title.lower().replace(' ', '-').replace('/', '-') + f'-{start}-{end}.xlsx'
    return Response(data, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': f'attachment; filename="{fname}"'})
