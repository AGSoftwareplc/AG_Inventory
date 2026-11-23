"""Shared helpers: database access, money/quantity parsing, login and permission checks."""
import os
import secrets
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from functools import wraps

from flask import abort, current_app, flash, g, redirect, request, session, url_for
from markupsafe import Markup, escape

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- permissions
PERMS = {
    'pos': 'Sell at POS',
    'sales_view': 'View sales history',
    'discount': 'Give sale discounts',
    'returns': 'Process customer returns',
    'sales_cancel': 'Cancel sales',
    'payments': 'Record customer / supplier payments',
    'customers': 'Manage customers',
    'products': 'Manage products and categories',
    'inventory': 'View stock and movements',
    'adjust': 'Adjust stock and run stocktakes',
    'purchases': 'Create and manage purchases',
    'receive': 'Receive purchases and supplier returns',
    'suppliers': 'Manage suppliers',
    'expenses': 'Manage expenses',
    'reports': 'View reports, profit and dashboard figures',
}
ADMIN = 'Administrator'
ROLES = [ADMIN, 'Sales Attendant', 'Inventory Manager']
DEFAULT_ROLE_PERMS = {
    'Sales Attendant': ['pos', 'sales_view', 'customers', 'inventory', 'payments'],
    'Inventory Manager': ['products', 'inventory', 'adjust', 'purchases', 'receive',
                          'suppliers', 'sales_view'],
}
DEFAULT_SETTINGS = {
    'shop_name': 'My Shop',
    'shop_address': '',
    'shop_phone': '',
    'currency': 'ETB',
    'payment_methods': 'Cash,Bank transfer,Mobile banking',
    'receipt_footer': 'Thank you for shopping with us!',
    'allow_negative': '0',
    'last_backup': '',
}
EXPENSE_CATEGORIES = ['Rent', 'Electricity', 'Water', 'Transportation', 'Salary',
                      'Maintenance', 'Other']


class RuleError(Exception):
    """A business rule was broken; the message is shown to the user."""


# ---------------------------------------------------------------- database
def db_path():
    return current_app.config['DATABASE']


def connect(path):
    con = sqlite3.connect(path, isolation_level=None, timeout=15)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys = ON')
    con.execute('PRAGMA journal_mode = WAL')
    return con


def get_db():
    if 'db' not in g:
        g.db = connect(db_path())
    return g.db


def close_db(_exc=None):
    con = g.pop('db', None)
    if con is not None:
        con.close()


@contextmanager
def tx(db=None):
    """All-or-nothing database transaction (write lock taken at start)."""
    db = db or get_db()
    db.execute('BEGIN IMMEDIATE')
    try:
        yield db
        db.execute('COMMIT')
    except BaseException:
        db.execute('ROLLBACK')
        raise


def init_db(path):
    """Create tables and default data if missing. Safe to run on every start."""
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    con = connect(path)
    with open(os.path.join(BASE_DIR, 'schema.sql'), encoding='utf-8') as f:
        con.executescript(f.read())
    for k, v in DEFAULT_SETTINGS.items():
        con.execute('INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)', (k, v))
    if not con.execute('SELECT 1 FROM role_permissions LIMIT 1').fetchone():
        for role, perms in DEFAULT_ROLE_PERMS.items():
            con.executemany('INSERT INTO role_permissions VALUES (?, ?)', [(role, p) for p in perms])
    if not con.execute('SELECT 1 FROM expense_categories LIMIT 1').fetchone():
        con.executemany('INSERT INTO expense_categories(name) VALUES (?)',
                        [(n,) for n in EXPENSE_CATEGORIES])
    if not con.execute('SELECT 1 FROM users LIMIT 1').fetchone():
        from werkzeug.security import generate_password_hash
        con.execute('INSERT INTO users(username, full_name, password_hash, role) VALUES (?,?,?,?)',
                    ('admin', 'Administrator', generate_password_hash('admin123'), ADMIN))
    con.close()


def setting(key):
    if 'settings' not in g:
        g.settings = {r['key']: r['value'] for r in get_db().execute('SELECT * FROM settings')}
    return g.settings.get(key, DEFAULT_SETTINGS.get(key, ''))


def payment_methods():
    return [m.strip() for m in setting('payment_methods').split(',') if m.strip()]


def audit(action, details='', db=None):
    (db or get_db()).execute('INSERT INTO audit_log(user_id, action, details) VALUES (?,?,?)',
                             (g.user['id'] if g.get('user') else None, action, details))


def next_no(db, prefix, table, col):
    """Next document number like S-000042. Call inside tx() so numbers never collide."""
    row = db.execute(f'SELECT MAX(id) AS m FROM {table}').fetchone()
    n = (row['m'] or 0) + 1
    while True:
        no = f'{prefix}-{n:06d}'
        if not db.execute(f'SELECT 1 FROM {table} WHERE {col}=?', (no,)).fetchone():
            return no
        n += 1


def backup_db(dest_dir=None, keep=30):
    """Copy the live database safely to backups/ (SQLite online backup)."""
    dest_dir = dest_dir or current_app.config['BACKUP_DIR']
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, f"shop-{datetime.now():%Y%m%d-%H%M%S}.db")
    src = connect(db_path())
    out = sqlite3.connect(dest)
    src.backup(out)
    out.close()
    src.close()
    files = sorted(f for f in os.listdir(dest_dir) if f.startswith('shop-') and f.endswith('.db'))
    for old in files[:-keep]:
        os.remove(os.path.join(dest_dir, old))
    return dest


