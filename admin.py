"""Administrator pages: users, role permissions, settings, backup/restore, audit log."""
import os
import sqlite3
import tempfile

from flask import Blueprint, flash, g, redirect, render_template, request, send_file, url_for
from werkzeug.security import generate_password_hash

import core
from core import (ADMIN, DEFAULT_SETTINGS, PERMS, ROLES, RuleError, admin_required, audit, get_db, tx)

bp = Blueprint('admin', __name__, url_prefix='/admin')


@bp.route('/users')
@admin_required
def users():
    rows = get_db().execute("SELECT u.*, (SELECT GROUP_CONCAT(b.name, ', ') FROM user_branches ub JOIN branches b "
                            "ON b.id=ub.branch_id WHERE ub.user_id=u.id) AS branch_names FROM users u "
                            "ORDER BY u.active DESC, u.username").fetchall()
    return render_template('admin/users.html', rows=rows)


@bp.route('/users/new', methods=['GET', 'POST'])
@bp.route('/users/<int:uid>', methods=['GET', 'POST'])
@admin_required
def user_edit(uid=None):
    db = get_db()
    u = db.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone() if uid else None
    if request.method == 'POST':
        f = request.form
        username, name, role = f.get('username', '').strip(), f.get('full_name', '').strip(), f.get('role')
        pw = f.get('password', '')
        active = 1 if f.get('active') else 0
        try:
            if not username or not name or role not in ROLES:
                raise RuleError('Username, full name and role are required.')
            if (not u or pw) and len(pw) < 6:
                raise RuleError('Password must be at least 6 characters.')
            if u and u['id'] == g.user['id'] and (role != ADMIN or not active):
                raise RuleError('You cannot remove your own administrator access.')
            if core.username_taken(username, g.business['id']):  # usernames are unique across businesses
                raise RuleError('This username is already taken.')
            branch_ids = [int(b) for b in f.getlist('branch') if b.isdigit()]
            if role != ADMIN and not branch_ids:
                open_ids = [r['id'] for r in db.execute('SELECT id FROM branches WHERE active=1')]
                if len(open_ids) != 1:
                    raise RuleError('Tick at least one branch this user works in.')
                branch_ids = open_ids  # a business with one branch: that branch
            with tx() as db:
                if u:
                    db.execute('UPDATE users SET username=?, full_name=?, role=?, active=? WHERE id=?',
                               (username, name, role, active, uid))
                    if pw:
                        db.execute('UPDATE users SET password_hash=? WHERE id=?',
                                   (generate_password_hash(pw), uid))
                else:
                    uid = db.execute('INSERT INTO users(username, full_name, password_hash, role, active) '
                                     'VALUES (?,?,?,?,?)', (username, name, generate_password_hash(pw),
                                                            role, active)).lastrowid
                db.execute('DELETE FROM user_branches WHERE user_id=?', (uid,))
                for b in (branch_ids or [1]):
                    db.execute('INSERT OR IGNORE INTO user_branches(user_id, branch_id) SELECT ?, id FROM branches '
                               'WHERE id=?', (uid, b))
                core.register_login(u['username'] if u else None, username, g.business['id'])
                audit('user_save', f'{username} role={role} active={active}', db)
            flash('User saved.', 'ok')
            return redirect(url_for('admin.users'))
        except sqlite3.IntegrityError:
            flash('This username is already taken.', 'error')
        except RuleError as e:
            flash(str(e), 'error')
    branches = db.execute('SELECT * FROM branches ORDER BY active DESC, id').fetchall()
    mine = {r['branch_id'] for r in db.execute('SELECT branch_id FROM user_branches WHERE user_id=?', (uid,))} \
        if uid else {1}
    if request.method == 'POST':
        mine = {int(b) for b in request.form.getlist('branch') if b.isdigit()}
    return render_template('admin/user_edit.html', u=u, roles=ROLES, f=request.form, branches=branches, mine=mine)


@bp.route('/branches')
@admin_required
def branches():
    rows = get_db().execute("SELECT b.*, (SELECT COUNT(*) FROM user_branches ub WHERE ub.branch_id=b.id) AS users, "
                            "(SELECT COUNT(*) FROM sales s WHERE s.branch_id=b.id) AS sales FROM branches b "
                            "ORDER BY b.active DESC, b.id").fetchall()
    return render_template('admin/branches.html', rows=rows)


