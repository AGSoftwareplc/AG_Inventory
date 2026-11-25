# Pharmacy module: SRS addendum

Source: *Pharmacy Management System SRS v1.1* (owner: Abebe Getnet, 02 November 2026).
This addendum says how that SRS is delivered as an **optional module of AG Inventory**, what is built in
this first release, and what is left for later phases.

## 1. Decision

The pharmacy is **not a separate system**. It is a switch per business:
**Admin → Business profile & settings → Pharmacy module**. One codebase, one SQLite file per business,
no Docker, same install. With the switch off, nothing changes for a normal shop.

The SRS was written for ASP.NET Core and PostgreSQL (SRS 14.1). AG Inventory keeps its own stack
(Python, Flask, SQLite, Waitress), as approved for the shop SRS. All the rules below are enforced in
`rules.py` and `pharmacy.py`, not in the browser.

## 2. Reused from AG Inventory as it is

| SRS area | Already in AG Inventory |
|---|---|
| Products, categories, barcodes, units, price history (FR-001-01, 04, 07, 10, 11) | Products |
| Suppliers, purchases, receiving, supplier returns (3.2, 3.3, 3.18) | Purchases & receiving, Return to supplier |
| Batch-level stock, FIFO/FEFO, transfers, stocktakes, adjustments (3.4, 6.3, BR-002, BR-005 to 008) | Stock batches (FIFO, expiry first), Transfers, Stocktake, Adjust stock |
| POS, receipts, payments, credit, TT numbers, returns, cancellations (3.5, 3.8, 3.9, 3.11 partly) | Sell (POS), Sales history, Customers |
| Users, roles, permissions, audit log (3.14, 3.25) | Admin → Users, Role permissions, Audit log |
| Multi-branch (3.15) | Branches, branch picker |
| Reports and dashboard (3.13 partly) | Reports, Daily report, Dashboard |
| Backup (NFR) | Daily automatic backup, Backup / Restore |

## 3. New in this release (pharmacy switched on)

| SRS reference | What is built |
|---|---|
| FR-001-02, 03, 05 | Medicine details on the product: generic name, brand, manufacturer, dosage form, strength. A product without them is general goods. |
| FR-001-08 | Regulatory class: OTC, POM, Narcotic, Psychotropic, Precursor. Anything but OTC is refused at the POS and sold only from a verified prescription. |
| FR-001-13, BR-026 | EFDA registration number and expiry. Expired registration blocks buying and selling. Expiring within 60 days is listed on Expiry & batches. |
| FR-001-14 | Storage condition with a cold-chain ❄ mark on purchase lines, POS search, prescription and labels. |
| FR-001-18 | Possible duplicate warning (same generic, strength, form, manufacturer). |
| BR-003, FR-004 | Batch number and expiry required when a medicine is received. Expired stock cannot be received. Transfers keep the batch number. |
| BR-004, BR-005, BR-009, FR-010-01 to 03 | Sales use FEFO and only *Available* batches that are not past expiry (expired from the day after the expiry date). |
| 6.3 stock status | Batch status: Available, Quarantined, Damaged, Recalled; Expired is shown automatically. |
| BR-013 | Customer returns of medicines go to a Quarantined batch; only a pharmacist releases them. |
| FR-010-02 | Expiry alerts at 30, 60 and 90 days (configurable), and a warning bar at the top. |
| FR-010-05, 06, 09 | Recalls by product and batch number, blocked in every branch; list of every sale, customer and patient (with phone) who received it; Excel export; each notification attempt recorded. |
| FR-010-04, 07, 10, FR-020-08 | Disposal of a batch with method, reason, two witnesses (one a licensed pharmacist), certificate upload; posts the write-off. Disposals of controlled medicines form the destruction register. |
| FR-006-01 to 04, 06, 09, 11, 14 | Prescriptions: number, date, patient, prescriber, items with dose, frequency, duration, instructions; scan upload (PDF/JPG/PNG, 10 MB); statuses Registered, Verified, Rejected, Partially dispensed, Dispensed, Cancelled (Expired shown automatically); duplicate number warning. |
| FR-006-05, 08, 10, 15, BR-010 | Pharmacist verification required; the verifying and dispensing pharmacist is recorded. |
| FR-006-07 | Partial dispensing. |
| FR-006-12 | Patients separate from customers: date of birth or age, sex, weight (required under 12), allergies, pregnancy / breastfeeding, conditions. |
| FR-006-13 | Prescriber registry (name, profession, licence, facility, phone); can be added while registering. |
| FR-006-16 | Validity 30 days, 7 for controlled medicines (configurable). |
| FR-007-01 to 06, 09, 11 | Verification shows strength, dose and instructions; allergy and 30-day duplicate-therapy checks; going on needs a reason, kept as an intervention; counselling notes. |
| FR-007-03 | Dispensing makes a normal sale: batch chosen by FEFO, quantity and batch recorded. |
| FR-007-07 | Patient history only for pharmacists and administrators; every view logged. |
| FR-007-08 | Dispensing labels (patient, medicine, strength, dose, frequency, duration, date, pharmacy, pharmacist). |
| FR-007-10 | Pharmacist licence number and expiry on the user; warning 30 days before; expired blocks verifying and dispensing. |
| FR-020-01, 07, 09 | Controlled-medicine register per medicine: date, document, in, out, running balance, patient, prescriber, pharmacist; opening and closing balance for a period; printable. Built from stock movements, which cannot be edited. |
| FR-020-02 | Controlled medicines: second person confirms each issue with their own username and password. |
| FR-020-03, 04 | EFDA form serial and scan required; no refills; shorter validity. |
| 8.1 roles | New role **Pharmacist**; new permissions *Pharmacy* (register) and *Dispense* (verify and dispense). |

