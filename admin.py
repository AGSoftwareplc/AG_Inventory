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
    rows = get_db().execute('SELECT * FROM users ORDER BY active DESC, username').fetchall()
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
                    db.execute('INSERT INTO user_branches(user_id, branch_id) VALUES (?, 1)', (uid,))
                core.register_login(u['username'] if u else None, username, g.business['id'])
                audit('user_save', f'{username} role={role} active={active}', db)
            flash('User saved.', 'ok')
            return redirect(url_for('admin.users'))
        except sqlite3.IntegrityError:
            flash('This username is already taken.', 'error')
        except RuleError as e:
            flash(str(e), 'error')
    return render_template('admin/user_edit.html', u=u, roles=ROLES, f=request.form)


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
        with tx() as db:
            for k in DEFAULT_SETTINGS:
                if k == 'last_backup':
                    continue
                v = request.form.get(k, '')
                if k == 'allow_negative':
                    v = '1' if v else '0'
                db.execute('UPDATE settings SET value=? WHERE key=?', (v.strip(), k))
            audit('settings_change', '', db)
        g.pop('settings', None)
        flash('Settings saved.', 'ok')
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
