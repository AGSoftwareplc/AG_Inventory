"""Products and categories."""
import sqlite3

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from core import RuleError, audit, branch_id, claim_once, fail, get_db, parse_money, parse_qty, requires, tx
import core
import rules
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
        'serial_tracked': 1 if str(f.get('serial_tracked', '')).strip().lower() in ('1', 'yes', 'y', 'true', 'on') else 0,
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
                     'price, avg_cost, min_stock, serial_tracked) VALUES (?,?,?,?,?,?,?,?,?,?)',
                     (v['code'], v['name'], v['category_id'], v['description'], v['unit'],
                      v['barcode'], v['price'], v['cost'], v['min_stock'], v.get('serial_tracked', 0))).lastrowid
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
                               'unit=?, barcode=?, price=?, min_stock=?, active=?, serial_tracked=? WHERE id=?',
                               (v['code'], v['name'], v['category_id'], v['description'], v['unit'],
                                v['barcode'], v['price'], v['min_stock'],
                                1 if request.form.get('active') else 0, v['serial_tracked'], pid))
                    if v['serial_tracked'] != p['serial_tracked']:
                        audit('product_serials', f"{v['code']}: serial numbers {'on' if v['serial_tracked'] else 'off'}", db)
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
    """Everything about one product: details, stock and batches, serial numbers, purchases, sales and movements."""
    db = get_db()
    p = db.execute('SELECT p.*, c.name AS cat FROM pv p LEFT JOIN categories c '
                   'ON c.id=p.category_id WHERE p.id=?', (pid,)).fetchone()
    if not p:
        return fail('Product not found.', 'products.index')
    tab = request.args.get('tab', 'sales')
    limit = 2000 if request.args.get('all') else 100
    b = branch_id()
    moves = db.execute('SELECT m.*, u.username FROM stock_moves m LEFT JOIN users u ON u.id=m.user_id '
                       f'WHERE product_id=? AND m.branch_id=? ORDER BY m.id DESC LIMIT {limit}',
                       (pid, b)).fetchall()
    prices = db.execute('SELECT pc.*, u.username FROM price_changes pc LEFT JOIN users u '
                        'ON u.id=pc.user_id WHERE product_id=? ORDER BY pc.id DESC LIMIT 20',
                        (pid,)).fetchall()
    batches = rules.open_batches(db, pid, b)
    # every item sold: date, receipt, qty, price (and default), seller, payment reference and the batches used
    sales = db.execute(
        "SELECT i.*, s.id AS sid, s.receipt_no, s.date, s.status, s.manual, s.created_at, u.username AS seller, "
        "c.name AS customer, (SELECT group_concat(reference, ', ') FROM sale_payments sp WHERE sp.sale_id=s.id AND "
        "reference<>'') AS refs, (SELECT group_concat('#' || bm.batch_id || ' ' || b.ref_no || ' × ' || printf('%g', -bm.qty), '; ') "
        "FROM stock_moves m JOIN batch_moves bm ON bm.move_id=m.id JOIN stock_batches b ON b.id=bm.batch_id "
        "WHERE m.ref_type='sale' AND m.ref_id=s.id AND m.product_id=i.product_id AND m.move_type='sale' AND bm.qty<0) AS batches, "
        "(SELECT group_concat(serial, ', ') FROM serials sr WHERE sr.sale_item_id=i.id) AS serials "
        "FROM sale_items i JOIN sales s ON s.id=i.sale_id LEFT JOIN users u ON u.id=s.user_id "
        f"LEFT JOIN customers c ON c.id=s.customer_id WHERE i.product_id=? AND s.branch_id=? ORDER BY s.id DESC LIMIT {limit}",
        (pid, b)).fetchall()
    purchases = db.execute(
        "SELECT i.*, pu.id AS pur_id, pu.ref_no, pu.date, pu.status, sp.name AS supplier, "
        "(SELECT group_concat('#' || id, ', ') FROM stock_batches WHERE purchase_item_id=i.id) AS batches "
        "FROM purchase_items i JOIN purchases pu ON pu.id=i.purchase_id JOIN suppliers sp ON sp.id=pu.supplier_id "
        f"WHERE i.product_id=? AND pu.branch_id=? ORDER BY pu.id DESC LIMIT {limit}", (pid, b)).fetchall()
    serials = db.execute('SELECT s.*, sa.receipt_no FROM serials s LEFT JOIN sales sa ON sa.id=s.sale_id '
                         "WHERE s.product_id=? AND (s.branch_id=? OR s.status IN ('Sold','In transit')) "
                         "ORDER BY CASE s.status WHEN 'In stock' THEN 0 ELSE 1 END, s.serial LIMIT 3000",
                         (pid, b)).fetchall() if p['serial_tracked'] or db.execute(
                             'SELECT 1 FROM serials WHERE product_id=? LIMIT 1', (pid,)).fetchone() else []
    in_stock_sn = sum(1 for r in serials if r['status'] == 'In stock' and r['branch_id'] == b)
    sold = db.execute("SELECT COALESCE(SUM(i.qty-i.returned_qty),0) AS q, COALESCE(SUM((i.qty-i.returned_qty)*i.price),0) AS v "
                      "FROM sale_items i JOIN sales s ON s.id=i.sale_id WHERE s.status='Completed' AND i.product_id=? "
                      "AND s.branch_id=?", (pid, b)).fetchone()
    return render_template('products/view.html', p=p, moves=moves, prices=prices, batches=batches, sales=sales,
                           purchases=purchases, serials=serials, in_stock_sn=in_stock_sn, sold=sold, tab=tab,
                           limit=limit, labels=rules.MOVE_LABELS)


