"""Pharmacy module: medicines, patients, prescribers, prescriptions and dispensing, expiry, recalls,
disposal and the controlled-medicine register.

Switched on per business in Admin -> Settings -> Pharmacy. With it off, none of these pages are shown and the
rest of the app works exactly as before. Stock, batches (FEFO), purchases, POS and receipts are the shared ones;
this file only adds what a pharmacy needs on top. Stock changes still go through rules.py.
"""
import os
import secrets
from datetime import date, timedelta

from flask import Blueprint, Response, abort, flash, g, redirect, render_template, request, send_file, url_for
from werkzeug.security import check_password_hash

import core
from core import (CONTROLLED, DOSAGE_FORMS, RX_CLASSES, RX_CLASS_LABELS, STORAGE, RuleError, audit, branch_id,
                  can, claim_once, fail, get_db, next_no, parse_money, parse_qty, payment_methods, pharmacy_on,
                  requires, rnd, setting, today, tx)
import rules

bp = Blueprint('pharmacy', __name__, url_prefix='/pharmacy')
STATUSES = ['Registered', 'Verified', 'Partially dispensed', 'Dispensed', 'Rejected', 'Cancelled']
BATCH_STATUSES = ['Available', 'Quarantined', 'Damaged']   # Recalled is set by a recall only
DISPOSAL_METHODS = ['Incineration', 'Return to manufacturer / supplier', 'Encapsulation', 'Other approved method']


@bp.before_request
def only_when_on():
    if g.get('user') and not pharmacy_on():
        flash('The pharmacy module is off. An administrator can switch it on in Admin → Settings.', 'error')
        return redirect(url_for('dash.index'))


# ---------------------------------------------------------------- helpers
def _upload(field, kind):
    """Save an uploaded PDF / JPG / PNG (up to 10 MB) for this business; returns the stored file name or ''."""
    f = request.files.get(field)
    if not f or not f.filename:
        return ''
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ('.pdf', '.jpg', '.jpeg', '.png'):
        raise RuleError('Upload a PDF, JPG or PNG file.')
    f.stream.seek(0, os.SEEK_END)
    size = f.stream.tell()
    f.stream.seek(0)
    if size > 10 * 1024 * 1024:
        raise RuleError('The file must be 10 MB or smaller.')
    name = f'{kind}-{date.today():%Y%m%d}-{secrets.token_hex(6)}{ext}'
    os.makedirs(_files_dir(), exist_ok=True)
    f.save(os.path.join(_files_dir(), name))
    return name


def _files_dir():
    return os.path.join(os.path.dirname(os.path.abspath(core.platform_path())), 'pharmacy',
                        f"business-{g.business['id'] if g.get('business') else 1}")


def licence_problem(user):
    """Why this user may not verify or dispense (None = allowed). FR-007-10."""
    if not user['licence_no'] or not user['licence_expiry']:
        return ('Only a pharmacist with a licence can do this. An administrator adds the licence number and '
                'expiry date to the user in Admin → Users.')
    if user['licence_expiry'] < today():
        return f"Your pharmacist licence expired on {user['licence_expiry']}. Renew it before dispensing."
    return None


def licence_warning(user):
    if user['licence_expiry'] and today() <= user['licence_expiry'] <= (date.today() + timedelta(days=30)).isoformat():
        return f"Your pharmacist licence expires on {user['licence_expiry']}."
    return None


def _need_pharmacist():
    why = licence_problem(g.user)
    if why:
        raise RuleError(why)


def patient_age(p):
    if not p or not p['birth_date']:
        return None
    try:
        b = date.fromisoformat(p['birth_date'])
    except ValueError:
        return None
    t = date.today()
    return t.year - b.year - ((t.month, t.day) < (b.month, b.day))


def _rx(db, rid):
    rx = db.execute('SELECT r.*, pt.name AS patient, pt.phone AS patient_phone, pt.allergies, pt.birth_date, pt.sex, '
                    'pt.weight_kg, pt.pregnancy, pt.conditions, pt.customer_id AS patient_customer, pr.name AS prescriber, '
                    'pr.licence_no AS prescriber_licence, pr.facility, pr.profession, u.full_name AS verifier '
                    'FROM prescriptions r JOIN patients pt ON pt.id=r.patient_id JOIN prescribers pr ON pr.id=r.prescriber_id '
                    'LEFT JOIN users u ON u.id=r.verified_by WHERE r.id=? AND r.branch_id=?', (rid, branch_id())).fetchone()
    if not rx:
        raise RuleError('Prescription not found in this branch.')
    return rx


def _rx_items(db, rid):
    return db.execute('SELECT i.*, p.code, p.name, p.unit, p.price, m.generic_name, m.strength, m.dosage_form, m.rx_class, '
                      'm.storage, COALESCE(ps.stock, 0) AS stock FROM rx_items i JOIN products p ON p.id=i.product_id '
                      'LEFT JOIN medicines m ON m.product_id=p.id LEFT JOIN product_stock ps ON ps.product_id=p.id '
                      'AND ps.branch_id=? WHERE i.prescription_id=? ORDER BY i.id', (branch_id(), rid)).fetchall()


def valid_until(rx, items):
    """Last day the prescription can be dispensed (30 days, or 7 for controlled medicines, by default)."""
    controlled = any(i['rx_class'] in CONTROLLED for i in items)
    days = int(setting('rx_controlled_days' if controlled else 'rx_valid_days') or (7 if controlled else 30))
    try:
        return (date.fromisoformat(rx['date']) + timedelta(days=days)).isoformat()
    except ValueError:
        return rx['date']


def shown_status(rx, items):
    if rx['status'] in ('Registered', 'Verified', 'Partially dispensed') and valid_until(rx, items) < today():
        return 'Expired'
    return rx['status']


