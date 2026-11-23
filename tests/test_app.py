"""Tests for the critical business rules and the acceptance criteria.

Run:  python -m unittest discover tests
"""
import io
import os
import re
import sys
import tempfile
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
import core  # noqa: E402
from core import connect  # noqa: E402


class ShopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.dbfile = os.path.join(self.tmp, 'test.db')
        self.app = create_app(self.dbfile, testing=True)
        self.app.config['BACKUP_DIR'] = os.path.join(self.tmp, 'backups')
        self.c = self.app.test_client()
        self.login('admin', 'admin123')

    # ---------------------------------------------------------------- helpers
    def db(self):
        return connect(self.dbfile)

    def one(self, sql, *args):
        con = self.db()
        r = con.execute(sql, args).fetchone()
        con.close()
        return r

    def csrf(self):
        with self.c.session_transaction() as s:
            s.setdefault('csrf', 'testtoken')
            return s['csrf']

    def post(self, url, data=None, once=True, **kw):
        data = dict(data or {})
        data['csrf_token'] = self.csrf()
        if once and 'once' not in data:
            data['once'] = os.urandom(8).hex()
        return self.c.post(url, data=data, **kw)

    def login(self, user, pw):
        self.c.get('/logout')
        r = self.post('/login', {'username': user, 'password': pw}, once=False)
        self.assertEqual(r.status_code, 302, 'login failed')

    def product(self, code='P1', price='150', cost='0', opening='0', min_stock='0'):
        self.post('/products/categories', {'name': 'Drinks'}, once=False)
        cat = self.one("SELECT id FROM categories WHERE name='Drinks'")['id']
        self.post('/products/new', {'code': code, 'name': 'Item ' + code, 'category_id': cat, 'unit': 'pcs',
                                    'price': price, 'cost': cost, 'opening': opening, 'min_stock': min_stock},
                  once=False)
        return self.one('SELECT * FROM products WHERE code=?', code)

    def supplier(self):
        self.post('/suppliers/new', {'name': 'ABC Wholesale', 'phone': '0911'}, once=False)
        return self.one('SELECT id FROM suppliers ORDER BY id DESC')['id']

    def purchase(self, pid, qty, cost, discount='0', confirm=True):
        sup = self.supplier()
        data = {'supplier_id': sup, 'date': '2026-11-23', 'pid': [pid], 'qty': [qty], 'cost': [cost],
                'discount': discount}
        if confirm:
            data['confirm'] = '1'
        self.post('/purchases/new', data)
        return self.one('SELECT * FROM purchases ORDER BY id DESC')

    def receive_all(self, pur):
        con = self.db()
        items = con.execute('SELECT * FROM purchase_items WHERE purchase_id=?', (pur['id'],)).fetchall()
        con.close()
        return self.post(f"/purchases/{pur['id']}/receive",
                         {f"r_{i['id']}": str(i['qty'] - i['received_qty']) for i in items})

    def sell(self, items, tendered='', discount='', customer='', dtype='amt', once=None):
        data = {'items': ','.join(f'{p}:{q}' for p, q in items), 'tendered': tendered, 'discount': discount,
                'discount_type': dtype, 'method': 'Cash', 'customer_id': customer}
        if once:
            data['once'] = once
        return self.post('/sales/pos', data)

    def stock(self, pid):
        return self.one('SELECT stock, avg_cost FROM products WHERE id=?', pid)

    def stocked_product(self):
        """Product P1, price 150, stock 20 at average cost 110 (two purchases)."""
        p = self.product()
        pur = self.purchase(p['id'], '10', '100')
        self.receive_all(pur)
        pur = self.purchase(p['id'], '10', '130', discount='100')  # 1300-100 = 1200 -> 120 each
        self.receive_all(pur)
        return p

    # ---------------------------------------------------------------- AC-001
    def test_create_product_with_category(self):
        p = self.product(opening='5', cost='80')
        self.assertEqual(p['category_id'], self.one("SELECT id FROM categories WHERE name='Drinks'")['id'])
        self.assertEqual(p['price'], 15000)
        self.assertEqual(p['stock'], 5)
        self.assertEqual(p['avg_cost'], 8000)
        r = self.post('/products/new', {'code': 'p1', 'name': 'dup', 'price': '1'}, once=False)
        self.assertIn(b'already uses this code', r.data if r.status_code == 200 else self.c.get(r.location).data)
        self.assertEqual(self.one("SELECT COUNT(*) n FROM products")['n'], 1)

    # ---------------------------------------------------------------- AC-002, AC-008, partial receiving
    def test_purchase_receiving_and_weighted_average(self):
        p = self.product()
        pur = self.purchase(p['id'], '10', '100', confirm=False)
        self.assertEqual(pur['status'], 'Draft')
        self.assertEqual(self.stock(p['id'])['stock'], 0)  # draft does not touch stock
        self.post(f"/purchases/{pur['id']}/confirm")
        item = self.one('SELECT id FROM purchase_items WHERE purchase_id=?', pur['id'])
        self.post(f"/purchases/{pur['id']}/receive", {f"r_{item['id']}": '4'})
        self.assertEqual(self.one('SELECT status FROM purchases WHERE id=?', pur['id'])['status'], 'Partially Received')
        self.assertEqual(self.stock(p['id'])['stock'], 4)
        self.post(f"/purchases/{pur['id']}/receive", {f"r_{item['id']}": '7'})  # more than ordered: refused
        self.assertEqual(self.stock(p['id'])['stock'], 4)
        self.post(f"/purchases/{pur['id']}/receive", {f"r_{item['id']}": '6'})
        self.assertEqual(self.one('SELECT status FROM purchases WHERE id=?', pur['id'])['status'], 'Received')
        s = self.stock(p['id'])
        self.assertEqual((s['stock'], s['avg_cost']), (10, 10000))
        pur2 = self.purchase(p['id'], '10', '130', discount='100')
        self.receive_all(pur2)
        s = self.stock(p['id'])
        self.assertEqual(s['stock'], 20)
        self.assertAlmostEqual(s['avg_cost'], 11000)  # (10*100 + 10*120) / 20

    def test_cancelled_purchase_cannot_receive(self):
        p = self.product()
        pur = self.purchase(p['id'], '5', '10')
        self.post(f"/purchases/{pur['id']}/cancel", {'reason': 'wrong order'})
        self.receive_all(pur)
        self.assertEqual(self.stock(p['id'])['stock'], 0)

    def test_purchase_return_and_payment(self):
        p = self.stocked_product()
        pur = self.one('SELECT * FROM purchases ORDER BY id DESC')
        item = self.one('SELECT id FROM purchase_items WHERE purchase_id=?', pur['id'])
        self.post(f"/purchases/{pur['id']}/return", {'item_id': item['id'], 'qty': '2', 'reason': 'damaged'})
        pur = self.one('SELECT * FROM purchases WHERE id=?', pur['id'])
        self.assertEqual(pur['returned_value'], 24000)
        s = self.stock(p['id'])
        self.assertEqual(s['stock'], 18)
        self.assertAlmostEqual(s['avg_cost'], (20 * 11000 - 2 * 12000) / 18)
        self.post(f"/purchases/{pur['id']}/pay", {'amount': '2000', 'method': 'Cash'})  # too much
        self.assertEqual(self.one('SELECT paid FROM purchases WHERE id=?', pur['id'])['paid'], 0)
        self.post(f"/purchases/{pur['id']}/pay", {'amount': '960', 'method': 'Cash'})
        self.assertEqual(self.one('SELECT paid FROM purchases WHERE id=?', pur['id'])['paid'], 96000)

    # ---------------------------------------------------------------- AC-003, AC-004, AC-005, AC-017
    def test_sale_totals_discount_change(self):
        p = self.stocked_product()
        r = self.sell([(p['id'], 3)], tendered='500', discount='30')
        self.assertEqual(r.status_code, 302)
        s = self.one('SELECT * FROM sales ORDER BY id DESC')
        self.assertEqual((s['subtotal'], s['discount'], s['total'], s['paid'], s['change_due']),
                         (45000, 3000, 42000, 42000, 8000))
        self.assertEqual(self.stock(p['id'])['stock'], 17)
        self.assertEqual(self.one('SELECT cost FROM sale_items WHERE sale_id=?', s['id'])['cost'], 11000)
        receipt = self.c.get(f"/sales/{s['id']}/receipt").data
        self.assertIn(b'420.00', receipt)
        self.assertIn(b'80.00', receipt)

    def test_percent_discount(self):
        p = self.stocked_product()
        self.sell([(p['id'], 2)], tendered='300', discount='10', dtype='pct')
        s = self.one('SELECT * FROM sales ORDER BY id DESC')
        self.assertEqual((s['discount'], s['total']), (3000, 27000))

    def test_cannot_oversell(self):
        p = self.stocked_product()
        self.sell([(p['id'], 21)], tendered='99999')
        self.assertIsNone(self.one('SELECT * FROM sales'))
        self.assertEqual(self.stock(p['id'])['stock'], 20)
        self.assertEqual(self.one('SELECT COUNT(*) n FROM stock_moves WHERE move_type="sale"')['n'], 0)

    def test_double_submit_records_one_sale(self):
        p = self.stocked_product()
        self.sell([(p['id'], 1)], tendered='150', once='same-token')
        self.sell([(p['id'], 1)], tendered='150', once='same-token')
        self.assertEqual(self.one('SELECT COUNT(*) n FROM sales')['n'], 1)
        self.assertEqual(self.stock(p['id'])['stock'], 19)

    def test_credit_sale_needs_customer_then_payment(self):
        p = self.stocked_product()
        self.sell([(p['id'], 2)], tendered='100')
        self.assertIsNone(self.one('SELECT * FROM sales'))
        self.post('/customers/new', {'name': 'Almaz', 'phone': '0922'}, once=False)
        cid = self.one('SELECT id FROM customers')['id']
        self.sell([(p['id'], 2)], tendered='100', customer=cid)
        s = self.one('SELECT * FROM sales')
        self.assertEqual((s['total'], s['paid']), (30000, 10000))
        self.post(f"/sales/{s['id']}/pay", {'amount': '200', 'method': 'Cash'})
        self.assertEqual(self.one('SELECT paid FROM sales')['paid'], 30000)

    def test_return_cancel_and_profit(self):
        p = self.stocked_product()
        self.sell([(p['id'], 3)], tendered='420', discount='30')
        sale = self.one('SELECT * FROM sales')
        item = self.one('SELECT id FROM sale_items WHERE sale_id=?', sale['id'])
        self.post(f"/sales/{sale['id']}/return", {'item_id': item['id'], 'qty': '1', 'reason': 'faulty'})
        self.assertEqual(self.one('SELECT refunded FROM sales')['refunded'], 14000)  # 150 * 420/450
        self.assertEqual(self.stock(p['id'])['stock'], 18)
        self.post('/expenses/', {'category_id': 1, 'amount': '20', 'date': self._today()})
        import rules
        con = self.db()
        pr = rules.profit(con, '2000-01-01', '2100-01-01')
        con.close()
        self.assertEqual(pr['net_sales'], 28000)
        self.assertEqual(pr['cogs'], 22000)
        self.assertEqual(pr['gross_profit'], 6000)
        self.assertEqual(pr['net_profit'], 4000)
        # a sale with returns cannot be cancelled
        self.post(f"/sales/{sale['id']}/cancel", {'reason': 'x'})
        self.assertEqual(self.one('SELECT status FROM sales')['status'], 'Completed')
        # a clean sale can be cancelled and stock comes back
        self.sell([(p['id'], 5)], tendered='750')
        s2 = self.one('SELECT id FROM sales ORDER BY id DESC')['id']
        self.assertEqual(self.stock(p['id'])['stock'], 13)
        self.post(f"/sales/{s2}/cancel", {'reason': 'customer changed mind'})
        self.assertEqual(self.stock(p['id'])['stock'], 18)
        self.assertEqual(self.one('SELECT status FROM sales WHERE id=?', s2)['status'], 'Cancelled')

    # ---------------------------------------------------------------- AC-006, AC-007, stocktake
    def test_adjustments_need_reason_and_low_stock_alert(self):
        p = self.stocked_product()
        self.post('/inventory/adjust', {'product_id': p['id'], 'kind': 'damaged', 'qty': '2', 'reason': ''}, once=False)
        self.assertEqual(self.stock(p['id'])['stock'], 20)
        self.post('/inventory/adjust', {'product_id': p['id'], 'kind': 'damaged', 'qty': '2', 'reason': 'broken'},
                  once=False)
        m = self.one("SELECT * FROM stock_moves WHERE move_type='damaged'")
        self.assertEqual((m['qty'], m['reason'], m['user_id']), (-2, 'broken', 1))
        self.assertEqual(self.stock(p['id'])['stock'], 18)
        self.assertNotIn(b'at or below minimum stock', self.c.get('/').data)
        con = self.db()
        con.execute('UPDATE products SET min_stock=18')
        con.close()
        self.assertIn(b'1 product at or below minimum stock', self.c.get('/').data)

    def test_stocktake(self):
        p = self.stocked_product()
        self.post('/inventory/stocktakes', {'notes': 'month end'}, once=False)
        st = self.one('SELECT id FROM stocktakes')['id']
        self.post(f'/inventory/stocktakes/{st}', {f"c_{p['id']}": '17', 'post': '1'}, once=False)
        self.assertEqual(self.stock(p['id'])['stock'], 17)
        self.assertEqual(self.one("SELECT qty FROM stock_moves WHERE move_type='stocktake'")['qty'], -3)

    # ---------------------------------------------------------------- AC-012
    def test_permissions(self):
        self.post('/admin/users/new', {'username': 'sara', 'full_name': 'Sara', 'role': 'Sales Attendant',
                                       'password': 'secret1', 'active': '1'}, once=False)
        p = self.stocked_product()
        self.login('sara', 'secret1')
        self.assertEqual(self.c.get('/reports/').status_code, 403)
        self.assertEqual(self.c.get('/admin/users').status_code, 403)
        self.assertEqual(self.post('/inventory/adjust', {'product_id': p['id'], 'kind': 'lost', 'qty': '1',
                                                         'reason': 'x'}).status_code, 403)
        self.sell([(p['id'], 1)], tendered='200', discount='10')  # no discount permission
        self.assertIsNone(self.one('SELECT * FROM sales'))
        self.sell([(p['id'], 1)], tendered='200')
        self.assertEqual(self.one('SELECT COUNT(*) n FROM sales')['n'], 1)
        self.assertNotIn(b'Gross profit', self.c.get('/').data)
        self.c.get('/logout')
        self.assertEqual(self.c.get('/inventory/').status_code, 302)  # not logged in -> login page

    def test_csrf_required(self):
        r = self.c.post('/expenses/', data={'category_id': 1, 'amount': '5'})
        self.assertEqual(r.status_code, 400)

    # ---------------------------------------------------------------- AC-009, AC-010, AC-013
    def test_reports_and_excel_export(self):
        p = self.stocked_product()
        self.sell([(p['id'], 2)], tendered='300')
        for key in ['stock', 'low', 'moves', 'writeoffs', 'stocktake', 'sales_period', 'sales_product',
                    'sales_category', 'sales_user', 'returns', 'cancelled', 'customer_balances',
                    'purch_history', 'purch_supplier', 'purch_product', 'outstanding', 'profit',
                    'expenses', 'expense_list', 'collections']:
            r = self.c.get(f'/reports/?r={key}&start=2000-01-01&end=2100-01-01')
            self.assertEqual(r.status_code, 200, key)
        r = self.c.get('/reports/?r=sales_product&start=2000-01-01&end=2100-01-01&format=xlsx')
        z = zipfile.ZipFile(io.BytesIO(r.data))
        sheet = z.read('xl/worksheets/sheet1.xml').decode()
        self.assertIn('Item P1', sheet)
        self.assertIn('<v>300.0</v>', sheet)
        for url in ['/', '/sales/', '/purchases/', '/inventory/', '/inventory/movements', '/products/',
                    '/suppliers', '/customers', '/expenses/', '/admin/settings', '/admin/roles', '/admin/audit',
                    '/sales/pos', f"/products/{p['id']}", '/purchases/1', '/sales/1']:
            self.assertEqual(self.c.get(url).status_code, 200, url)

    # ---------------------------------------------------------------- AC-014
    def test_backup_and_restore(self):
        p = self.product(code='KEEP')
        r = self.post('/admin/backup', once=False)
        backup = r.data
        r.close()
        self.assertTrue(backup.startswith(b'SQLite format 3'))
        con = self.db()
        con.execute("UPDATE products SET name='changed'")
        con.close()
        self.post('/admin/restore', {'file': (io.BytesIO(backup), 'b.db')}, once=False,
                  content_type='multipart/form-data')
        self.assertEqual(self.one('SELECT name FROM products WHERE id=?', p['id'])['name'], 'Item KEEP')

    # ---------------------------------------------------------------- BR-007 / BR-008 for expenses
    def test_expense_reversal(self):
        self.post('/expenses/', {'category_id': 1, 'amount': '500', 'date': self._today()})
        e = self.one('SELECT * FROM expenses')
        self.post(f"/expenses/{e['id']}/reverse", {'reason': ''})
        self.assertEqual(self.one('SELECT COUNT(*) n FROM expenses')['n'], 1)  # reason required
        self.post(f"/expenses/{e['id']}/reverse", {'reason': 'typed wrong amount'})
        self.post(f"/expenses/{e['id']}/reverse", {'reason': 'again'})  # only once
        con = self.db()
        rows = con.execute('SELECT amount, reversal_of FROM expenses ORDER BY id').fetchall()
        con.close()
        self.assertEqual([(r['amount'], r['reversal_of']) for r in rows], [(50000, None), (-50000, e['id'])])
        import rules
        con = self.db()
        self.assertEqual(rules.profit(con, '2000-01-01', '2100-01-01')['expenses'], 0)
        con.close()

    def test_dashboard_graphs(self):
        html = self.c.get('/').data
        for title in (b'Net sales per day', b'Where the money went', b'How customers paid',
                      b'Stock value by category', b'Low stock'):
            self.assertIn(title, html)
        self.assertIn(b'per month', self.c.get('/?start=2026-01-01&end=2026-12-31').data)
        self.login('cashier', 'cashier123')  # no report rights: counts only, no money graphs
        html = self.c.get('/').data.split(b'<dialog')[0]  # the page itself, not the Help text
        self.assertIn(b'Number of sales per day', html)
        self.assertNotIn(b'How customers paid', html)

    def test_bulk_products(self):
        self.assertEqual(self.c.get('/products/bulk').status_code, 200)
        form = {'code': ['B1', 'B2', 'B1', ''], 'name': ['Tea', 'Milk', 'Copy', ''],
                'category_id': ['', '', '', ''], 'unit': ['pcs', 'litre', 'pcs', 'pcs'],
                'barcode': ['', '', '', ''], 'price': ['10', '', '5', ''], 'cost': ['6', '', '', ''],
                'opening': ['4', '', '', ''], 'min_stock': ['0', '0', '0', '0']}
        r = self.post('/products/bulk', form)
        # row 2 has no price, row 3 repeats code B1: nothing saved, typed values kept
        self.assertIn(b'Nothing was saved yet: 2 row(s)', r.data)
        self.assertIn(b'also used in row 1', r.data)
        self.assertIn(b'value="Milk"', r.data)
        self.assertIsNone(self.one("SELECT 1 FROM products WHERE code IN ('B1','B2')"))
        form.update(code=['B1', 'B2', 'B3', ''], price=['10', '20', '5', ''])
        r = self.post('/products/bulk', form, follow_redirects=True)
        self.assertIn(b'3 product(s) saved', r.data)
        tea = self.one("SELECT stock, avg_cost, price FROM products WHERE code='B1'")
        self.assertEqual((tea['stock'], tea['avg_cost'], tea['price']), (4, 600, 1000))
        self.assertEqual(self.one("SELECT unit FROM products WHERE code='B2'")['unit'], 'litre')
        # an existing code is refused on the next batch
        r = self.post('/products/bulk', {'code': ['b2'], 'name': ['Again'], 'price': ['1']})
        self.assertIn(b'already belongs to another product', r.data)

    # ---------------------------------------------------------------- security
    def test_default_password_warning(self):
        self.assertIn(b'starter password', self.c.get('/').data)
        self.post('/password', {'current': 'admin123', 'new': 'n3w-pass', 'confirm': 'n3w-pass'}, once=False)
        self.assertNotIn(b'starter password', self.c.get('/').data)
        self.login('admin', 'n3w-pass')

    def test_starter_user_per_role(self):
        con = self.db()
        users = {r['username']: r['role'] for r in con.execute('SELECT username, role FROM users')}
        con.close()
        self.assertEqual(users, {'admin': 'Administrator', 'cashier': 'Sales Attendant',
                                 'storekeeper': 'Inventory Manager', 'cashier2': 'Sales Attendant',
                                 'cashier3': 'Sales Attendant'})
        self.login('cashier', 'cashier123')
        self.assertIn(b'starter password for "cashier"', self.c.get('/').data)
        self.assertEqual(self.c.get('/sales/pos').status_code, 200)
        self.assertEqual(self.c.get('/purchases/').status_code, 403)
        self.login('storekeeper', 'store123')
        self.assertEqual(self.c.get('/purchases/').status_code, 200)
        self.assertEqual(self.c.get('/sales/pos').status_code, 403)
        # seeded only once: a deactivated or renamed starter user is not re-created on restart
        con = self.db()
        con.execute("UPDATE users SET username='kasa' WHERE username='cashier'")
        con.close()
        create_app(self.dbfile, testing=True)
        self.assertIsNone(self.one("SELECT 1 FROM users WHERE username='cashier'"))
        self.login('cashier3', 'sales333')
        self.assertEqual(self.c.get('/sales/pos').status_code, 200)

    def test_new_starter_users_added_to_existing_shop(self):
        """A shop seeded with the first 3 starter users (v1.1) gets the extra cashiers once."""
        con = self.db()
        con.execute("DELETE FROM user_branches WHERE user_id IN (SELECT id FROM users WHERE username IN ('cashier2','cashier3'))")
        con.execute("DELETE FROM users WHERE username IN ('cashier2', 'cashier3')")
        con.execute("UPDATE settings SET value='1' WHERE key='seeded_role_users'")
        con.close()
        create_app(self.dbfile, testing=True)
        self.assertEqual(self.one("SELECT COUNT(*) FROM users WHERE role='Sales Attendant'")[0], 3)
        con = self.db()
        con.execute("DELETE FROM user_branches WHERE user_id IN (SELECT id FROM users WHERE username='cashier3')")
        con.execute("DELETE FROM users WHERE username='cashier3'")
        con.close()
        create_app(self.dbfile, testing=True)
        self.assertIsNone(self.one("SELECT 1 FROM users WHERE username='cashier3'"))

    def test_upgrade_old_database(self):
        """A database from version 1.0 (no expenses.reversal_of) is upgraded on start."""
        con = self.db()
        con.execute('ALTER TABLE expenses DROP COLUMN reversal_of')
        con.close()
        create_app(self.dbfile, testing=True)
        con = self.db()
        cols = [r['name'] for r in con.execute('PRAGMA table_info(expenses)')]
        con.close()
        self.assertIn('reversal_of', cols)

    def test_backup_before_v2_upgrade(self):
        con = self.db()
        con.execute('DROP TABLE user_branches')
        con.execute('DROP TABLE branches')
        con.close()
        backups = os.path.join(self.tmp, 'bk')
        path = core.backup_before_upgrade(self.dbfile, backups)
        self.assertTrue(path and os.path.exists(path))
        self.assertIsNone(core.backup_before_upgrade(self.dbfile.replace('test', 'none'), backups))
        core.init_db(self.dbfile)  # upgrades again: branches are back
        self.assertTrue(self.one('SELECT 1 FROM branches WHERE id=1'))
        self.assertIsNone(core.backup_before_upgrade(self.dbfile, backups))  # already v2: no copy

    # ---------------------------------------------------------------- businesses & branches
    def platform(self):
        return connect(os.path.join(self.tmp, 'platform.db'))

    def test_first_business_and_main_branch(self):
        # the existing shop becomes business 1 with a Main branch; admin is a Super Admin
        p = self.platform()
        biz = p.execute('SELECT * FROM businesses').fetchall()
        self.assertEqual(len(biz), 1)
        self.assertEqual(biz[0]['code'], 'MAIN')
        self.assertTrue(p.execute("SELECT is_super FROM logins WHERE username='admin'").fetchone()['is_super'])
        p.close()
        branch = self.one('SELECT code, name FROM branches')
        self.assertEqual((branch['code'], branch['name']), ('MAIN', 'Main branch'))
        # existing users are placed in the Main branch
        self.assertTrue(self.one("SELECT 1 FROM user_branches ub JOIN users u ON u.id=ub.user_id "
                                 "WHERE u.username='admin' AND ub.branch_id=1"))

    def test_second_business_is_isolated(self):
        with self.app.app_context():
            from flask import g
            g.pdb = connect(os.path.join(self.tmp, 'platform.db'))
            core.current_app.config['PLATFORM_DB'] = os.path.join(self.tmp, 'platform.db')
            bid = core.create_business('Second Shop', 'SEC', 'owner2', 'Owner Two', 'secret2')
            g.pdb.commit()
            g.pdb.close()
        self.assertEqual(bid, 2)
        # owner2 logs into business 2, not the first shop's data
        self.login('owner2', 'secret2')
        self.post('/products/new', {'code': 'SECP', 'name': 'Second-only', 'unit': 'pcs',
                                    'price': '500', 'cost': '0', 'opening': '0', 'min_stock': '0'}, once=False)
        self.assertIn(b'SECP', self.c.get('/products/').data)
        # the first shop never sees it
        self.login('admin', 'admin123')
        self.assertNotIn(b'SECP', self.c.get('/products/').data)
        # usernames are unique across businesses
        r = self.post('/admin/users/new', {'username': 'owner2', 'full_name': 'Clash',
                                            'role': 'Sales Attendant', 'password': 'secret9', 'active': '1'}, once=False)
        self.assertIn(b'already taken', r.data)

    def test_branches_users_and_picker(self):
        r = self.post('/admin/branches/new', {'code': 'bole', 'name': 'Bole shop', 'address': 'Bole Rd',
                                               'active': '1'}, once=False, follow_redirects=True)
        self.assertIn(b'Branch saved', r.data)
        bole = self.one("SELECT * FROM branches WHERE code='BOLE'")
        self.assertEqual(bole['name'], 'Bole shop')
        r = self.post('/admin/branches/new', {'code': 'BOLE', 'name': 'Dup'}, once=False)
        self.assertIn(b'already uses this code', r.data)
        # with two branches a user must be given at least one
        r = self.post('/admin/users/new', {'username': 'abel', 'full_name': 'Abel', 'role': 'Sales Attendant',
                                            'password': 'secret1', 'active': '1'}, once=False)
        self.assertIn(b'Tick at least one branch', r.data)
        self.post('/admin/users/new', {'username': 'abel', 'full_name': 'Abel', 'role': 'Sales Attendant',
                                        'password': 'secret1', 'active': '1', 'branch': [str(bole['id'])]}, once=False)
        # the admin can pick either branch in the top bar; Abel only works in Bole
        html = self.c.get('/').data.decode()
        self.assertIn('name="branch"', html)
        self.post('/branch', {'branch': str(bole['id']), 'next': '/'}, once=False)
        self.assertIn('selected>📍 Bole shop', self.c.get('/').data.decode())
        self.login('abel', 'secret1')
        html = self.c.get('/').data.decode()
        self.assertIn('📍 Bole shop', html)
        self.assertNotIn('Main branch', html[:html.index('</header>')])
        self.post('/branch', {'branch': '1'}, once=False)  # not his branch: ignored
        self.assertNotIn('Main branch', self.c.get('/').data.decode().split('</header>')[0])

    def test_stock_per_branch_and_transfers(self):
        p = self.stocked_product()  # 20 at Main, average cost 110
        self.post('/admin/branches/new', {'code': 'BOLE', 'name': 'Bole shop', 'active': '1'}, once=False)
        bole = self.one("SELECT id FROM branches WHERE code='BOLE'")['id']
        bstock = lambda b: self.one('SELECT stock, avg_cost FROM product_stock WHERE product_id=? AND branch_id=?',
                                    p['id'], b)
        # Bole has none of it yet, so a sale there is refused and Main is untouched
        self.post('/branch', {'branch': str(bole)}, once=False)
        self.sell([(p['id'], 1)], tendered='150')
        self.assertIsNone(self.one('SELECT * FROM sales'))
        # send 5 from Main to Bole: Main 15 at once, Bole only when received
        self.post('/branch', {'branch': '1'}, once=False)
        r = self.post('/inventory/transfers/new', {'to_branch': str(bole), 'pid': [str(p['id'])], 'qty': ['5']},
                      follow_redirects=True)
        self.assertIn(b'In transit', r.data)
        t = self.one('SELECT * FROM transfers')
        self.assertEqual(t['ref_no'], 'MAIN-T-000001')
        self.assertEqual(bstock(1)['stock'], 15)
        self.assertIsNone(bstock(bole))
        item = self.one('SELECT * FROM transfer_items')
        self.assertAlmostEqual(item['unit_cost'], 11000)
        self.post(f"/inventory/transfers/{t['id']}", {'receive': '1', f"r_{item['id']}": '5'}, once=False)
        self.assertEqual(self.one('SELECT status FROM transfers')['status'], 'In transit')  # only Bole can receive
        self.post('/branch', {'branch': str(bole)}, once=False)
        self.post(f"/inventory/transfers/{t['id']}", {'receive': '1', f"r_{item['id']}": '4'}, once=False)
        self.assertEqual(self.one('SELECT status FROM transfers')['status'], 'Received')
        self.assertEqual(bstock(bole)['stock'], 4)  # one lost in transit
        self.assertAlmostEqual(bstock(bole)['avg_cost'], 11000)  # same cost as at Main
        self.assertEqual(self.stock(p['id'])['stock'], 19)  # business total
        # a sale at Bole takes stock from Bole only, numbered with the branch code
        self.sell([(p['id'], 2)], tendered='300')
        s = self.one('SELECT * FROM sales ORDER BY id DESC')
        self.assertEqual((s['branch_id'], s['receipt_no'][:7]), (bole, 'BOLE-S-'))
        self.assertEqual((bstock(bole)['stock'], bstock(1)['stock']), (2, 15))
        self.assertIn(b'BOLE-S-', self.c.get('/sales/').data)
        self.assertNotIn(b'BOLE-S-', self.post('/branch', {'branch': '1', 'next': '/sales/'}, once=False,
                                               follow_redirects=True).data)
        # a transfer that never left can be cancelled by the sender: stock comes back
        self.post('/inventory/transfers/new', {'to_branch': str(bole), 'pid': [str(p['id'])], 'qty': ['3']})
        t2 = self.one('SELECT id FROM transfers ORDER BY id DESC')['id']
        self.assertEqual(bstock(1)['stock'], 12)
        self.post(f'/inventory/transfers/{t2}', {'cancel': '1', 'reason': 'Truck broke down'}, once=False)
        self.assertEqual(bstock(1)['stock'], 15)
        # cannot send more than the branch has
        r = self.post('/inventory/transfers/new', {'to_branch': str(bole), 'pid': [str(p['id'])], 'qty': ['99']})
        self.assertIn(b'Not enough stock', r.data)

    def test_old_stock_moves_to_main_branch(self):
        self.product(code='OLD', cost='10', opening='7')
        con = core.connect(self.app.config['DATABASE'])
        con.execute('DELETE FROM product_stock')  # as before version 2: stock only on products
        con.close()
        core.init_db(self.app.config['DATABASE'])
        row = self.one("SELECT ps.stock FROM product_stock ps JOIN products p ON p.id=ps.product_id WHERE p.code='OLD'")
        self.assertEqual(row['stock'], 7)

    def test_reports_and_dashboard_by_branch(self):
        p = self.stocked_product()  # 20 at Main
        self.post('/admin/branches/new', {'code': 'BOLE', 'name': 'Bole shop', 'active': '1'}, once=False)
        bole = self.one("SELECT id FROM branches WHERE code='BOLE'")['id']
        self.post('/inventory/transfers/new', {'to_branch': str(bole), 'pid': [str(p['id'])], 'qty': ['5']})
        t = self.one('SELECT id FROM transfers')['id']
        item = self.one('SELECT id FROM transfer_items')['id']
        self.post('/branch', {'branch': str(bole)}, once=False)
        self.post(f'/inventory/transfers/{t}', {'receive': '1', f'r_{item}': '5'}, once=False)
        self.sell([(p['id'], 2)], tendered='300')  # Bole: 300.00
        self.post('/branch', {'branch': '1'}, once=False)
        self.sell([(p['id'], 1)], tendered='150')  # Main: 150.00
        html = self.c.get('/').data.decode()  # dashboard follows the branch in the top bar
        self.assertIn('150.00', html)
        self.assertNotIn('450.00', html)
        html = self.c.get('/?branch=all').data.decode()
        self.assertIn('450.00', html)
        self.assertIn('Branches compared', html)
        html = self.c.get(f'/reports/?r=stock&branch={bole}').data.decode()
        self.assertIn('Bole shop', html)
        self.assertIn('<td class="n">3</td>', html)
        rows = self.c.get('/reports/?r=branch_sales').data.decode()
        self.assertIn('Main branch', rows)
        self.assertIn('300.00', rows)
        self.assertIn(b'Stock by branch', self.c.get('/reports/?r=branch_stock').data)
        # a cashier only sees their own branch and cannot ask for all branches
        self.post('/admin/users/new', {'username': 'boleuser', 'full_name': 'B', 'role': 'Sales Attendant',
                                        'password': 'secret1', 'active': '1', 'branch': [str(bole)]}, once=False)
        self.login('boleuser', 'secret1')
        html = self.c.get('/?branch=all').data.decode()
        self.assertNotIn('Branches compared', html)

    def test_super_admin_manages_businesses(self):
        r = self.post('/admin/businesses', {'name': 'Second Shop', 'code': 'sec', 'admin_username': 'owner2',
                                            'admin_name': 'Owner Two', 'admin_password': 'secret2'},
                      once=False, follow_redirects=True)
        self.assertIn(b'Business created', r.data)
        self.assertIn(b'Second Shop', r.data)
        r = self.post('/admin/businesses', {'name': 'Again', 'code': 'SEC', 'admin_username': 'x9',
                                            'admin_name': 'X', 'admin_password': 'secret9'}, once=False)
        self.assertIn(b'already uses this code', r.data)
        self.login('owner2', 'secret2')
        self.assertEqual(self.c.get('/admin/businesses').status_code, 403)  # not a Super Admin
        self.login('admin', 'admin123')
        self.post('/admin/businesses', {'toggle': '2'}, once=False)  # close it: nobody can log in
        r = self.post('/login', {'username': 'owner2', 'password': 'secret2'}, once=False, follow_redirects=True)
        self.assertIn(b'Wrong username or password', r.data)
        r = self.post('/admin/businesses', {'toggle': '1'}, once=False, follow_redirects=True)
        self.assertIn(b'cannot close your own business', r.data)

    def test_unit_choices(self):
        html = self.c.get('/products/new').data.decode()
        for u in ('pcs', 'kg', 'litre', 'carton', 'dozen'):
            self.assertIn(f'<option >{u}</option>', html.replace('selected', ''))
        self.post('/admin/settings', {'shop_name': 'S', 'currency': 'ETB', 'payment_methods': 'Cash',
                                      'units': 'pcs,kg,sack'}, once=False)
        self.assertIn('>sack</option>', self.c.get('/products/bulk').data.decode())
        p = self.product(code='RICE')
        self.post(f"/products/{p['id']}/edit", {'code': 'RICE', 'name': 'Rice', 'unit': 'sack', 'price': '10',
                                                 'min_stock': '0'}, once=False)
        self.assertEqual(self.one("SELECT unit FROM products WHERE code='RICE'")['unit'], 'sack')

    def test_business_profile_and_logo(self):
        png = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00'
               b'\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00'
               b'\x00\x00IEND\xaeB`\x82')
        data = {'shop_name': 'Abebe Traders', 'shop_tin': '0012345678', 'shop_email': 'info@example.com',
                'currency': 'ETB', 'payment_methods': 'Cash', 'receipt_footer': 'Thanks',
                'logo': (io.BytesIO(png), 'logo.png')}
        r = self.post('/admin/settings', data, once=False, content_type='multipart/form-data', follow_redirects=True)
        self.assertIn(b'Business profile saved', r.data)
        self.assertIn(b'class="toplogo"', r.data)
        self.assertEqual(self.c.get('/logo').data, png)
        # wrong file type is refused
        bad = dict(data, logo=(io.BytesIO(b'not an image'), 'logo.gif'))
        r = self.post('/admin/settings', bad, once=False, content_type='multipart/form-data', follow_redirects=True)
        self.assertIn(b'PNG or JPG', r.data)
        # letterhead on receipts shows the logo, TIN and branch
        p = self.product(price='100', cost='50', opening='5')
        self.sell([(p['id'], 1)], tendered='100')
        sid = self.one('SELECT id FROM sales ORDER BY id DESC')['id']
        html = self.c.get(f'/sales/{sid}/receipt').data.decode()
        self.assertIn('Abebe Traders', html)
        self.assertIn('TIN: 0012345678', html)
        self.assertIn('class="headlogo"', html)
        self.assertIn('Branch: Main branch', html)

    # ---------------------------------------------------------------- side menu and help
    def test_side_menu_order_and_help(self):
        html = self.c.get('/').data.decode()
        order = ['Sell (POS)', 'Dashboard', 'Sales history', 'Purchases &amp; receiving', 'Stock levels',
                 'Expenses', 'Reports', 'Users, settings &amp; backup']
        side = html[html.index('<aside'):html.index('</aside>')]
        pos = [side.index(label) for label in order]
        self.assertEqual(pos, sorted(pos))
        self.assertIn('>⚙ Admin</a>', html)  # admin pages always reachable from the top bar
        for key in ('selling', 'buying', 'stock', 'money', 'admin'):  # coloured, foldable groups
            self.assertIn(f'<details class="group g-{key}"', side)
        self.assertIn('toggleMini()', side)  # the whole menu can shrink to icons
        self.assertIn('id="help-dashboard"', html)  # help pop-up with the manual
        self.assertIn("'help-' + (topic || 'dashboard')", html)  # opens at this page's topic
        self.assertIn("|| 'pos')", self.c.get('/sales/pos').data.decode())
        self.assertEqual(self.c.get('/manual').status_code, 200)
        self.post('/admin/users/new', {'username': 'sara', 'full_name': 'Sara', 'role': 'Sales Attendant',
                                       'password': 'secret1', 'active': '1'}, once=False)
        self.login('sara', 'secret1')
        html = self.c.get('/').data.decode()
        side = html[html.index('<aside'):html.index('</aside>')]
        self.assertIn('Sell (POS)', side)
        self.assertNotIn('Users, settings', side)
        self.assertNotIn('>⚙ Admin</a>', html)
        self.assertNotIn('Adjust stock', side)

    @staticmethod
    def _today():
        from datetime import date
        return date.today().isoformat()


if __name__ == '__main__':
    unittest.main()