def daily_backup():
    today = date.today().isoformat()
    if setting('last_backup') == today:
        return
    backup_db()
    get_db().execute("UPDATE settings SET value=? WHERE key='last_backup'", (today,))
    g.pop('settings', None)


def restore_db(uploaded_path):
    con = sqlite3.connect(uploaded_path)
    try:
        ok = con.execute("SELECT 1 FROM sqlite_master WHERE name='products'").fetchone()
        if not ok:
            raise RuleError('This file is not an AG Inventory backup.')
    except sqlite3.DatabaseError:
        raise RuleError('This file is not a valid database backup.')
    finally:
        con.close()
    backup_db()  # keep a copy of the current data first
    close_db()
    shutil.copyfile(uploaded_path, db_path())
    for ext in ('-wal', '-shm'):
        if os.path.exists(db_path() + ext):
            os.remove(db_path() + ext)


# ---------------------------------------------------------------- numbers
def rnd(x):
    """Round a number of cents to a whole cent (half up)."""
    return int(Decimal(str(x)).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def parse_money(s, field='Amount', allow_zero=True):
    s = (s or '').replace(',', '').strip() or '0'
    try:
        d = Decimal(s)
    except InvalidOperation:
        raise RuleError(f'{field} must be a number.')
    if d < 0 or (not allow_zero and d == 0):
        raise RuleError(f'{field} must be greater than zero.' if not allow_zero
                        else f'{field} cannot be negative.')
    return int((d * 100).quantize(Decimal('1'), rounding=ROUND_HALF_UP))


def parse_qty(s, field='Quantity', allow_zero=False, allow_negative=False):
    s = (s or '').replace(',', '').strip()
    try:
        q = round(float(Decimal(s)), 3)
    except (InvalidOperation, ValueError):
        raise RuleError(f'{field} must be a number.')
    if (q < 0 and not allow_negative) or (q == 0 and not allow_zero):
        raise RuleError(f'{field} must be greater than zero.')
    return q


def fmt_money(cents):
    cents = rnd(cents or 0)
    sign = '-' if cents < 0 else ''
    cents = abs(cents)
    return f'{sign}{cents // 100:,}.{cents % 100:02d}'


def fmt_qty(q):
    q = round(q or 0, 3)
    return f'{q:,.3f}'.rstrip('0').rstrip('.') if q != int(q) else f'{int(q):,}'


def money_input(cents):
    """Value for an <input> (no thousands separator)."""
    return fmt_money(cents).replace(',', '')


def today():
    return date.today().isoformat()


def date_range():
    """Read ?start=&end= from the query string (defaults: this month)."""
    t = date.today()
    start = request.args.get('start') or t.replace(day=1).isoformat()
    end = request.args.get('end') or t.isoformat()
    return start, end


# ---------------------------------------------------------------- auth
def can(perm):
    u = g.get('user')
    if not u:
        return False
    return u['role'] == ADMIN or perm in g.perms


def login_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if not g.get('user'):
            return redirect(url_for('auth.login', next=request.path))
        return view(*a, **kw)
    return wrapped


def requires(*perms):
    """Allow the view if the user has ANY of the given permissions."""
    def deco(view):
        @wraps(view)
        def wrapped(*a, **kw):
            if not g.get('user'):
                return redirect(url_for('auth.login', next=request.path))
            if not any(can(p) for p in perms):
                abort(403)
            return view(*a, **kw)
        return wrapped
    return deco


def admin_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if not g.get('user'):
            return redirect(url_for('auth.login', next=request.path))
        if g.user['role'] != ADMIN:
            abort(403)
        return view(*a, **kw)
    return wrapped


def load_user():
    g.user, g.perms = None, set()
    uid = session.get('uid')
    if uid:
        u = get_db().execute('SELECT * FROM users WHERE id=? AND active=1', (uid,)).fetchone()
        if u:
            g.user = u
            g.perms = {r['perm'] for r in get_db().execute(
                'SELECT perm FROM role_permissions WHERE role=?', (u['role'],))}
        else:
            session.clear()


# ---------------------------------------------------------------- forms
def csrf_token():
    if 'csrf' not in session:
        session['csrf'] = secrets.token_hex(16)
    return session['csrf']


def csrf_field():
    return Markup(f'<input type="hidden" name="csrf_token" value="{escape(csrf_token())}">')


def once_field():
    """Hidden one-time token: the same form can be processed only once (no double sales)."""
    return Markup(f'<input type="hidden" name="once" value="{secrets.token_hex(16)}">')


def check_csrf():
    if request.method == 'POST':
        sent = request.form.get('csrf_token') or request.headers.get('X-CSRF-Token')
        if not sent or sent != session.get('csrf'):
            abort(400, 'Form expired. Please go back, reload the page and try again.')


def claim_once(db):
    """Inside tx(): mark this form's one-time token as used, or refuse a repeat."""
    tok = request.form.get('once')
    if not tok:
        return
    try:
        db.execute('INSERT INTO used_tokens(token) VALUES (?)', (tok,))
    except sqlite3.IntegrityError:
        raise RuleError('This form was already submitted. Nothing was recorded twice.')


def fail(err, endpoint=None, **kw):
    flash(str(err), 'error')
    return redirect(url_for(endpoint, **kw) if endpoint else (request.referrer or url_for('dash.index')))
