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
    """The database file of the business being worked on (one SQLite file per business)."""
    return g.get('business_db') or current_app.config['DATABASE']


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
    for key in ('db', 'pdb'):
        con = g.pop(key, None)
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


def init_db(path, seed_starters=True):
    """Create a business database's tables and default data if missing. Safe to run on every start."""
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    con = connect(path)
    with open(os.path.join(BASE_DIR, 'schema.sql'), encoding='utf-8') as f:
        con.executescript(f.read())
    migrate(con)
    for k, v in DEFAULT_SETTINGS.items():
        con.execute('INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)', (k, v))
    if not con.execute('SELECT 1 FROM role_permissions LIMIT 1').fetchone():
        for role, perms in DEFAULT_ROLE_PERMS.items():
            con.executemany('INSERT INTO role_permissions VALUES (?, ?)', [(role, p) for p in perms])
    if not con.execute('SELECT 1 FROM expense_categories LIMIT 1').fetchone():
        con.executemany('INSERT INTO expense_categories(name) VALUES (?)',
                        [(n,) for n in EXPENSE_CATEGORIES])
    if not con.execute('SELECT 1 FROM branches LIMIT 1').fetchone():
        con.execute("INSERT INTO branches(id, code, name) VALUES (1, 'MAIN', 'Main branch')")
    if seed_starters:
        seed_users(con)
    # every user works in at least one branch (users from before branches existed: the Main branch)
    con.execute('INSERT OR IGNORE INTO user_branches(user_id, branch_id) SELECT id, 1 FROM users '
                'WHERE id NOT IN (SELECT user_id FROM user_branches)')
    con.close()


# One starter login per role: (username, full name, role, starter password).
# Created once per database; afterwards the administrator manages them in Users.
# Starter logins. Only add to the END of this list: each database remembers how many it has
# seeded (settings 'seeded_role_users'), so new entries are added to existing shops once.
STARTER_USERS = [
    ('admin', 'Administrator', ADMIN, 'admin123'),
    ('cashier', 'Cashier', 'Sales Attendant', 'cashier123'),
    ('storekeeper', 'Store keeper', 'Inventory Manager', 'store123'),
    ('cashier2', 'Cashier 2', 'Sales Attendant', 'sales222'),
    ('cashier3', 'Cashier 3', 'Sales Attendant', 'sales333'),
]


def seed_users(con):
    from werkzeug.security import generate_password_hash
    row = con.execute("SELECT value FROM settings WHERE key='seeded_role_users'").fetchone()
    done = 0 if row is None else (3 if row['value'] == '1' else int(row['value']))  # '1' = first 3 (v1.1)
    for username, name, role, pw in STARTER_USERS[done:]:
        con.execute('INSERT OR IGNORE INTO users(username, full_name, password_hash, role) VALUES (?,?,?,?)',
                    (username, name, generate_password_hash(pw), role))
    if done != len(STARTER_USERS):
        con.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('seeded_role_users', ?)",
                    (str(len(STARTER_USERS)),))


def uses_starter_password(user):
    """True while a starter login still has its starter password."""
    from werkzeug.security import check_password_hash
    for username, _n, _r, pw in STARTER_USERS:
        if user['username'].lower() == username:
            return check_password_hash(user['password_hash'], pw)
    return False


# Columns added after version 1.0: (table, column, definition). Applied once to older databases.
MIGRATIONS = [
    ('expenses', 'reversal_of', 'INTEGER REFERENCES expenses(id)'),
    # version 2: every branch-level record belongs to a branch (existing data: the Main branch, id 1)
    ('sales', 'branch_id', 'INTEGER NOT NULL DEFAULT 1'),
    ('purchases', 'branch_id', 'INTEGER NOT NULL DEFAULT 1'),
    ('stock_moves', 'branch_id', 'INTEGER NOT NULL DEFAULT 1'),
    ('stocktakes', 'branch_id', 'INTEGER NOT NULL DEFAULT 1'),
    ('expenses', 'branch_id', 'INTEGER NOT NULL DEFAULT 1'),
    ('returns', 'branch_id', 'INTEGER NOT NULL DEFAULT 1'),
    ('audit_log', 'branch_id', 'INTEGER'),
]


def migrate(con):
    for table, col, definition in MIGRATIONS:
        cols = {r['name'] for r in con.execute(f'PRAGMA table_info({table})')}
        if col not in cols:
            con.execute(f'ALTER TABLE {table} ADD COLUMN {col} {definition}')


# ---------------------------------------------------------------- businesses (platform)
# Each business has its own SQLite file, so one business can never read another's data.
# A small platform file lists the businesses and which business each username belongs to.
PLATFORM_SCHEMA = """
CREATE TABLE IF NOT EXISTS businesses (
  id INTEGER PRIMARY KEY,
  code TEXT NOT NULL UNIQUE COLLATE NOCASE,
  name TEXT NOT NULL,
  db_file TEXT NOT NULL,                 -- relative to the platform file's folder (or absolute)
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE TABLE IF NOT EXISTS logins (
  username TEXT PRIMARY KEY COLLATE NOCASE,   -- usernames are unique across all businesses
  business_id INTEGER NOT NULL REFERENCES businesses(id),
  is_super INTEGER NOT NULL DEFAULT 0         -- Super Admin: manages the businesses
);
"""


def platform_path():
    return current_app.config['PLATFORM_DB']


def business_file(platform, row):
    return os.path.join(os.path.dirname(os.path.abspath(platform)), row['db_file'])