def safety_warnings(db, rx, items):
    """Allergy and duplicate-therapy checks (FR-007-05, FR-007-09). A pharmacist must give a reason to go on."""
    out = []
    allergies = [a.strip().lower() for a in (rx['allergies'] or '').replace(';', ',').split(',') if a.strip()]
    for i in items:
        names = [n.lower() for n in (i['generic_name'], i['name']) if n]
        for a in allergies:
            if any(a in n or n in a for n in names):
                out.append(f"Allergy: the patient is allergic to “{a}” and {i['name']} is prescribed.")
        if i['generic_name']:
            dup = db.execute(
                "SELECT r.ref_no, s.date FROM dispensings d JOIN prescriptions r ON r.id=d.prescription_id "
                "JOIN sales s ON s.id=d.sale_id JOIN sale_items si ON si.sale_id=s.id JOIN medicines m "
                "ON m.product_id=si.product_id WHERE r.patient_id=? AND r.id<>? AND s.status='Completed' "
                "AND lower(m.generic_name)=lower(?) AND s.date>=? ORDER BY s.date DESC LIMIT 1",
                (rx['patient_id'], rx['id'], i['generic_name'],
                 (date.today() - timedelta(days=30)).isoformat())).fetchone()
            if dup:
                out.append(f"Duplicate therapy: {i['generic_name']} was already dispensed to this patient on "
                           f"{dup['date']} ({dup['ref_no']}).")
    age = patient_age(rx)
    if age is not None and age < 12 and not rx['weight_kg']:
        out.append('The patient is under 12: record the weight on the patient before dispensing.')
    return out


# ---------------------------------------------------------------- patients
@bp.route('/patients')
@requires('pharmacy', 'dispense')
def patients():
    q = request.args.get('q', '').strip()
    rows = get_db().execute('SELECT * FROM patients WHERE name LIKE ? OR phone LIKE ? ORDER BY name LIMIT 500',
                            (f'%{q}%', f'%{q}%')).fetchall()
    return render_template('pharmacy/patients.html', rows=rows, q=q, age=patient_age)


@bp.route('/patients/new', methods=['GET', 'POST'])
@bp.route('/patients/<int:pid>', methods=['GET', 'POST'])
@requires('pharmacy', 'dispense')
def patient(pid=None):
    db = get_db()
    p = db.execute('SELECT * FROM patients WHERE id=?', (pid,)).fetchone() if pid else None
    if pid and not p:
        return fail('Patient not found.', 'pharmacy.patients')
    if request.method == 'POST':
        f = request.form
        try:
            name = f.get('name', '').strip()
            if not name:
                raise RuleError('The patient name is required.')
            birth = f.get('birth_date', '').strip() or None
            if not birth and f.get('age', '').strip():
                age = int(parse_qty(f.get('age'), 'Age', allow_zero=True))
                birth = date(date.today().year - age, 1, 1).isoformat()
            weight = parse_qty(f.get('weight_kg'), 'Weight') if f.get('weight_kg', '').strip() else None
            if birth and patient_age({'birth_date': birth}) is not None and patient_age({'birth_date': birth}) < 12 \
                    and not weight:
                raise RuleError('The weight is required for a child under 12.')
            vals = (name, f.get('phone', '').strip(), birth, f.get('sex', ''), weight, f.get('allergies', '').strip(),
                    f.get('pregnancy', ''), f.get('conditions', '').strip(), f.get('customer_id', type=int))
            with tx() as db:
                if p:
                    db.execute('UPDATE patients SET name=?, phone=?, birth_date=?, sex=?, weight_kg=?, allergies=?, '
                               'pregnancy=?, conditions=?, customer_id=? WHERE id=?', vals + (pid,))
                else:
                    pid = db.execute('INSERT INTO patients(name, phone, birth_date, sex, weight_kg, allergies, pregnancy, '
                                     'conditions, customer_id) VALUES (?,?,?,?,?,?,?,?,?)', vals).lastrowid
                audit('patient_save', f'{pid} {name}', db)
            flash('Patient saved.', 'ok')
            nxt = f.get('next', '')
            return redirect(nxt + f'?patient_id={pid}' if nxt.startswith('/pharmacy/') else url_for('pharmacy.patient', pid=pid))
        except RuleError as e:
            flash(str(e), 'error')
    history = []
    if p and (can('dispense') or g.user['role'] == core.ADMIN):  # FR-007-07: only pharmacists / managers, and logged
        history = db.execute('SELECT r.*, pr.name AS prescriber FROM prescriptions r JOIN prescribers pr ON '
                             'pr.id=r.prescriber_id WHERE r.patient_id=? ORDER BY r.id DESC LIMIT 200', (pid,)).fetchall()
        audit('patient_history_view', f'patient {pid}')
    customers = db.execute('SELECT id, name, phone FROM customers ORDER BY name').fetchall()
    return render_template('pharmacy/patient.html', p=p, f=request.form, history=history, customers=customers,
                           age=patient_age(p), nxt=request.args.get('next', ''))


# ---------------------------------------------------------------- prescribers
@bp.route('/prescribers', methods=['GET', 'POST'])
@requires('pharmacy', 'dispense')
def prescribers():
    db = get_db()
    if request.method == 'POST':
        f = request.form
        try:
            if not f.get('name', '').strip():
                raise RuleError('The prescriber name is required.')
            vals = tuple(f.get(k, '').strip() for k in ('name', 'profession', 'licence_no', 'facility', 'phone'))
            rid = f.get('id', type=int)
            with tx() as db:
                if rid:
                    db.execute('UPDATE prescribers SET name=?, profession=?, licence_no=?, facility=?, phone=? WHERE id=?',
                               vals + (rid,))
                else:
                    rid = db.execute('INSERT INTO prescribers(name, profession, licence_no, facility, phone) '
                                     'VALUES (?,?,?,?,?)', vals).lastrowid
                audit('prescriber_save', f'{rid} {vals[0]}', db)
            flash('Prescriber saved.', 'ok')
            nxt = f.get('next', '')
            if nxt.startswith('/pharmacy/'):
                return redirect(nxt + f'?prescriber_id={rid}')
        except RuleError as e:
            flash(str(e), 'error')
        return redirect(url_for('pharmacy.prescribers'))
    rows = db.execute('SELECT pr.*, (SELECT COUNT(*) FROM prescriptions r WHERE r.prescriber_id=pr.id) AS n '
                      'FROM prescribers pr ORDER BY name').fetchall()
    edit = db.execute('SELECT * FROM prescribers WHERE id=?', (request.args.get('edit', type=int),)).fetchone()
    return render_template('pharmacy/prescribers.html', rows=rows, edit=edit, nxt=request.args.get('next', ''))


