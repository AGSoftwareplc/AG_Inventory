"""Operating expenses and expense categories."""
import sqlite3

from flask import Blueprint, flash, g, redirect, render_template, request, url_for

from core import (RuleError, audit, claim_once, date_range, get_db, next_no, parse_money,
                  payment_methods, requires, today, tx)

bp = Blueprint('expenses', __name__, url_prefix='/expenses')


@bp.route('/', methods=['GET', 'POST'])
@requires('expenses')
def index():
    db = get_db()
    if request.method == 'POST':
        try:
            amount = parse_money(request.form.get('amount'), allow_zero=False)
            cat = request.form.get('category_id', type=int)
            if not cat:
                raise RuleError('Choose a category.')
            with tx() as db:
                claim_once(db)
                ref = next_no(db, 'E', 'expenses', 'ref_no')
                db.execute('INSERT INTO expenses(ref_no, category_id, description, amount, date, method, '
                           'user_id) VALUES (?,?,?,?,?,?,?)',
                           (ref, cat, request.form.get('description', '').strip(), amount,
                            request.form.get('date') or today(), request.form.get('method', 'Cash'),
                            g.user['id']))
            flash(f'Expense {ref} recorded.', 'ok')
        except RuleError as e:
            flash(str(e), 'error')
        return redirect(url_for('expenses.index'))
    start, end = date_range()
    cat = request.args.get('cat', type=int)
    sql = ('SELECT e.*, c.name AS cat, u.username, (SELECT r.ref_no FROM expenses r WHERE r.reversal_of=e.id) '
           'AS reversed_by FROM expenses e JOIN expense_categories c ON '
           'c.id=e.category_id LEFT JOIN users u ON u.id=e.user_id WHERE e.date BETWEEN ? AND ?')
    args = [start, end]
    if cat:
        sql += ' AND e.category_id=?'
        args.append(cat)
    rows = db.execute(sql + ' ORDER BY e.date DESC, e.id DESC', args).fetchall()
    cats = db.execute('SELECT * FROM expense_categories ORDER BY name').fetchall()
    return render_template('expenses/index.html', rows=rows, cats=cats, start=start, end=end, cat=cat,
                           total=sum(r['amount'] for r in rows), methods=payment_methods(), today=today())


@bp.route('/<int:eid>/reverse', methods=['POST'])
@requires('expenses')
def reverse(eid):
    """Correct a wrong expense with a negative entry (expenses are never deleted)."""
    reason = request.form.get('reason', '').strip()
    try:
        if not reason:
            raise RuleError('A reason is required to reverse an expense.')
        with tx() as db:
            claim_once(db)
            e = db.execute('SELECT * FROM expenses WHERE id=?', (eid,)).fetchone()
            if not e:
                raise RuleError('Expense not found.')
            if e['reversal_of'] or e['amount'] < 0:
                raise RuleError('A reversal entry cannot be reversed.')
            if db.execute('SELECT 1 FROM expenses WHERE reversal_of=?', (eid,)).fetchone():
                raise RuleError('This expense is already reversed.')
            ref = next_no(db, 'E', 'expenses', 'ref_no')
            db.execute('INSERT INTO expenses(ref_no, category_id, description, amount, date, method, user_id, '
                       'reversal_of) VALUES (?,?,?,?,?,?,?,?)',
                       (ref, e['category_id'], f"Reversal of {e['ref_no']}: {reason}", -e['amount'], today(),
                        e['method'], g.user['id'], eid))
            audit('expense_reverse', f"{e['ref_no']}: {reason}", db)
        flash(f"Expense {e['ref_no']} reversed by {ref}.", 'ok')
    except RuleError as err:
        flash(str(err), 'error')
    return redirect(url_for('expenses.index'))


@bp.route('/categories', methods=['POST'])
@requires('expenses')
def add_category():
    name = request.form.get('name', '').strip()
    if name:
        try:
            with tx() as db:
                db.execute('INSERT INTO expense_categories(name) VALUES (?)', (name,))
                audit('expense_category', name, db)
            flash('Category added.', 'ok')
        except sqlite3.IntegrityError:
            flash('This category already exists.', 'error')
    return redirect(url_for('expenses.index'))
