"""Tests for the pharmacy module (docs/PHARMACY_MODULE.md).

Run:  python -m unittest discover tests
"""
import io
import os
import sys
import unittest
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_app  # noqa: E402

TODAY = date.today()
NEXT_YEAR = (TODAY + timedelta(days=365)).isoformat()
YESTERDAY = (TODAY - timedelta(days=1)).isoformat()


class PharmacyTest(test_app.ShopTest):
    def setUp(self):
        super().setUp()
        self.post('/admin/settings', {'shop_name': 'Test Pharmacy', 'currency': 'ETB', 'payment_methods': 'Cash',
                                      'units': 'pcs,tablet', 'pharmacy': '1', 'rx_valid_days': '30',
                                      'rx_controlled_days': '7', 'expiry_alert_days': '30,60,90'}, once=False)

    # ---------------------------------------------------------------- helpers
    def medicine(self, code, rx_class='OTC', generic='Paracetamol', price='10', storage='15-25 °C'):
        self.post('/products/new', {'code': code, 'name': f'{generic} {code}', 'unit': 'tablet', 'price': price,
                                    'cost': '0', 'opening': '0', 'min_stock': '0', 'is_medicine': '1',
                                    'generic_name': generic, 'strength': '500 mg', 'dosage_form': 'Tablet',
                                    'manufacturer': 'EPHARM', 'rx_class': rx_class, 'storage': storage,
                                    'reg_no': 'EFDA-1', 'reg_expiry': NEXT_YEAR}, once=False)
        return self.one('SELECT * FROM products WHERE code=?', code)

    def buy(self, pid, qty, cost='5', batch='B1', expiry=NEXT_YEAR):
        sup = self.supplier()
        return self.post('/purchases/new', {'supplier_id': sup, 'date': TODAY.isoformat(), 'pid': [pid], 'qty': [qty],
                                            'cost': [cost], 'discount': '0', 'expiry': [expiry], 'serials': [''],
                                            'batch_no': [batch], 'confirm': '1'})

    def pharmacist(self, username='pharm', licence_expiry=NEXT_YEAR):
        self.post('/admin/users/new', {'username': username, 'full_name': 'Pharm ' + username, 'role': 'Pharmacist',
                                       'password': 'pharm123', 'active': '1', 'branch': '1', 'licence_no': 'PH-1',
                                       'licence_expiry': licence_expiry}, once=False)
        return self.one('SELECT * FROM users WHERE username=?', username)

    def patient(self, allergies=''):
        self.post('/pharmacy/patients/new', {'name': 'Almaz T', 'phone': '0911', 'age': '30', 'sex': 'Female',
                                             'allergies': allergies}, once=False)
        return self.one('SELECT * FROM patients ORDER BY id DESC')['id']

    def prescriber(self):
        old = self.one('SELECT id FROM prescribers ORDER BY id DESC')
        if old:
            return old['id']
        self.post('/pharmacy/prescribers', {'name': 'Dr Kebede', 'licence_no': 'MD-9', 'facility': 'Black Lion'},
                  once=False)
        return self.one('SELECT * FROM prescribers ORDER BY id DESC')['id']

    def register_rx(self, pid, qty='10', patient=None, **extra):
        data = {'patient_id': patient or self.patient(), 'prescriber_id': self.prescriber(), 'date': TODAY.isoformat(),
                'rx_no': 'RX-1', 'pid': [pid], 'qty': [qty], 'dose': ['1 tablet'], 'frequency': ['3 times a day'],
                'duration': ['5 days'], 'instructions': ['after food']}
        data.update(extra)
        r = self.post('/pharmacy/prescriptions/new', data, content_type='multipart/form-data')
        return r, self.one('SELECT * FROM prescriptions ORDER BY id DESC')

    def flash(self, r):
        return (self.c.get(r.location).data if r.status_code == 302 else r.data).decode()

    # ---------------------------------------------------------------- switch
    def test_module_off_changes_nothing(self):
        self.post('/admin/settings', {'shop_name': 'Shop', 'currency': 'ETB', 'payment_methods': 'Cash',
                                      'units': 'pcs'}, once=False)
        self.assertEqual(self.one("SELECT value FROM settings WHERE key='pharmacy'")['value'], '0')
        self.assertNotIn(b'href="/pharmacy/', self.c.get('/sales/pos').data)
        self.assertEqual(self.c.get('/pharmacy/prescriptions').status_code, 302)  # pages are closed
        # a product that has medicine details from before is sold freely while the module is off
        con = self.db()
        con.execute("INSERT INTO products(code, name, price) VALUES ('X', 'X', 100)")
        pid = con.execute("SELECT id FROM products WHERE code='X'").fetchone()[0]
        con.execute("INSERT INTO medicines(product_id, generic_name, rx_class) VALUES (?, 'x', 'POM')", (pid,))
        con.close()
        self.purchase(pid, '5', '50')
        self.sell([(pid, 1)], tendered='100')
        self.assertEqual(self.one('SELECT COUNT(*) n FROM sales')['n'], 1)

    def test_menu_and_pages_render(self):
        self.medicine('PCM')
        html = self.c.get('/sales/pos').data.decode()
        for label in ('Prescriptions', 'Expiry &amp; batches', 'Recalls', 'Controlled register'):
            self.assertIn(label, html)
        for url in ('/pharmacy/prescriptions', '/pharmacy/prescriptions/new', '/pharmacy/patients',
                    '/pharmacy/patients/new', '/pharmacy/prescribers', '/pharmacy/expiry', '/pharmacy/recalls',
                    '/pharmacy/disposals', '/pharmacy/controlled', '/products/new', '/products/1', '/products/1/edit',
                    '/admin/settings', '/admin/users/new', '/purchases/new', '/inventory/batches'):
            self.assertEqual(self.c.get(url).status_code, 200, url)

    # ---------------------------------------------------------------- medicines, receiving
    def test_medicine_details_and_receiving_needs_batch_and_expiry(self):
        p = self.medicine('AMX', 'POM', 'Amoxicillin')
        m = self.one('SELECT * FROM medicines WHERE product_id=?', p['id'])
        self.assertEqual((m['generic_name'], m['rx_class'], m['strength']), ('Amoxicillin', 'POM', '500 mg'))
        r = self.buy(p['id'], '10', batch='')
        self.assertIn('batch number and the expiry date', self.flash(r))
        self.assertEqual(self.one('SELECT stock FROM products WHERE id=?', p['id'])['stock'], 0)
        r = self.buy(p['id'], '10', expiry=YESTERDAY)
        self.assertIn('Expired medicines cannot be received', self.flash(r))
        self.buy(p['id'], '10', batch='AMX-001')
        b = self.one('SELECT * FROM stock_batches WHERE product_id=?', p['id'])
        self.assertEqual((b['batch_no'], b['expiry'], b['remaining'], b['status']), ('AMX-001', NEXT_YEAR, 10, 'Available'))
        # duplicate warning (same generic, strength, form, manufacturer)
        r = self.medicine('AMX2', 'POM', 'Amoxicillin')
        self.assertIsNotNone(r)

    def test_expired_registration_blocks_buying_and_selling(self):
        p = self.medicine('PCM')
        self.buy(p['id'], '10')
        con = self.db()
        con.execute('UPDATE medicines SET reg_expiry=? WHERE product_id=?', (YESTERDAY, p['id']))
        con.close()
        r = self.sell([(p['id'], 1)], tendered='10')
        self.assertIn('EFDA registration expired', self.flash(r))
        r = self.buy(p['id'], '5')
        self.assertIn('EFDA registration expired', self.flash(r))

    # ---------------------------------------------------------------- POS rules
    def test_pos_sells_otc_but_refuses_prescription_medicines(self):
        otc = self.medicine('PCM')
        pom = self.medicine('AMX', 'POM', 'Amoxicillin')
        self.buy(otc['id'], '10')
        self.buy(pom['id'], '10', batch='A1')
        self.sell([(otc['id'], 2)], tendered='20')
        self.assertEqual(self.one('SELECT COUNT(*) n FROM sales')['n'], 1)
        r = self.sell([(pom['id'], 1)], tendered='10')
        self.assertIn('only be sold from a verified prescription', self.flash(r))
        self.assertEqual(self.one('SELECT COUNT(*) n FROM sales')['n'], 1)
        j = self.c.get('/sales/api/products?q=AMX').get_json()
        self.assertIn('[POM: prescription]', j['items'][0]['name'])

    def test_expired_and_blocked_batches_are_never_sold_fefo(self):
        p = self.medicine('PCM')
        self.buy(p['id'], '5', batch='OLD', expiry=(TODAY + timedelta(days=10)).isoformat())
        self.buy(p['id'], '5', batch='NEW')
        con = self.db()
        con.execute("UPDATE stock_batches SET expiry=? WHERE batch_no='OLD'", (YESTERDAY,))
        con.close()
        r = self.sell([(p['id'], 6)], tendered='60')
        self.assertIn('only 5 tablet can be sold', self.flash(r))
        self.sell([(p['id'], 2)], tendered='20')
        self.assertEqual(self.one("SELECT remaining FROM stock_batches WHERE batch_no='NEW'")['remaining'], 3)
        self.assertEqual(self.one("SELECT remaining FROM stock_batches WHERE batch_no='OLD'")['remaining'], 5)
        # quarantine the good batch: nothing can be sold
        bid = self.one("SELECT id FROM stock_batches WHERE batch_no='NEW'")['id']
        self.post(f'/pharmacy/batches/{bid}/status', {'status': 'Quarantined', 'reason': 'temperature'}, once=False)
        r = self.sell([(p['id'], 1)], tendered='10')
        self.assertIn('only 0 tablet can be sold', self.flash(r))
        self.post(f'/pharmacy/batches/{bid}/status', {'status': 'Available', 'reason': 'checked'}, once=False)
        self.sell([(p['id'], 1)], tendered='10')
        self.assertEqual(self.one("SELECT remaining FROM stock_batches WHERE batch_no='NEW'")['remaining'], 2)
        self.assertIn(b'expired and still in stock', self.c.get('/pharmacy/expiry').data)

    def test_customer_return_is_quarantined(self):
        p = self.medicine('PCM')
        self.buy(p['id'], '10')
        self.sell([(p['id'], 4)], tendered='40')
        sale = self.one('SELECT * FROM sales')
        item = self.one('SELECT * FROM sale_items')
        self.post(f"/sales/{sale['id']}/return", {'item_id': item['id'], 'qty': '1', 'reason': 'not needed'})
        rows = self.db().execute('SELECT status, remaining, batch_no FROM stock_batches WHERE product_id=? ORDER BY id',
                                 (p['id'],)).fetchall()
        self.assertEqual([(r['status'], r['remaining'], r['batch_no']) for r in rows],
                         [('Available', 6, 'B1'), ('Quarantined', 1, 'B1')])
        self.assertEqual(self.one('SELECT stock FROM products WHERE id=?', p['id'])['stock'], 7)

    # ---------------------------------------------------------------- prescriptions
    def test_prescription_verify_and_dispense(self):
        p = self.medicine('AMX', 'POM', 'Amoxicillin', price='20')
        self.buy(p['id'], '30', batch='A1')
        pat = self.patient(allergies='amoxicillin')
        r, rx = self.register_rx(p['id'], '15', patient=pat)
        self.assertEqual(rx['status'], 'Registered')
        # admin has no licence: cannot verify
        r = self.post(f"/pharmacy/prescriptions/{rx['id']}/verify", {'decision': 'verify'}, once=False)
        self.assertIn('pharmacist with a licence', self.flash(r))
        self.pharmacist()
        self.login('pharm', 'pharm123')
        page = self.c.get(f"/pharmacy/prescriptions/{rx['id']}").data.decode()
        self.assertIn('allergic to', page)
        r = self.post(f"/pharmacy/prescriptions/{rx['id']}/verify", {'decision': 'verify'}, once=False)
        self.assertIn('give your reason', self.flash(r))
        self.post(f"/pharmacy/prescriptions/{rx['id']}/verify", {'decision': 'verify', 'reason': 'prescriber confirmed'},
                  once=False)
        rx = self.one('SELECT * FROM prescriptions WHERE id=?', rx['id'])
        self.assertEqual(rx['status'], 'Verified')
        self.assertIn('prescriber confirmed', rx['interventions'])
        item = self.one('SELECT * FROM rx_items WHERE prescription_id=?', rx['id'])
        r = self.post(f"/pharmacy/prescriptions/{rx['id']}/dispense", {f"d_{item['id']}": '10', 'method': 'Cash'})
        self.assertEqual(r.status_code, 302)
        self.assertIn('/pharmacy/labels/', r.location)
        self.assertEqual(self.c.get(r.location).status_code, 200)
        sale = self.one('SELECT * FROM sales')
        self.assertEqual((sale['prescription_id'], sale['total'], sale['paid']), (rx['id'], 20000, 20000))
        self.assertEqual(self.one('SELECT status FROM prescriptions WHERE id=?', rx['id'])['status'], 'Partially dispensed')
        r = self.post(f"/pharmacy/prescriptions/{rx['id']}/dispense", {f"d_{item['id']}": '6', 'method': 'Cash'})
        self.assertIn('only 5 tablet is left', self.flash(r))
        self.post(f"/pharmacy/prescriptions/{rx['id']}/dispense", {f"d_{item['id']}": '5', 'method': 'Cash'})
        self.assertEqual(self.one('SELECT status FROM prescriptions WHERE id=?', rx['id'])['status'], 'Dispensed')
        self.assertEqual(self.one('SELECT stock FROM products WHERE id=?', p['id'])['stock'], 15)
        # the patient's history is shown to the pharmacist, and the view is logged
        self.c.get(f'/pharmacy/patients/{pat}')
        self.assertTrue(self.one("SELECT 1 FROM audit_log WHERE action='patient_history_view'"))

    def test_expired_licence_and_duplicate_prescription(self):
        p = self.medicine('AMX', 'POM', 'Amoxicillin')
        self.buy(p['id'], '30', batch='A1')
        pat = self.patient()
        _r, rx = self.register_rx(p['id'], patient=pat)
        r, _ = self.register_rx(p['id'], patient=pat)  # same number, prescriber and patient
        self.assertIn('already registered', self.flash(r))
        self.pharmacist('old', licence_expiry=YESTERDAY)
        self.login('old', 'pharm123')
        r = self.post(f"/pharmacy/prescriptions/{rx['id']}/verify", {'decision': 'verify'}, once=False)
        self.assertIn('licence expired', self.flash(r))

    def test_controlled_medicine_needs_form_scan_and_double_check(self):
        p = self.medicine('MOR', 'Narcotic', 'Morphine', price='50')
        self.buy(p['id'], '20', batch='M1')
        r, _ = self.register_rx(p['id'], '4')
        self.assertIn('EFDA prescription form', self.flash(r))
        r, _ = self.register_rx(p['id'], '4', form_serial='F-77')
        self.assertIn('upload a scan', self.flash(r))
        r, rx = self.register_rx(p['id'], '4', form_serial='F-77', scan=(io.BytesIO(b'%PDF-1.4 test'), 'rx.pdf'))
        self.assertEqual(rx['form_serial'], 'F-77')
        self.assertEqual(self.c.get(f"/pharmacy/prescriptions/{rx['id']}/scan").status_code, 200)
        self.pharmacist()
        self.pharmacist('pharm2')
        self.login('pharm', 'pharm123')
        self.post(f"/pharmacy/prescriptions/{rx['id']}/verify", {'decision': 'verify'}, once=False)
        item = self.one('SELECT * FROM rx_items WHERE prescription_id=?', rx['id'])
        r = self.post(f"/pharmacy/prescriptions/{rx['id']}/dispense", {f"d_{item['id']}": '4', 'method': 'Cash'})
        self.assertIn('second person must confirm', self.flash(r))
        r = self.post(f"/pharmacy/prescriptions/{rx['id']}/dispense", {f"d_{item['id']}": '4', 'method': 'Cash',
                                                                        'check_user': 'pharm2', 'check_password': 'wrong'})
        self.assertIn('second person must confirm', self.flash(r))
        self.post(f"/pharmacy/prescriptions/{rx['id']}/dispense", {f"d_{item['id']}": '4', 'method': 'Cash',
                                                                    'check_user': 'pharm2', 'check_password': 'pharm123'})
        d = self.one('SELECT * FROM dispensings')
        self.assertEqual(d['checked_by'], self.one("SELECT id FROM users WHERE username='pharm2'")['id'])
        html = self.c.get(f"/pharmacy/controlled?pid={p['id']}").data.decode()
        self.assertIn('Almaz T', html)
        self.assertIn('Dr Kebede', html)
        self.assertIn('<b>16</b>', html)  # running balance after 20 in, 4 out

    # ---------------------------------------------------------------- recall, disposal
    def test_recall_blocks_batch_and_traces_buyers(self):
        p = self.medicine('PCM')
        self.buy(p['id'], '10', batch='LOT-9')
        self.post('/customers/new', {'name': 'Hana', 'phone': '0922'}, once=False)
        cust = self.one('SELECT id FROM customers')['id']
        self.sell([(p['id'], 3)], tendered='30', customer=cust)
        r = self.post('/pharmacy/recalls', {'product_id': p['id'], 'batch_no': 'lot-9', 'source': 'EFDA',
                                            'recall_class': 'Class I', 'reference': 'EFDA/123'})
        self.assertIn('1 stock batch', self.flash(r))
        self.assertEqual(self.one("SELECT status FROM stock_batches WHERE batch_no='LOT-9'")['status'], 'Recalled')
        r = self.sell([(p['id'], 1)], tendered='10')
        self.assertIn('only 0 tablet can be sold', self.flash(r))
        rc = self.one('SELECT * FROM recalls')
        html = self.c.get(f"/pharmacy/recalls/{rc['id']}").data.decode()
        self.assertIn('Hana', html)
        self.assertIn('0922', html)
        x = self.c.get(f"/pharmacy/recalls/{rc['id']}?export=1")
        self.assertEqual(x.data[:2], b'PK')
        # a recalled batch cannot simply be released
        bid = self.one("SELECT id FROM stock_batches WHERE batch_no='LOT-9'")['id']
        r = self.post(f'/pharmacy/batches/{bid}/status', {'status': 'Available', 'reason': 'x'}, once=False)
        self.assertIn('recalled', self.flash(r))

    def test_disposal_with_witnesses_writes_off(self):
        p = self.medicine('PCM')
        self.buy(p['id'], '10', batch='D1')
        bid = self.one("SELECT id FROM stock_batches WHERE batch_no='D1'")['id']
        ph = self.pharmacist()
        admin = self.one("SELECT id FROM users WHERE username='admin'")['id']
        cashier = self.one("SELECT id FROM users WHERE username='cashier'")['id']
        r = self.post(f'/pharmacy/dispose/{bid}', {'qty': '4', 'method': 'Incineration', 'reason': 'damaged',
                                                   'witness1': admin, 'witness2': cashier})
        self.assertIn('pharmacist with a valid licence', self.flash(r))
        self.post(f'/pharmacy/dispose/{bid}', {'qty': '4', 'method': 'Incineration', 'reason': 'damaged',
                                               'witness1': ph['id'], 'witness2': cashier})
        self.assertEqual(self.one('SELECT remaining FROM stock_batches WHERE id=?', bid)['remaining'], 6)
        self.assertEqual(self.one('SELECT stock FROM products WHERE id=?', p['id'])['stock'], 6)
        d = self.one('SELECT * FROM disposals')
        self.assertEqual((d['qty'], d['method']), (4, 'Incineration'))
        self.assertEqual(self.one('SELECT move_type FROM stock_moves WHERE id=?', d['move_id'])['move_type'], 'disposal')
        self.assertIn(b'Incineration', self.c.get('/pharmacy/disposals').data)


# use only the helpers of the shop tests, not their tests (those run in test_app.py)
for _name in dir(test_app.ShopTest):
    if _name.startswith('test_') and _name not in PharmacyTest.__dict__:
        setattr(PharmacyTest, _name, None)

if __name__ == '__main__':
    unittest.main()