# ---------------------------------------------------------------- prescriptions
@bp.route('/')
@bp.route('/prescriptions')
@requires('pharmacy', 'dispense')
def prescriptions():
    db = get_db()
    st, q = request.args.get('status', 'open'), request.args.get('q', '').strip()
    sql = ('SELECT r.*, pt.name AS patient, pr.name AS prescriber FROM prescriptions r JOIN patients pt ON '
           'pt.id=r.patient_id JOIN prescribers pr ON pr.id=r.prescriber_id WHERE r.branch_id=?')
    args = [branch_id()]
    if st == 'open':
        sql += " AND r.status IN ('Registered','Verified','Partially dispensed')"
    elif st:
        sql += ' AND r.status=?'
        args.append(st)
    if q:
        sql += ' AND (pt.name LIKE ? OR r.ref_no LIKE ? OR r.rx_no LIKE ? OR pt.phone LIKE ?)'
        args += [f'%{q}%'] * 4
    rows = db.execute(sql + ' ORDER BY r.id DESC LIMIT 500', args).fetchall()
    rows = [dict(r, shown=shown_status(r, _rx_items(db, r['id']))) for r in rows]
    return render_template('pharmacy/prescriptions.html', rows=rows, st=st, q=q, statuses=STATUSES)


@bp.route('/prescriptions/new', methods=['GET', 'POST'])
@requires('pharmacy', 'dispense')
def rx_new():
    db = get_db()
    f = request.form
    if request.method == 'POST':
        try:
            patient_id, prescriber_id = f.get('patient_id', type=int), f.get('prescriber_id', type=int)
            if not db.execute('SELECT 1 FROM patients WHERE id=?', (patient_id,)).fetchone():
                raise RuleError('Choose the patient (or add a new one).')
            if not db.execute('SELECT 1 FROM prescribers WHERE id=?', (prescriber_id,)).fetchone():
                raise RuleError('Choose the prescriber (or add a new one).')
            rx_date = f.get('date') or today()
            if rx_date > today():
                raise RuleError('The prescription date cannot be in the future.')
            rx_no = f.get('rx_no', '').strip()
            lines = []
            for pid, q, dose, freq, dur, ins in zip(f.getlist('pid'), f.getlist('qty'), f.getlist('dose'),
                                                    f.getlist('frequency'), f.getlist('duration'),
                                                    f.getlist('instructions')):
                if pid:
                    lines.append((int(pid), parse_qty(q), dose.strip(), freq.strip(), dur.strip(), ins.strip()))
            if not lines:
                raise RuleError('Add at least one medicine.')
            controlled = [db.execute('SELECT rx_class FROM medicines WHERE product_id=?', (l[0],)).fetchone() for l in lines]
            is_controlled = any(c and c['rx_class'] in CONTROLLED for c in controlled)
            if is_controlled and not f.get('form_serial', '').strip():
                raise RuleError('Controlled medicines need the serial number of the EFDA prescription form.')
            if rx_no and db.execute("SELECT 1 FROM prescriptions WHERE rx_no=? AND patient_id=? AND prescriber_id=? "
                                    "AND status NOT IN ('Rejected','Cancelled')", (rx_no, patient_id, prescriber_id)).fetchone() \
                    and not f.get('dup_ok'):  # FR-006-11
                raise RuleError(f'Prescription number {rx_no} of this prescriber was already registered for this '
                                f'patient. Tick “It is a new prescription” only if it really is another one.')
            scan = _upload('scan', 'rx')
            if is_controlled and not scan:
                raise RuleError('Controlled medicines: upload a scan or photo of the original prescription form.')
            with tx() as db:
                claim_once(db)
                ref = next_no(db, 'RX', 'prescriptions', 'ref_no')
                rid = db.execute('INSERT INTO prescriptions(ref_no, rx_no, form_serial, date, patient_id, prescriber_id, '
                                 'scan_file, notes, user_id, branch_id) VALUES (?,?,?,?,?,?,?,?,?,?)',
                                 (ref, rx_no, f.get('form_serial', '').strip(), rx_date, patient_id, prescriber_id, scan,
                                  f.get('notes', '').strip(), g.user['id'], branch_id())).lastrowid
                db.executemany('INSERT INTO rx_items(prescription_id, product_id, qty, dose, frequency, duration, '
                               'instructions) VALUES (?,?,?,?,?,?,?)', [(rid,) + (l[0], l[1]) + l[2:] for l in lines])
                audit('rx_register', ref, db)
            flash('Prescription registered. A pharmacist verifies it next.', 'ok')
            return redirect(url_for('pharmacy.rx_view', rid=rid))
        except RuleError as e:
            flash(str(e), 'error')
    patients_ = db.execute('SELECT id, name, phone FROM patients ORDER BY name').fetchall()
    prescribers_ = db.execute('SELECT id, name, facility FROM prescribers ORDER BY name').fetchall()
    products = db.execute('SELECT pv.id, pv.code, pv.name, pv.unit, pv.stock, m.rx_class, m.strength FROM pv '
                          'LEFT JOIN medicines m ON m.product_id=pv.id WHERE pv.active=1 ORDER BY pv.name').fetchall()
    return render_template('pharmacy/rx_new.html', patients=patients_, prescribers=prescribers_, products=products,
                           f=f, today=today(), sel_patient=request.args.get('patient_id', type=int) or f.get('patient_id', type=int),
                           sel_prescriber=request.args.get('prescriber_id', type=int) or f.get('prescriber_id', type=int))