@bp.route('/branches/new', methods=['GET', 'POST'])
@bp.route('/branches/<int:bid>', methods=['GET', 'POST'])
@admin_required
def branch_edit(bid=None):
    db = get_db()
    b = db.execute('SELECT * FROM branches WHERE id=?', (bid,)).fetchone() if bid else None
    if bid and not b:
        return core.fail('Branch not found.', 'admin.branches')
    if request.method == 'POST':
        f = request.form
        code, name = f.get('code', '').strip().upper(), f.get('name', '').strip()
        active = 1 if f.get('active') or (b and b['id'] == 1) else 0  # the Main branch stays active
        try:
            if not code or not name:
                raise RuleError('Branch code and name are required.')
            if not code.replace('-', '').isalnum() or len(code) > 10:
                raise RuleError('The branch code must be letters and digits only, up to 10 (it is used in document numbers).')
            with tx() as db:
                vals = (code, name, f.get('address', '').strip(), f.get('phone', '').strip(), active)
                if b:
                    db.execute('UPDATE branches SET code=?, name=?, address=?, phone=?, active=? WHERE id=?', vals + (bid,))
                else:
                    bid = db.execute('INSERT INTO branches(code, name, address, phone, active) VALUES (?,?,?,?,?)',
                                     vals).lastrowid
                audit('branch_save', f'{code} {name} active={active}', db)
            flash('Branch saved.', 'ok')
            return redirect(url_for('admin.branches'))
        except sqlite3.IntegrityError:
            flash('Another branch already uses this code.', 'error')
        except RuleError as e:
            flash(str(e), 'error')
    return render_template('admin/branch_edit.html', b=b, f=request.form)


@bp.route('/roles', methods=['GET', 'POST'])
@admin_required
def roles():
    db = get_db()
    others = [r for r in ROLES if r != ADMIN]
    if request.method == 'POST':
        with tx() as db:
            db.execute('DELETE FROM role_permissions')
            for role in others:
                for p in PERMS:
                    if request.form.get(f'{role}|{p}'):
                        db.execute('INSERT INTO role_permissions VALUES (?,?)', (role, p))
            audit('permissions_change', '', db)
        flash('Permissions saved.', 'ok')
        return redirect(url_for('admin.roles'))
    have = {(r['role'], r['perm']) for r in db.execute('SELECT * FROM role_permissions')}
    return render_template('admin/roles.html', roles=others, perms=PERMS, have=have)


@bp.route('/settings', methods=['GET', 'POST'])
@admin_required
def settings():
    if request.method == 'POST':
        logo = request.files.get('logo')
        if logo and logo.filename and os.path.splitext(logo.filename)[1].lower() not in ('.png', '.jpg', '.jpeg'):
            flash('The logo must be a PNG or JPG picture. Nothing was saved.', 'error')
            return redirect(url_for('admin.settings'))
        if logo and logo.filename:
            logo.stream.seek(0, os.SEEK_END)
            too_big = logo.stream.tell() > 1024 * 1024
            logo.stream.seek(0)
            if too_big:
                flash('The logo must be 1 MB or smaller. Nothing was saved.', 'error')
                return redirect(url_for('admin.settings'))
        with tx() as db:
            for k in DEFAULT_SETTINGS:
                if k in ('last_backup', 'logo_file'):
                    continue
                v = request.form.get(k, '')
                if k == 'allow_negative':
                    v = '1' if v else '0'
                db.execute('UPDATE settings SET value=? WHERE key=?', (v.strip(), k))
            audit('settings_change', '', db)
            if logo and logo.filename:
                ext = os.path.splitext(logo.filename)[1].lower()
                folder = os.path.join(os.path.dirname(os.path.abspath(core.platform_path())), 'logos')
                os.makedirs(folder, exist_ok=True)
                name = f"business-{g.business['id']}{ext}"
                for old_ext in ('.png', '.jpg', '.jpeg'):  # one logo per business
                    old_file = os.path.join(folder, f"business-{g.business['id']}{old_ext}")
                    if os.path.exists(old_file):
                        os.remove(old_file)
                logo.save(os.path.join(folder, name))
                db.execute("UPDATE settings SET value=? WHERE key='logo_file'", (name,))
            elif request.form.get('remove_logo'):
                db.execute("UPDATE settings SET value='' WHERE key='logo_file'")
        g.pop('settings', None)
        flash('Business profile saved.', 'ok')
        return redirect(url_for('admin.settings'))
    bdir = core.current_app.config['BACKUP_DIR']
    backups = sorted((f for f in os.listdir(bdir) if f.endswith('.db')), reverse=True) if os.path.isdir(bdir) else []
    return render_template('admin/settings.html', backups=backups[:30], backup_dir=bdir)


@bp.route('/backup', methods=['POST'])
@admin_required
def backup():
    path = core.backup_db()
    audit('backup', os.path.basename(path))
    return send_file(path, as_attachment=True, download_name=os.path.basename(path))


@bp.route('/restore', methods=['POST'])
@admin_required
def restore():
    f = request.files.get('file')
    if not f or not f.filename:
        flash('Choose a backup file.', 'error')
        return redirect(url_for('admin.settings'))
    fd, tmp = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    try:
        f.save(tmp)
        core.restore_db(tmp)
        bcon = core.connect(core.db_path())
        core.sync_logins(core.get_platform(), g.business['id'], bcon)
        bcon.close()
        flash('Backup restored. Please log in again.', 'ok')
        return redirect(url_for('auth.logout'))
    except RuleError as e:
        flash(str(e), 'error')
        return redirect(url_for('admin.settings'))
    finally:
        os.remove(tmp)


@bp.route('/audit')
@admin_required
def audit_log():
    rows = get_db().execute('SELECT a.*, u.username FROM audit_log a LEFT JOIN users u ON u.id=a.user_id '
                            'ORDER BY a.id DESC LIMIT 500').fetchall()
    return render_template('admin/audit.html', rows=rows)