## 4. Not in this release (later phases, or need the owner's decision)

| SRS reference | Why not now |
|---|---|
| FR-001-15, 3.21 (VAT, TOT, withholding, fiscal device / e-invoicing) | The shop SRS decided *no VAT*. Needs a tax adviser and a fiscal device vendor first (SRS Appendix B). |
| 3.11 integrated accounting, journals, period close (BR-018, BR-025) | Large; AG Inventory keeps profit reports and expenses. |
| 3.22 Amharic UI, Ethiopian calendar, FR-001-20, FR-007-12 | The shop SRS decided *English only*. |
| FR-005 payment APIs (telebirr, CBE Birr, card terminal), 3.24 SMS / email | Need provider accounts and internet; payments are recorded by method and TT number today. |
| 3.19 cashier shifts and cash drawer | Not requested for the shop; can be added as its own step. |
| FR-003 purchase requests, approval matrix, three-way matching, BR-019 segregation of duties | Purchases stay one-step (confirm = in stock), as the owner chose in requirements update v1.1. |
| FR-001-06, 3.17 packaging conversions (box, strip, tablet) | Products have one unit; sell by the smallest unit. A conversion table is a separate change. |
| FR-001-16, 17 markup pricing, maximum retail price, margin review | Prices are set per product today. |
| FR-001-18 Manager approval of new products | Only the duplicate warning is built. |
| FR-001-12 generic substitution record | Not built; the pharmacist can note it in the intervention reason. |
| FR-006-16 refills | Not built (no refills). |
| FR-020-05 locked location, FR-020-06 weekly balance check that blocks issue | Use a stocktake weekly; automatic blocking not built. |
| FR-008 loyalty, insurers and third-party payers (FR-005-21) | Out of scope / Should. |
| FR-001-19 VEN / ABC classification, FR-012 reorder planning beyond minimum stock | Later. |
| BR-022 minimum remaining shelf life on receiving | Expired stock is refused; a minimum shelf life rule is not built. |

## 5. Acceptance criteria covered

AC-001, AC-002, AC-004, AC-005, AC-009, AC-018 (register balance), AC-019 (recall trace).
Tests: `tests/test_pharmacy.py` (12 tests). The existing 55 tests still pass.

## 6. Data

New tables: `medicines`, `patients`, `prescribers`, `prescriptions`, `rx_items`, `dispensings`, `recalls`,
`recall_contacts`, `disposals`. New columns: `stock_batches.batch_no`, `stock_batches.status`,
`purchase_items.batch_no`, `users.licence_no`, `users.licence_expiry`, `sales.prescription_id`.
Existing databases get them automatically at the next start; existing data is not changed.
Uploaded scans and certificates are kept in `data/pharmacy/business-<id>/`.