@bp.route('/prescriptions/<int:rid>')
@requires('pharmacy', 'dispense')
def rx_view(rid):
    db = get_db()
    try:
        rx = _rx(db, rid)
    except RuleError as e:
        return fail(e, 'pharmacy.prescriptions')
    items = _rx_items(db, rid)
    disp = db.execute('SELECT d.*, s.receipt_no, s.total, s.date, u.full_name AS pharmacist, c.full_name AS checker '
                      'FROM dispensings d JOIN sales s ON s.id=d.sale_id JOIN users u ON u.id=d.pharmacist_id '
                      'LEFT JOIN users c ON c.id=d.checked_by WHERE d.prescription_id=? ORDER BY d.id', (rid,)).fetchall()
    shown = shown_status(rx, items)
    warn = safety_warnings(db, rx, items) if rx['status'] == 'Registered' else []
    customers = db.execute('SELECT id, name, phone FROM customers ORDER BY name').fetchall()
    users = db.execute('SELECT id, full_name, username FROM users WHERE active=1 AND id<>? ORDER BY full_name',
                       (g.user['id'],)).fetchall()
    controlled = any(i['rx_class'] in CONTROLLED for i in items)
    return render_template('pharmacy/rx_view.html', rx=rx, items=items, disp=disp, shown=shown, warn=warn,
                           valid=valid_until(rx, items), lic=licence_problem(g.user), lic_warn=licence_warning(g.user),
                           customers=customers, methods=payment_methods(), users=users, controlled=controlled,
                           age=patient_age(rx), CONTROLLED=CONTROLLED)


@bp.route('/prescriptions/<int:rid>/scan')
@requires('pharmacy', 'dispense')
def rx_scan(rid):
    rx = get_db().execute('SELECT scan_file FROM prescriptions WHERE id=? AND branch_id=?', (rid, branch_id())).fetchone()
    if not rx or not rx['scan_file']:
        abort(404)
    return send_file(os.path.join(_files_dir(), os.path.basename(rx['scan_file'])), max_age=0)


@bp.route('/prescriptions/<int:rid>/verify', methods=['POST'])
@requires('dispense')
def rx_verify(rid):
    """Pharmacist verification (FR-006-05). Warnings need a reason, which is kept as an intervention."""
    f = request.form
    try:
        with tx() as db:
            _need_pharmacist()
            rx = _rx(db, rid)
            items = _rx_items(db, rid)
            if rx['status'] != 'Registered':
                raise RuleError('Only a registered prescription can be verified.')
            if valid_until(rx, items) < today():
                raise RuleError(f'This prescription is no longer valid (it could be dispensed until {valid_until(rx, items)}).')
            for i in items:
                med = rules.medicine(db, i['product_id'])
                rules.check_registration(med, i, 'dispensed')
            if f.get('decision') == 'reject':
                if not f.get('reason', '').strip():
                    raise RuleError('Give the reason for rejecting the prescription.')
                db.execute("UPDATE prescriptions SET status='Rejected', reject_reason=?, verified_by=?, "
                           "verified_at=datetime('now','localtime') WHERE id=?", (f['reason'].strip(), g.user['id'], rid))
                audit('rx_reject', f"{rx['ref_no']}: {f['reason'].strip()}", db)
                msg = 'Prescription rejected.'
            else:
                warn = safety_warnings(db, rx, items)
                if any(w.startswith('The patient is under 12') for w in warn):
                    raise RuleError(warn[-1])
                if warn and not f.get('reason', '').strip():
                    raise RuleError('Read the warnings and give your reason to go on (or reject the prescription).')
                notes = '; '.join(warn) + (f" | Reason: {f['reason'].strip()}" if warn else '')
                db.execute("UPDATE prescriptions SET status='Verified', verified_by=?, verified_at=datetime('now','localtime'), "
                           'interventions=?, counselling=? WHERE id=?',
                           (g.user['id'], notes, f.get('counselling', '').strip(), rid))
                audit('rx_verify', rx['ref_no'] + (f' with warnings: {notes}' if warn else ''), db)
                msg = 'Prescription verified. It can now be dispensed.'
        flash(msg, 'ok')
    except RuleError as e:
        flash(str(e), 'error')
    return redirect(url_for('pharmacy.rx_view', rid=rid))


@bp.route('/prescriptions/<int:rid>/cancel', methods=['POST'])
@requires('pharmacy', 'dispense')
def rx_cancel(rid):
    try:
        with tx() as db:
            rx = _rx(db, rid)
            if rx['status'] not in ('Registered', 'Verified'):
                raise RuleError('Only a prescription that was not dispensed yet can be cancelled.')
            if not request.form.get('reason', '').strip():
                raise RuleError('A reason is required to cancel.')
            db.execute("UPDATE prescriptions SET status='Cancelled', reject_reason=? WHERE id=?",
                       (request.form['reason'].strip(), rid))
            audit('rx_cancel', f"{rx['ref_no']}: {request.form['reason'].strip()}", db)
        flash('Prescription cancelled.', 'ok')
    except RuleError as e:
        flash(str(e), 'error')
    return redirect(url_for('pharmacy.rx_view', rid=rid))