@bp.route('/<int:pid>/serials', methods=['POST'])
@requires('products', 'adjust')
def register_serials(pid):
    """Give serial numbers to items already in stock (e.g. after switching serial numbers on)."""
    try:
        with tx() as db:
            p = rules.product(db, pid)
            if not p['serial_tracked']:
                raise RuleError('Switch on serial numbers for this product first (Edit product).')
            sers = rules.parse_serials(request.form.get('serials'))
            if not sers:
                raise RuleError('Type or scan at least one serial number.')
            free = p['stock'] - len(rules.serials_in_stock(db, pid))
            if len(sers) > free + rules.EPS:
                raise RuleError(f'Only {core.fmt_qty(max(free, 0))} items in stock have no serial number yet.')
            batch = rules.open_batches(db, pid, branch_id())
            rules.register_serials(db, p, sers, g.user['id'], 'Registered (stock on hand)', ref_type='product',
                                   ref_id=pid, batch_id=batch[0]['id'] if batch else None)
            audit('serials_register', f"{p['code']}: {len(sers)}", db)
        flash(f'{len(sers)} serial number(s) registered.', 'ok')
    except RuleError as e:
        flash(str(e), 'error')
    return redirect(url_for('products.view', pid=pid, tab='serials'))


@bp.route('/serial/<path:sn>')
@requires('products', 'inventory', 'sales_view')
def serial(sn):
    """The life of one serial-numbered item: purchased, sold, returned, current status."""
    db = get_db()
    r = db.execute('SELECT s.*, p.name, p.code, b.name AS branch FROM serials s JOIN products p ON p.id=s.product_id '
                   'LEFT JOIN branches b ON b.id=s.branch_id WHERE s.serial=?', (sn,)).fetchone()
    if not r:
        return fail('Serial number not found.', 'products.index')
    events = db.execute('SELECT e.*, u.username, b.name AS branch FROM serial_events e LEFT JOIN users u ON '
                        'u.id=e.user_id LEFT JOIN branches b ON b.id=e.branch_id WHERE serial_id=? ORDER BY e.id',
                        (r['id'],)).fetchall()
    return render_template('products/serial.html', r=r, events=events)


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


# ---------------------------------------------------------------- import from Excel (update section 1.1)
PRODUCT_COLS = [  # (field, header in the template, other accepted headers)
    ('code', 'Code *', ['code', 'product code', 'sku', 'item code']),
    ('name', 'Name *', ['name', 'product name', 'product', 'item name', 'description name']),
    ('category', 'Category', ['category', 'category name']),
    ('unit', 'Unit', ['unit', 'unit of measure', 'uom']),
    ('barcode', 'Barcode', ['barcode', 'bar code']),
    ('price', 'Selling price *', ['selling price', 'price', 'sale price']),
    ('cost', 'Purchase cost', ['purchase cost', 'cost', 'unit cost']),
    ('opening', 'Opening stock', ['opening stock', 'stock', 'quantity', 'qty', 'opening']),
    ('min_stock', 'Minimum stock', ['minimum stock', 'min stock', 'min', 'reorder level']),
    ('serial_tracked', 'Serial numbers (yes/no)', ['serial numbers yes no', 'serial numbers', 'serial', 'serial tracked']),
    ('description', 'Description', ['description', 'details', 'notes']),
]
CATEGORY_COLS = [('name', 'Category name *', ['category name', 'name', 'category']),
                 ('active', 'Active (yes/no)', ['active yes no', 'active'])]