def sync_logins(pcon, business_id, bcon):
    """Make every user of a business findable at login (new usernames only)."""
    for u in bcon.execute('SELECT username FROM users'):
        pcon.execute('INSERT OR IGNORE INTO logins(username, business_id) VALUES (?, ?)',
                     (u['username'], business_id))


def init_platform(platform, first_db):
    """Create the platform file. The first time, the existing shop becomes business 1 and its
    administrators become Super Admins. Then bring every business database up to date."""
    pcon = connect(platform)
    pcon.executescript(PLATFORM_SCHEMA)
    if not pcon.execute('SELECT 1 FROM businesses LIMIT 1').fetchone():
        try:
            rel = os.path.relpath(os.path.abspath(first_db), os.path.dirname(os.path.abspath(platform)))
        except ValueError:  # another drive on Windows
            rel = os.path.abspath(first_db)
        init_db(first_db)
        bcon = connect(first_db)
        name = (bcon.execute("SELECT value FROM settings WHERE key='shop_name'").fetchone() or ['My Shop'])[0]
        pcon.execute("INSERT INTO businesses(id, code, name, db_file) VALUES (1, 'MAIN', ?, ?)", (name, rel))
        sync_logins(pcon, 1, bcon)
        for u in bcon.execute('SELECT username FROM users WHERE role=?', (ADMIN,)):
            pcon.execute('UPDATE logins SET is_super=1 WHERE username=?', (u['username'],))
        bcon.close()
    for b in pcon.execute('SELECT * FROM businesses').fetchall():
        path = business_file(platform, b)
        init_db(path, seed_starters=(b['id'] == 1))
        bcon = connect(path)
        sync_logins(pcon, b['id'], bcon)
        bcon.close()
    pcon.close()


def get_platform():
    if 'pdb' not in g:
        g.pdb = connect(platform_path())
    return g.pdb


def create_business(name, code, admin_username, admin_name, admin_password):
    """New business with its own database file, a Main branch and its first administrator."""
    from werkzeug.security import generate_password_hash
    name, code, admin_username = name.strip(), code.strip().upper(), admin_username.strip()
    if not name or not code or not admin_username or not admin_name.strip():
        raise RuleError('Business name, code, admin username and admin name are required.')
    if not code.isalnum() or len(code) > 10:
        raise RuleError('The business code must be letters and digits only, up to 10.')
    if len(admin_password) < 6:
        raise RuleError('The admin password must be at least 6 characters.')
    platform = platform_path()
    p = get_platform()
    if p.execute('SELECT 1 FROM businesses WHERE code=?', (code,)).fetchone():
        raise RuleError('Another business already uses this code.')
    if p.execute('SELECT 1 FROM logins WHERE username=?', (admin_username,)).fetchone():
        raise RuleError('This username is already taken.')
    bid = (p.execute('SELECT MAX(id) FROM businesses').fetchone()[0] or 0) + 1
    rel = f'biz-{bid}.db'
    path = os.path.join(os.path.dirname(os.path.abspath(platform)), rel)
    init_db(path, seed_starters=False)
    bcon = connect(path)
    bcon.execute("INSERT OR REPLACE INTO settings(key, value) VALUES ('seeded_role_users', ?)",
                 (str(len(STARTER_USERS)),))
    bcon.execute("UPDATE settings SET value=? WHERE key='shop_name'", (name,))
    uid = bcon.execute('INSERT INTO users(username, full_name, password_hash, role) VALUES (?,?,?,?)',
                       (admin_username, admin_name.strip(), generate_password_hash(admin_password), ADMIN)).lastrowid
    bcon.execute('INSERT INTO user_branches(user_id, branch_id) VALUES (?, 1)', (uid,))
    bcon.close()
    p.execute('INSERT INTO businesses(id, code, name, db_file) VALUES (?,?,?,?)', (bid, code, name, rel))
    p.execute('INSERT INTO logins(username, business_id) VALUES (?, ?)', (admin_username, bid))
    return bid


def username_taken(username, business_id):
    """True if another business already uses this username."""
    r = get_platform().execute('SELECT business_id FROM logins WHERE username=?', (username,)).fetchone()
    return bool(r) and r['business_id'] != business_id


def register_login(old, new, business_id):
    """Keep the platform's username list in step with a business's users."""
    p = get_platform()
    if old and old.lower() != new.lower():
        p.execute('UPDATE logins SET username=? WHERE username=? AND business_id=?', (new, old, business_id))
    p.execute('INSERT OR IGNORE INTO logins(username, business_id) VALUES (?, ?)', (new, business_id))


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


def use_business(bid):
    """Point this request at one business's database. Returns the business row, or None."""
    b = get_platform().execute('SELECT * FROM businesses WHERE id=? AND active=1', (bid,)).fetchone()
    if b:
        if g.get('business_db') != business_file(platform_path(), b):
            db = g.pop('db', None)
            if db is not None:
                db.close()
            g.pop('settings', None)
        g.business, g.business_db = b, business_file(platform_path(), b)
    return b


def load_user():
    g.user, g.perms, g.business, g.is_super = None, set(), None, False
    uid, bid = session.get('uid'), session.get('bid', 1)
    if uid and use_business(bid):
        u = get_db().execute('SELECT * FROM users WHERE id=? AND active=1', (uid,)).fetchone()
        if u:
            g.user = u
            g.perms = {r['perm'] for r in get_db().execute(
                'SELECT perm FROM role_permissions WHERE role=?', (u['role'],))}
            r = get_platform().execute('SELECT is_super FROM logins WHERE username=? AND business_id=?',
                                       (u['username'], bid)).fetchone()
            g.is_super = bool(r and r['is_super'])
            return
    if uid:
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