@bp.route('/prescriptions/<int:rid>/dispense', methods=['POST'])
@requires('dispense')
def rx_dispense(rid):
    """Hand over the medicines: a normal sale (FEFO batches, receipt, payment) linked to the prescription.
    Controlled medicines need a second person to confirm with their password (FR-020-02)."""
    f = request.form
    try:
        with tx() as db:
            claim_once(db)
            _need_pharmacist()
            rx = _rx(db, rid)
            items = _rx_items(db, rid)
            if rx['status'] not in ('Verified', 'Partially dispensed'):
                raise RuleError('A pharmacist must verify the prescription before dispensing.')
            if valid_until(rx, items) < today():
                raise RuleError('This prescription is no longer valid.')
            lines, now = [], {}
            for i in items:
                left = rules._q(i['qty'] - i['dispensed_qty'])
                v = f.get(f'd_{i["id"]}', '').strip()
                q = parse_qty(v, 'Quantity', allow_zero=True) if v else 0
                if q > left + rules.EPS:
                    raise RuleError(f"{i['name']}: only {core.fmt_qty(left)} {i['unit']} is left on the prescription.")
                if q > rules.EPS:
                    lines.append((i['product_id'], q))
                    now[i['id']] = q
            if not lines:
                raise RuleError('Enter the quantity to dispense.')
            checker = None
            if any(i['rx_class'] in CONTROLLED and i['id'] in now for i in items):
                cu = db.execute('SELECT * FROM users WHERE username=? AND active=1', (f.get('check_user', '').strip(),)).fetchone()
                if not cu or cu['id'] == g.user['id'] or not check_password_hash(cu['password_hash'], f.get('check_password', '')):
                    raise RuleError('Controlled medicine: a second person must confirm with their own username and '
                                    'password.')
                checker = cu['id']
            subtotal = sum(rnd(now[i['id']] * i['price']) for i in items if i['id'] in now)
            paid = parse_money(f.get('tendered'), 'Amount paid') if f.get('tendered', '').strip() else subtotal
            customer = f.get('customer_id', type=int) or rx['patient_customer']
            sid = rules.create_sale(db, lines, 0, paid, f.get('method') or 'Cash', customer, g.user['id'], today(),
                                    allow_credit=can('credit'), reference=f.get('reference', ''),
                                    prescription={'id': rid, 'products': {i['product_id'] for i in items}})
            for iid, q in now.items():
                db.execute('UPDATE rx_items SET dispensed_qty=ROUND(dispensed_qty+?, 3) WHERE id=?', (q, iid))
            left = db.execute('SELECT COUNT(*) FROM rx_items WHERE prescription_id=? AND dispensed_qty < qty-0.0005',
                              (rid,)).fetchone()[0]
            db.execute('UPDATE prescriptions SET status=?, counselling=CASE WHEN ?<>\'\' THEN ? ELSE counselling END '
                       'WHERE id=?', ('Partially dispensed' if left else 'Dispensed', f.get('counselling', '').strip(),
                                      f.get('counselling', '').strip(), rid))
            did = db.execute('INSERT INTO dispensings(prescription_id, sale_id, pharmacist_id, checked_by) VALUES (?,?,?,?)',
                             (rid, sid, g.user['id'], checker)).lastrowid
            audit('rx_dispense', f"{rx['ref_no']} sale {sid}" + (f' checked by user {checker}' if checker else ''), db)
        flash('Dispensed. Print the labels and the receipt.', 'ok')
        return redirect(url_for('pharmacy.label', did=did))
    except (RuleError, ValueError) as e:
        flash(str(e) if isinstance(e, RuleError) else 'Check the quantities.', 'error')
    return redirect(url_for('pharmacy.rx_view', rid=rid))


@bp.route('/labels/<int:did>')
@requires('pharmacy', 'dispense')
def label(did):
    """Dispensing labels, one per medicine (FR-007-08)."""
    db = get_db()
    d = db.execute('SELECT d.*, u.full_name AS pharmacist, s.receipt_no, s.date FROM dispensings d JOIN users u '
                   'ON u.id=d.pharmacist_id JOIN sales s ON s.id=d.sale_id WHERE d.id=?', (did,)).fetchone()
    if not d:
        abort(404)
    rx = _rx(db, d['prescription_id'])
    items = db.execute('SELECT si.qty, p.name, p.unit, m.strength, m.dosage_form, m.storage, ri.dose, ri.frequency, '
                       'ri.duration, ri.instructions FROM sale_items si JOIN products p ON p.id=si.product_id '
                       'LEFT JOIN medicines m ON m.product_id=p.id LEFT JOIN rx_items ri ON ri.prescription_id=? AND '
                       'ri.product_id=si.product_id WHERE si.sale_id=?', (rx['id'], d['sale_id'])).fetchall()
    return render_template('pharmacy/label.html', d=d, rx=rx, items=items)


# ---------------------------------------------------------------- expiry, batch status, recalls, disposal
@bp.route('/expiry')
@requires('inventory')
def expiry():
    db = get_db()
    bands = sorted({int(x) for x in setting('expiry_alert_days').split(',') if x.strip().isdigit()} or {30, 60, 90})
    t = today()
    horizon = (date.today() + timedelta(days=bands[-1])).isoformat()
    base = ('SELECT b.*, p.code, p.name, p.unit FROM stock_batches b JOIN products p ON p.id=b.product_id '
            'WHERE b.branch_id=? AND b.remaining>0.0005 ')
    expired = db.execute(base + "AND b.expiry IS NOT NULL AND b.expiry<>'' AND b.expiry<? ORDER BY b.expiry",
                         (branch_id(), t)).fetchall()
    soon = db.execute(base + "AND b.expiry>=? AND b.expiry<=? ORDER BY b.expiry", (branch_id(), t, horizon)).fetchall()
    blocked = db.execute(base + "AND b.status<>'Available' ORDER BY b.status, p.name", (branch_id(),)).fetchall()
    regs = db.execute("SELECT m.*, p.code, p.name FROM medicines m JOIN products p ON p.id=m.product_id WHERE "
                      "p.active=1 AND m.reg_expiry IS NOT NULL AND m.reg_expiry<>'' AND m.reg_expiry<=? ORDER BY m.reg_expiry",
                      ((date.today() + timedelta(days=60)).isoformat(),)).fetchall()

    def band(b):
        days = (date.fromisoformat(b['expiry']) - date.today()).days
        return next((x for x in bands if days <= x), bands[-1]), days
    return render_template('pharmacy/expiry.html', expired=expired, soon=[(b, *band(b)) for b in soon], blocked=blocked,
                           regs=regs, bands=bands, today=t)