IMPORT_KINDS = {'products': PRODUCT_COLS, 'categories': CATEGORY_COLS}


def _fields(kind):
    return {f: [core_norm(h)] + alts for f, h, alts in IMPORT_KINDS[kind]}


def core_norm(h):
    import re
    return re.sub(r'[^a-z0-9]+', ' ', h.lower().replace('*', '')).strip()


@bp.route('/import/template/<kind>.xlsx')
@requires('products')
def import_template(kind):
    """Empty Excel template (header row, one example row) for the product or category import."""
    from flask import Response
    from reports import T, xlsx_blocks
    if kind not in IMPORT_KINDS:
        return fail('Unknown template.', 'products.import_page')
    cols = [(h, f, T) for f, h, _a in IMPORT_KINDS[kind]]
    example = ({'code': 'P-001', 'name': 'Example: Coca-Cola 500 ml', 'category': 'Drinks', 'unit': 'pcs',
                'barcode': '', 'price': '35', 'cost': '28', 'opening': '24', 'min_stock': '6', 'serial_tracked': 'no',
                'description': ''} if kind == 'products' else {'name': 'Drinks', 'active': 'yes'})
    data = xlsx_blocks('', '', [(None, cols, [example], {})], sheet_name=kind.capitalize())
    return Response(data, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': f'attachment; filename="{kind}-import-template.xlsx"'})


def _check_rows(kind, rows):
    """Validate imported rows. Each row gets status OK / Duplicate / Error and a message; nothing is saved."""
    db = get_db()
    out, seen, seen_bc = [], {}, {}
    cats = {r['name'].lower(): r['id'] for r in db.execute('SELECT id, name FROM categories')}
    for n, r in enumerate(rows, start=2):  # row 1 is the header
        row_no = r.get('_row', n)
        r = {k: str(v if v is not None else '').strip() for k, v in r.items() if k != '_row'}
        res = {'row': row_no, 'data': dict(r), 'status': 'OK', 'msg': ''}
        try:
            if kind == 'categories':
                name = r.get('name', '')
                if not name:
                    raise RuleError('Category name is required.')
                if name.lower() in seen:
                    res['status'], res['msg'] = 'Duplicate', f"Also in row {seen[name.lower()]} of the file."
                elif name.lower() in cats:
                    res['status'], res['msg'] = 'Duplicate', 'This category already exists.'
                seen.setdefault(name.lower(), res['row'])
            else:
                v = _form_values({**r, 'category_id': None}, True)
                if not r.get('price'):
                    raise RuleError('Selling price is required.')
                code, bc = v['code'].lower(), v['barcode']
                if code in seen:
                    res['status'], res['msg'] = 'Duplicate', f"Code {v['code']} is also in row {seen[code]} of the file."
                elif db.execute('SELECT 1 FROM products WHERE code=?', (v['code'],)).fetchone():
                    res['status'], res['msg'] = 'Duplicate', f"Code {v['code']} already belongs to a product."
                elif bc and (bc in seen_bc or db.execute('SELECT 1 FROM products WHERE barcode=?', (bc,)).fetchone()):
                    res['status'], res['msg'] = 'Duplicate', f'Barcode {bc} is already used.'
                seen.setdefault(code, res['row'])
                if bc:
                    seen_bc.setdefault(bc, res['row'])
                if res['status'] == 'OK':
                    if r.get('category') and r['category'].lower() not in cats:
                        res['msg'] = f"New category {r['category']} will be created."
                    if v['serial_tracked'] and v['opening'] > 0:
                        res['msg'] = (res['msg'] + ' ').lstrip() + 'Register the serial numbers of the opening stock on the product page.'
        except (RuleError, ValueError) as e:
            res['status'], res['msg'] = 'Error', str(e) if isinstance(e, RuleError) else 'Check the values in this row.'
        out.append(res)
    return out


def _save_rows(kind, checked):
    """Save the rows that passed the check (inside one transaction). Returns how many were saved."""
    n = 0
    with tx() as db:
        claim_once(db)
        cats = {r['name'].lower(): r['id'] for r in db.execute('SELECT id, name FROM categories')}
        for res in checked:
            if res['status'] != 'OK':
                continue
            r = res['data']
            if kind == 'categories':
                db.execute('INSERT INTO categories(name, active) VALUES (?, ?)',
                           (r['name'], 0 if r.get('active', '').lower() in ('no', 'n', '0', 'false') else 1))
                cats[r['name'].lower()] = True
            else:
                cid = None
                if r.get('category'):
                    cid = cats.get(r['category'].lower())
                    if not cid:
                        cid = db.execute('INSERT INTO categories(name) VALUES (?)', (r['category'],)).lastrowid
                        cats[r['category'].lower()] = cid
                v = _form_values({**r, 'category_id': cid}, True)
                v['category_id'] = cid
                if not v['unit']:
                    v['unit'] = 'pcs'
                _create(db, v)
            n += 1
        audit('import', f'{kind}: {n} saved', db)
    return n


@bp.route('/import', methods=['GET', 'POST'])
@requires('products')
def import_page():
    """Import products or categories from Excel / CSV: check first, then save the correct rows.
    Rows with errors or duplicates are listed and can be downloaded, corrected and uploaded again."""
    import json
    import excelio
    kind = request.values.get('kind', 'products')
    if kind not in IMPORT_KINDS:
        kind = 'products'
    checked, saved, stage = [], None, 'upload'
    if request.method == 'POST':
        try:
            if request.form.get('action') == 'save':
                rows = json.loads(request.form.get('rows') or '[]')
                checked = _check_rows(kind, [dict(r['data'], _row=r['row']) for r in rows])  # check again: data may have changed
                saved = _save_rows(kind, checked)
                stage = 'done'
                flash(f'{saved:,} {kind} imported.', 'ok')
            else:
                f = request.files.get('file')
                if not f or not f.filename:
                    raise RuleError('Choose the Excel or CSV file to import.')
                table = excelio.read_table(f.filename, f.read())
                if len(table) < 2:
                    raise RuleError('The file has no rows under the header row.')
                cols = excelio.header_map(table[0], _fields(kind))
                need = ['code', 'name', 'price'] if kind == 'products' else ['name']
                missing = [h for fld, h, _a in IMPORT_KINDS[kind] if fld in need and fld not in cols]
                if missing:
                    raise RuleError('These columns are missing in the first row: ' + ', '.join(missing) +
                                    '. Use the template.')
                rows = [dict({fld: (row[i] if i < len(row) else '') for fld, i in cols.items()}, _row=n)
                        for n, row in enumerate(table[1:], start=2)]
                checked = _check_rows(kind, rows)
                stage = 'check'
        except (RuleError, ValueError) as e:
            flash(str(e) if isinstance(e, RuleError) else 'The file could not be read.', 'error')
        except sqlite3.IntegrityError:
            flash('Another product already uses one of these codes or barcodes. Nothing was imported.', 'error')
            stage = 'upload'
    counts = {k: sum(1 for r in checked if r['status'] == k) for k in ('OK', 'Duplicate', 'Error')}
    return render_template('products/import.html', kind=kind, stage=stage, checked=checked, counts=counts,
                           saved=saved, cols=IMPORT_KINDS[kind])


@bp.route('/import/errors', methods=['POST'])
@requires('products')
def import_errors():
    """The rows that were not imported, as an Excel file in the template layout, to correct and upload again."""
    import json
    from flask import Response
    from reports import T, xlsx_blocks
    kind = request.form.get('kind', 'products')
    if kind not in IMPORT_KINDS:
        return fail('Unknown import.', 'products.import_page')
    rows = [dict({f: r['data'].get(f, '') for f, _h, _a in IMPORT_KINDS[kind]}, _problem=f"{r['status']}: {r['msg']}")
            for r in json.loads(request.form.get('rows') or '[]')]
    cols = [(h, f, T) for f, h, _a in IMPORT_KINDS[kind]] + [('Problem (delete this column or leave it)', '_problem', T)]
    data = xlsx_blocks('', '', [(None, cols, rows, {})], sheet_name=kind.capitalize())
    return Response(data, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': f'attachment; filename="{kind}-to-correct.xlsx"'})
