"""AG Inventory: start the web app, login and shared page setup.

Run for development:  python app.py
Run in the shop:      python serve.py   (uses the Waitress server)
"""
import os
import secrets

from flask import Blueprint, Flask, flash, g, redirect, render_template, request, send_file, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

import core
from core import (BASE_DIR, audit, can, check_csrf, close_db, csrf_field, fmt_money, fmt_qty,
                  get_db, load_user, login_required, money_input, once_field, setting, tx)

auth = Blueprint('auth', __name__)


@auth.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        name = request.form.get('username', '').strip()
        # find the user's business first: each business has its own database
        login = core.get_platform().execute('SELECT business_id FROM logins WHERE username=?', (name,)).fetchone()
        u = None
        if login and core.use_business(login['business_id']):
            u = get_db().execute('SELECT * FROM users WHERE username=?', (name,)).fetchone()
        if u and u['active'] and check_password_hash(u['password_hash'], request.form.get('password', '')):
            session.clear()
            session['uid'], session['bid'] = u['id'], login['business_id']
            g.user = u
            audit('login', u['username'])
            nxt = request.args.get('next', '')
            return redirect(nxt if nxt.startswith('/') and not nxt.startswith('//') else url_for('dash.index'))
        flash('Wrong username or password, or the account is deactivated.', 'error')
    return render_template('login.html')


@auth.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('auth.login'))


@auth.route('/password', methods=['GET', 'POST'])
@login_required
def password():
    if request.method == 'POST':
        cur, new = request.form.get('current', ''), request.form.get('new', '')
        if not check_password_hash(g.user['password_hash'], cur):
            flash('Current password is wrong.', 'error')
        elif new == cur:
            flash('Choose a password that is different from the current one.', 'error')
        elif len(new) < 6 or new != request.form.get('confirm'):
            flash('New password must be at least 6 characters and match the confirmation.', 'error')
        else:
            with tx() as db:
                db.execute('UPDATE users SET password_hash=?, must_change=0 WHERE id=?',
                           (generate_password_hash(new), g.user['id']))
                audit('password_change', g.user['username'], db)
            flash('Password changed.', 'ok')
            return redirect(url_for('dash.index'))
    return render_template('password.html')


@auth.route('/branch', methods=['POST'])
@login_required
def switch_branch():
    """Work in another branch (only one the user is assigned to)."""
    bid = request.form.get('branch', type=int)
    if any(b['id'] == bid for b in g.branches):
        session['branch'] = bid
    nxt = request.form.get('next', '')
    return redirect(nxt if nxt.startswith('/') and not nxt.startswith('//') else url_for('dash.index'))


@auth.route('/logo')
@login_required
def logo():
    path = core.logo_path()
    if not path:
        return ('', 404)
    with open(path, 'rb') as f:
        data = f.read()
    return app_response(data, path)


def app_response(data, path):
    from flask import Response
    kind = 'image/png' if path.lower().endswith('.png') else 'image/jpeg'
    return Response(data, mimetype=kind, headers={'Cache-Control': 'no-cache'})


@auth.route('/manual')
@login_required
def manual():
    """The full user manual on one printable page (same text as the Help pop-up)."""
    return render_template('manual.html')


def low_stock_count():
    return get_db().execute('SELECT COUNT(*) FROM pv WHERE active=1 AND in_branch AND stock<=min_stock '
                            'AND min_stock>0').fetchone()[0]


def create_app(database=None, testing=False):
    app = Flask(__name__)
    data_dir = os.path.join(BASE_DIR, 'data')
    os.makedirs(data_dir, exist_ok=True)
    key_file = os.path.join(data_dir, 'secret.key')
    if not os.path.exists(key_file):
        with open(key_file, 'w') as f:
            f.write(secrets.token_hex(32))
    with open(key_file) as f:
        secret = f.read().strip()
    app.config.update(
        SECRET_KEY=secret,
        DATABASE=database or os.environ.get('AGINV_DB', os.path.join(data_dir, 'shop.db')),
        BACKUP_DIR=os.path.join(BASE_DIR, 'backups'),
        TESTING=testing,
        MAX_CONTENT_LENGTH=200 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        SEND_FILE_MAX_AGE_DEFAULT=0,  # browsers re-check CSS/JS, so updates show without clearing the cache
    )
    core.backup_before_upgrade(app.config['DATABASE'], app.config['BACKUP_DIR'])
    # one small platform file lists the businesses; each business has its own database file
    app.config['PLATFORM_DB'] = os.path.join(os.path.dirname(os.path.abspath(app.config['DATABASE'])),
                                             'platform.db')
    core.init_platform(app.config['PLATFORM_DB'], app.config['DATABASE'])

    import admin, contacts, expenses, inventory, products, purchases, reports, sales
    for bp in (auth, reports.dash, admin.bp, contacts.bp, expenses.bp, inventory.bp, products.bp,
               purchases.bp, reports.bp, sales.bp):
        app.register_blueprint(bp)

    app.teardown_appcontext(close_db)

    @app.before_request
    def before():
        if request.endpoint == 'static':
            return
        load_user()
        check_csrf()
        # a password reset to the default must be changed before anything else
        if g.user and g.user['must_change'] and request.endpoint not in ('auth.password', 'auth.logout', 'auth.logo'):
            flash('Your password was reset. Please choose a new password to continue.', 'error')
            return redirect(url_for('auth.password'))
        if g.user and not app.config['TESTING']:
            try:
                core.daily_backup()
            except Exception as e:  # never block the shop because of a backup problem
                app.logger.warning('Daily backup failed: %s', e)

    app.jinja_env.filters.update(money=fmt_money, qty=fmt_qty, money_input=money_input)
    app.jinja_env.globals.update(csrf=csrf_field, once=once_field, can=can, setting=setting)

    @app.context_processor
    def ctx():
        low = low_stock_count() if g.get('user') and can('inventory') else 0
        return {'low_stock': low, 'currency': setting('currency')}

    @app.errorhandler(403)
    def forbidden(_e):
        return render_template('error.html', title='Not allowed',
                               msg='Your role does not have permission for this page.'), 403

    @app.errorhandler(404)
    def notfound(_e):
        return render_template('error.html', title='Not found', msg='This page does not exist.'), 404

    @app.errorhandler(400)
    def badreq(e):
        return render_template('error.html', title='Request refused', msg=e.description), 400

    return app


if __name__ == '__main__':
    create_app().run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)), debug=False)