@bp.route('/batches/<int:bid>/status', methods=['POST'])
@requires('adjust')
def batch_status(bid):
    """Quarantine, release or mark a batch damaged. Only a pharmacist releases quarantined medicine (BR-013)."""
    new = request.form.get('status')
    try:
        with tx() as db:
            b = db.execute('SELECT * FROM stock_batches WHERE id=? AND branch_id=?', (bid, branch_id())).fetchone()
            if not b:
                raise RuleError('Batch not found in this branch.')
            if new not in BATCH_STATUSES:
                raise RuleError('Choose a status.')
            if b['status'] == 'Recalled':
                raise RuleError('This batch is recalled. Return it to the supplier or dispose of it.')
            if b['status'] == 'Quarantined' and new == 'Available' and not can('dispense'):
                raise RuleError('Only a pharmacist can release a quarantined batch after inspecting it.')
            if not request.form.get('reason', '').strip():
                raise RuleError('A reason is required.')
            db.execute('UPDATE stock_batches SET status=? WHERE id=?', (new, bid))
            audit('batch_status', f"batch {bid} {b['status']} -> {new}: {request.form['reason'].strip()}", db)
        flash(f'Batch #{bid} is now {new}.', 'ok')
    except RuleError as e:
        flash(str(e), 'error')
    return redirect(url_for('inventory.batch', bid=bid))


@bp.route('/recalls', methods=['GET', 'POST'])
@requires('adjust', 'dispense')
def recalls():
    db = get_db()
    f = request.form
    if request.method == 'POST':
        try:
            pid, bno = f.get('product_id', type=int), f.get('batch_no', '').strip()
            if not pid or not bno:
                raise RuleError('Choose the product and type the batch number.')
            with tx() as db:
                claim_once(db)
                ref = next_no(db, 'RC', 'recalls', 'ref_no')
                rid = db.execute('INSERT INTO recalls(ref_no, product_id, batch_no, source, recall_class, reference, action, '
                                 'date, user_id) VALUES (?,?,?,?,?,?,?,?,?)',
                                 (ref, pid, bno, f.get('source', ''), f.get('recall_class', ''), f.get('reference', '').strip(),
                                  f.get('action', '').strip(), f.get('date') or today(), g.user['id'])).lastrowid
                n = db.execute("UPDATE stock_batches SET status='Recalled' WHERE product_id=? AND batch_no=? COLLATE NOCASE",
                               (pid, bno)).rowcount
                audit('recall', f'{ref} product {pid} batch {bno}: {n} batches blocked', db)
            flash(f'Recall registered. {n} stock batch(es) with this batch number are blocked from sale in every branch.'
                  if n else 'Recall registered. No stock with this batch number was found.', 'ok')
            return redirect(url_for('pharmacy.recall', rid=rid))
        except RuleError as e:
            flash(str(e), 'error')
    rows = db.execute('SELECT r.*, p.name, p.code FROM recalls r JOIN products p ON p.id=r.product_id '
                      'ORDER BY r.status DESC, r.id DESC').fetchall()
    products = db.execute('SELECT id, code, name FROM products WHERE id IN (SELECT product_id FROM medicines) '
                          'OR id IN (SELECT product_id FROM stock_batches WHERE batch_no<>\'\') ORDER BY name').fetchall()
    return render_template('pharmacy/recalls.html', rows=rows, products=products, today=today())


def recall_trace(db, r):
    """Every sale (customer and patient, with phone) that received the recalled batch, in all branches (FR-010-09)."""
    return db.execute(
        "SELECT s.id, s.receipt_no, s.date, s.branch_id, br.name AS branch, c.name AS customer, c.phone AS customer_phone, "
        "pt.name AS patient, pt.phone AS patient_phone, ROUND(SUM(-bm.qty), 3) AS qty, "
        "(SELECT COUNT(*) FROM recall_contacts rc WHERE rc.recall_id=? AND rc.sale_id=s.id) AS contacts "
        "FROM stock_batches b JOIN batch_moves bm ON bm.batch_id=b.id AND bm.qty<0 JOIN stock_moves m ON m.id=bm.move_id "
        "AND m.move_type='sale' JOIN sales s ON s.id=m.ref_id JOIN branches br ON br.id=s.branch_id "
        "LEFT JOIN customers c ON c.id=s.customer_id LEFT JOIN prescriptions rx ON rx.id=s.prescription_id "
        "LEFT JOIN patients pt ON pt.id=rx.patient_id WHERE b.product_id=? AND b.batch_no=? COLLATE NOCASE "
        "AND s.status='Completed' GROUP BY s.id ORDER BY s.date, s.id", (r['id'], r['product_id'], r['batch_no'])).fetchall()


