"""Suppliers and customers."""
from flask import Blueprint, flash, redirect, render_template, request, url_for

from core import fail, get_db, requires, tx

bp = Blueprint('contacts', __name__)

SUPPLIER_FIELDS = ['name', 'contact_person', 'phone', 'address', 'email', 'description']
CUSTOMER_FIELDS = ['name', 'phone', 'address', 'notes']


def _search(table, q, extra=''):
    sql = f'SELECT * FROM {table} WHERE 1=1 {extra}'
    args = []
    if q:
        sql += ' AND (name LIKE ? OR phone LIKE ?)'
        args = [f'%{q}%', f'%{q}%']
    return get_db().execute(sql + ' ORDER BY name LIMIT 1000', args).fetchall()


def _save(table, fields, rid, endpoint, has_active):
    vals = {k: request.form.get(k, '').strip() for k in fields}
    if not vals['name']:
        flash('Name is required.', 'error')
        return None
    with tx() as db:
        if rid:
            sets = ', '.join(f'{k}=?' for k in fields) + (', active=?' if has_active else '')
            args = list(vals.values()) + ([1 if request.form.get('active') else 0] if has_active else [])
            db.execute(f'UPDATE {table} SET {sets} WHERE id=?', args + [rid])
        else:
            cols = ', '.join(fields)
            rid = db.execute(f'INSERT INTO {table}({cols}) VALUES ({",".join("?" * len(fields))})',
                             list(vals.values())).lastrowid
    flash('Saved.', 'ok')
    return redirect(url_for(endpoint, rid=rid))


# ---------------------------------------------------------------- suppliers
@bp.route('/suppliers')
@requires('suppliers', 'purchases')
def suppliers():
    q = request.args.get('q', '').strip()
    rows = get_db().execute(
        "SELECT s.*, (SELECT COALESCE(SUM(total-returned_value-paid),0) FROM purchases p WHERE "
        "p.supplier_id=s.id AND p.status NOT IN ('Draft','Cancelled')) AS owed FROM suppliers s "
        "WHERE (? = '' OR s.name LIKE ? OR s.phone LIKE ?) ORDER BY s.name",
        (q, f'%{q}%', f'%{q}%')).fetchall()
    return render_template('contacts/suppliers.html', rows=rows, q=q)


@bp.route('/suppliers/new', methods=['GET', 'POST'])
@bp.route('/suppliers/<int:rid>/edit', methods=['GET', 'POST'])
@requires('suppliers')
def supplier_edit(rid=None):
    r = get_db().execute('SELECT * FROM suppliers WHERE id=?', (rid,)).fetchone() if rid else None
    if request.method == 'POST':
        resp = _save('suppliers', SUPPLIER_FIELDS, rid, 'contacts.supplier', True)
        if resp:
            return resp
    return render_template('contacts/edit.html', r=r, kind='Supplier', fields=SUPPLIER_FIELDS,
                           has_active=True)


@bp.route('/suppliers/<int:rid>')
@requires('suppliers', 'purchases')
def supplier(rid):
    db = get_db()
    r = db.execute('SELECT * FROM suppliers WHERE id=?', (rid,)).fetchone()
    if not r:
        return fail('Supplier not found.', 'contacts.suppliers')
    hist = db.execute('SELECT * FROM purchases WHERE supplier_id=? ORDER BY id DESC', (rid,)).fetchall()
    owed = sum(p['total'] - p['returned_value'] - p['paid'] for p in hist
               if p['status'] not in ('Draft', 'Cancelled'))
    pays = db.execute('SELECT pp.*, p.ref_no, u.username FROM purchase_payments pp JOIN purchases p ON '
                      'p.id=pp.purchase_id LEFT JOIN users u ON u.id=pp.user_id WHERE p.supplier_id=? '
                      'ORDER BY pp.id DESC LIMIT 200', (rid,)).fetchall()
    return render_template('contacts/supplier.html', r=r, hist=hist, owed=owed, pays=pays)


# ---------------------------------------------------------------- customers
@bp.route('/customers')
@requires('customers', 'sales_view')
def customers():
    q = request.args.get('q', '').strip()
    rows = get_db().execute(
        "SELECT c.*, (SELECT COALESCE(SUM(total-refunded-paid),0) FROM sales s WHERE "
        "s.customer_id=c.id AND s.status='Completed') AS owes FROM customers c "
        "WHERE (? = '' OR c.name LIKE ? OR c.phone LIKE ?) ORDER BY c.name",
        (q, f'%{q}%', f'%{q}%')).fetchall()
    return render_template('contacts/customers.html', rows=rows, q=q)


@bp.route('/customers/new', methods=['GET', 'POST'])
@bp.route('/customers/<int:rid>/edit', methods=['GET', 'POST'])
@requires('customers')
def customer_edit(rid=None):
    r = get_db().execute('SELECT * FROM customers WHERE id=?', (rid,)).fetchone() if rid else None
    if request.method == 'POST':
        resp = _save('customers', CUSTOMER_FIELDS, rid, 'contacts.customer', False)
        if resp:
            return resp
    return render_template('contacts/edit.html', r=r, kind='Customer', fields=CUSTOMER_FIELDS,
                           has_active=False)


@bp.route('/customers/<int:rid>')
@requires('customers', 'sales_view')
def customer(rid):
    db = get_db()
    r = db.execute('SELECT * FROM customers WHERE id=?', (rid,)).fetchone()
    if not r:
        return fail('Customer not found.', 'contacts.customers')
    hist = db.execute('SELECT * FROM sales WHERE customer_id=? ORDER BY id DESC', (rid,)).fetchall()
    owes = sum(s['total'] - s['refunded'] - s['paid'] for s in hist if s['status'] == 'Completed')
    pays = db.execute('SELECT sp.*, s.receipt_no, u.username FROM sale_payments sp JOIN sales s ON s.id=sp.sale_id '
                      'LEFT JOIN users u ON u.id=sp.user_id WHERE s.customer_id=? ORDER BY sp.id DESC LIMIT 200',
                      (rid,)).fetchall()
    import rules
    return render_template('contacts/customer.html', r=r, hist=hist, owes=owes, pays=pays,
                           pay_status=rules.payment_status)