@bp.route('/recalls/<int:rid>', methods=['GET', 'POST'])
@requires('adjust', 'dispense')
def recall(rid):
    db = get_db()
    r = db.execute('SELECT r.*, p.name, p.code, p.unit FROM recalls r JOIN products p ON p.id=r.product_id WHERE r.id=?',
                   (rid,)).fetchone()
    if not r:
        return fail('Recall not found.', 'pharmacy.recalls')
    if request.method == 'POST':
        try:
            with tx() as db:
                if request.form.get('close'):
                    db.execute("UPDATE recalls SET status='Closed' WHERE id=?", (rid,))
                    audit('recall_close', r['ref_no'], db)
                else:
                    if not request.form.get('note', '').strip():
                        raise RuleError('Write what was done (e.g. phoned, no answer).')
                    db.execute('INSERT INTO recall_contacts(recall_id, sale_id, note, user_id) VALUES (?,?,?,?)',
                               (rid, request.form.get('sale_id', type=int), request.form['note'].strip(), g.user['id']))
            flash('Saved.', 'ok')
        except RuleError as e:
            flash(str(e), 'error')
        return redirect(url_for('pharmacy.recall', rid=rid))
    batches = db.execute('SELECT b.*, br.name AS branch FROM stock_batches b JOIN branches br ON br.id=b.branch_id '
                         'WHERE b.product_id=? AND b.batch_no=? COLLATE NOCASE ORDER BY b.branch_id, b.id',
                         (r['product_id'], r['batch_no'])).fetchall()
    trace = recall_trace(db, r)
    if request.args.get('export'):
        from reports import xlsx_bytes
        cols = [('Receipt', 'receipt_no', 'text'), ('Date', 'date', 'text'), ('Branch', 'branch', 'text'),
                ('Customer', 'customer', 'text'), ('Customer phone', 'customer_phone', 'text'),
                ('Patient', 'patient', 'text'), ('Patient phone', 'patient_phone', 'text'), ('Qty', 'qty', 'qty')]
        data = xlsx_bytes(f"Recall {r['ref_no']}", f"{r['name']} batch {r['batch_no']}", cols,
                          [dict(t) for t in trace], {})
        return Response(data, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                        headers={'Content-Disposition': f'attachment; filename="recall-{r["ref_no"]}.xlsx"'})
    contacts = db.execute('SELECT rc.*, u.username, s.receipt_no FROM recall_contacts rc LEFT JOIN users u ON u.id=rc.user_id '
                          'LEFT JOIN sales s ON s.id=rc.sale_id WHERE rc.recall_id=? ORDER BY rc.id', (rid,)).fetchall()
    return render_template('pharmacy/recall.html', r=r, batches=batches, trace=trace, contacts=contacts)


@bp.route('/dispose/<int:bid>', methods=['GET', 'POST'])
@requires('adjust')
def dispose(bid):
    """Destroy expired, damaged or recalled stock with two witnesses, one a pharmacist (FR-010-10)."""
    db = get_db()
    b = db.execute('SELECT b.*, p.name, p.code, p.unit, m.rx_class FROM stock_batches b JOIN products p ON p.id=b.product_id '
                   'LEFT JOIN medicines m ON m.product_id=p.id WHERE b.id=? AND b.branch_id=?', (bid, branch_id())).fetchone()
    if not b:
        return fail('Batch not found in this branch.', 'pharmacy.expiry')
    users = db.execute('SELECT * FROM users WHERE active=1 ORDER BY full_name').fetchall()
    f = request.form
    if request.method == 'POST':
        try:
            qty = parse_qty(f.get('qty'))
            w1, w2 = f.get('witness1', type=int), f.get('witness2', type=int)
            if not w1 or not w2 or w1 == w2:
                raise RuleError('Choose two different witnesses.')
            ws = [u for u in users if u['id'] in (w1, w2)]
            if len(ws) != 2 or not any(not licence_problem(u) for u in ws):
                raise RuleError('At least one witness must be a pharmacist with a valid licence.')
            if not f.get('method') or not f.get('reason', '').strip():
                raise RuleError('Choose the disposal method and give the reason.')
            cert = _upload('certificate', 'disposal')
            with tx() as db:
                claim_once(db)
                b2 = db.execute('SELECT * FROM stock_batches WHERE id=?', (bid,)).fetchone()
                ref = next_no(db, 'DS', 'disposals', 'ref_no')
                m = rules.move_stock(db, b['product_id'], -qty, 'disposal', g.user['id'], 'disposal', None, ref,
                                     reason=f"{f['method']}: {f['reason'].strip()}", batches=[b2])
                db.execute('INSERT INTO disposals(ref_no, batch_id, product_id, qty, method, reason, witness1, witness2, '
                           'certificate, date, move_id, user_id, branch_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (ref, bid, b['product_id'], qty, f['method'], f['reason'].strip(), w1, w2, cert,
                            f.get('date') or today(), m.id, g.user['id'], branch_id()))
                audit('disposal', f'{ref} batch {bid} qty {qty}', db)
            flash(f'Disposal {ref} recorded and written off.', 'ok')
            return redirect(url_for('pharmacy.disposals'))
        except RuleError as e:
            flash(str(e), 'error')
    return render_template('pharmacy/dispose.html', b=b, users=users, methods=DISPOSAL_METHODS, today=today(), f=f,
                           CONTROLLED=CONTROLLED)


@bp.route('/disposals')
@requires('adjust', 'inventory')
def disposals():
    rows = get_db().execute(
        'SELECT d.*, p.name, p.unit, b.batch_no, b.expiry, w1.full_name AS w1, w2.full_name AS w2, u.username, m.rx_class '
        'FROM disposals d JOIN products p ON p.id=d.product_id JOIN stock_batches b ON b.id=d.batch_id '
        'JOIN users w1 ON w1.id=d.witness1 JOIN users w2 ON w2.id=d.witness2 LEFT JOIN users u ON u.id=d.user_id '
        'LEFT JOIN medicines m ON m.product_id=d.product_id WHERE d.branch_id=? ORDER BY d.id DESC LIMIT 1000',
        (branch_id(),)).fetchall()
    return render_template('pharmacy/disposals.html', rows=rows, CONTROLLED=CONTROLLED)


@bp.route('/disposals/<int:did>/certificate')
@requires('adjust', 'inventory')
def certificate(did):
    d = get_db().execute('SELECT certificate FROM disposals WHERE id=? AND branch_id=?', (did, branch_id())).fetchone()
    if not d or not d['certificate']:
        abort(404)
    return send_file(os.path.join(_files_dir(), os.path.basename(d['certificate'])), max_age=0)


# ---------------------------------------------------------------- controlled-medicine register
@bp.route('/controlled')
@requires('dispense', 'reports')
def controlled():
    """Register of one narcotic / psychotropic medicine: every movement with the running balance (FR-020-01).
    It is built from the stock movements, which can never be edited: a correction is always a new movement."""
    db = get_db()
    meds = db.execute('SELECT p.id, p.code, p.name, p.unit, m.rx_class, m.strength FROM medicines m JOIN products p '
                      'ON p.id=m.product_id WHERE m.rx_class IN (?,?) ORDER BY p.name', CONTROLLED).fetchall()
    pid = request.args.get('pid', type=int) or (meds[0]['id'] if meds else None)
    start = request.args.get('start') or date.today().replace(day=1).isoformat()
    end = request.args.get('end') or today()
    rows, opening, med = [], 0, None
    if pid:
        med = next((m for m in meds if m['id'] == pid), None)
        if not med:
            return fail('Choose a controlled medicine.', 'pharmacy.controlled')
        before = db.execute('SELECT balance FROM stock_moves WHERE product_id=? AND branch_id=? AND date(at)<? '
                            'ORDER BY id DESC LIMIT 1', (pid, branch_id(), start)).fetchone()
        opening = before['balance'] if before else 0
        rows = db.execute(
            "SELECT m.*, u.full_name AS by_user, pt.name AS patient, pr.name AS prescriber, ph.full_name AS pharmacist, "
            "ck.full_name AS checker, rx.ref_no AS rx_ref, rx.form_serial FROM stock_moves m LEFT JOIN users u ON u.id=m.user_id "
            "LEFT JOIN sales s ON m.ref_type='sale' AND s.id=m.ref_id LEFT JOIN prescriptions rx ON rx.id=s.prescription_id "
            "LEFT JOIN patients pt ON pt.id=rx.patient_id LEFT JOIN prescribers pr ON pr.id=rx.prescriber_id "
            "LEFT JOIN dispensings d ON d.sale_id=s.id LEFT JOIN users ph ON ph.id=d.pharmacist_id "
            "LEFT JOIN users ck ON ck.id=d.checked_by WHERE m.product_id=? AND m.branch_id=? AND m.qty<>0 "
            "AND date(m.at) BETWEEN ? AND ? ORDER BY m.id", (pid, branch_id(), start, end)).fetchall()
    return render_template('pharmacy/controlled.html', meds=meds, med=med, pid=pid, rows=rows, opening=opening,
                           start=start, end=end, labels=rules.MOVE_LABELS)


# ---------------------------------------------------------------- shared with other pages
def save_medicine(db, pid, form):
    """Medicine details from the product form (pharmacy on). An empty generic name and class OTC with nothing
    else filled in means the product is general goods: its medicine row is removed."""
    v = {k: (form.get(k) or '').strip() for k in ('generic_name', 'brand', 'manufacturer', 'dosage_form', 'strength',
                                                  'rx_class', 'storage', 'reg_no', 'reg_expiry')}
    if not form.get('is_medicine'):
        db.execute('DELETE FROM medicines WHERE product_id=?', (pid,))
        return
    if v['rx_class'] not in RX_CLASSES:
        raise RuleError('Choose the regulatory class of the medicine.')
    if not v['generic_name']:
        raise RuleError('The generic name of the medicine is required.')
    old = db.execute('SELECT * FROM medicines WHERE product_id=?', (pid,)).fetchone()
    db.execute('INSERT INTO medicines(product_id, generic_name, brand, manufacturer, dosage_form, strength, rx_class, '
               'storage, reg_no, reg_expiry) VALUES (?,?,?,?,?,?,?,?,?,?) ON CONFLICT(product_id) DO UPDATE SET '
               'generic_name=excluded.generic_name, brand=excluded.brand, manufacturer=excluded.manufacturer, '
               'dosage_form=excluded.dosage_form, strength=excluded.strength, rx_class=excluded.rx_class, '
               'storage=excluded.storage, reg_no=excluded.reg_no, reg_expiry=excluded.reg_expiry',
               (pid, v['generic_name'], v['brand'], v['manufacturer'], v['dosage_form'], v['strength'], v['rx_class'],
                v['storage'], v['reg_no'], v['reg_expiry'] or None))
    if not old or old['rx_class'] != v['rx_class']:
        audit('medicine_class', f"product {pid}: {old['rx_class'] if old else '-'} -> {v['rx_class']}", db)


def duplicate_medicine(db, pid, form):
    """A possible duplicate: same generic name, strength, dosage form and manufacturer (FR-001-18)."""
    if not form.get('is_medicine') or not (form.get('generic_name') or '').strip():
        return None
    return db.execute('SELECT p.name, p.code FROM medicines m JOIN products p ON p.id=m.product_id WHERE '
                      'lower(m.generic_name)=lower(?) AND lower(m.strength)=lower(?) AND lower(m.dosage_form)=lower(?) '
                      'AND lower(m.manufacturer)=lower(?) AND m.product_id<>?',
                      tuple((form.get(k) or '').strip() for k in ('generic_name', 'strength', 'dosage_form',
                                                                   'manufacturer')) + (pid or 0,)).fetchone()


def alerts():
    """Counts for the warning bar: batches expired with stock, and batches expiring within the first alert band."""
    db = get_db()
    bands = sorted(int(x) for x in setting('expiry_alert_days').split(',') if x.strip().isdigit()) or [30]
    t = today()
    r = db.execute("SELECT SUM(expiry<?) AS expired, SUM(expiry>=? AND expiry<=?) AS soon FROM stock_batches WHERE "
                   "branch_id=? AND remaining>0.0005 AND expiry IS NOT NULL AND expiry<>''",
                   (t, t, (date.today() + timedelta(days=bands[0])).isoformat(), branch_id())).fetchone()
    return {'expired': r['expired'] or 0, 'soon': r['soon'] or 0, 'days': bands[0]}


def form_lists():
    return {'rx_classes': RX_CLASSES, 'rx_labels': RX_CLASS_LABELS, 'dosage_forms': DOSAGE_FORMS, 'storage': STORAGE}
