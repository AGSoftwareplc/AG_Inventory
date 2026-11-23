# Acceptance checklist (SRS v1.1)

This table shows how each acceptance criterion is met and how it is checked. The automated tests are in `tests/test_app.py`. Run them with `python -m unittest discover tests`. GitHub runs them on every change, on Windows and Linux, with Python 3.10 and 3.13.

| ID | Criterion | How it is checked |
|---|---|---|
| AC-001 | Admin creates a product and assigns its category | `test_create_product_with_category` |
| AC-002 | Confirmed purchase receiving increases stock | `test_purchase_receiving_and_weighted_average`, `test_cancelled_purchase_cannot_receive` |
| AC-003 | Confirmed sale decreases stock | `test_sale_totals_discount_change` |
| AC-004 | Cannot sell more than available stock | `test_cannot_oversell` |
| AC-005 | Totals, discounts and change are correct | `test_sale_totals_discount_change`, `test_percent_discount` |
| AC-006 | Adjustments record a reason and the user | `test_adjustments_need_reason_and_low_stock_alert` |
| AC-007 | Low-stock notification at the threshold | `test_adjustments_need_reason_and_low_stock_alert` |
| AC-008 | Weighted average cost is correct | `test_purchase_receiving_and_weighted_average`, `test_purchase_return_and_payment` |
| AC-009 | Sales and purchase histories can be retrieved | `test_reports_and_excel_export` (history pages and reports) |
| AC-010 | Dashboard shows correct indicators | `test_return_cancel_and_profit` (same profit function as the dashboard), `test_permissions` |
| AC-011 | Gross and net operating profit are accurate | `test_return_cancel_and_profit`, `test_expense_reversal` |
| AC-012 | Unauthorized users cannot do restricted operations | `test_permissions`, `test_csrf_required` |
| AC-013 | Reports export to Excel and save as PDF | `test_reports_and_excel_export` (.xlsx). For PDF, use **Print / PDF** in the browser, then *Save as PDF* |
| AC-014 | Backup and restore work | `test_backup_and_restore` |
| AC-015 | Works on desktop and mobile | Checked by hand in a browser at 1280 px and 390 px wide (POS, menu, receipt, dashboard) |
| AC-016 | Installs on Windows with only Python, no Docker, no database server | `install.bat` / `start.bat`. The GitHub test run installs and tests on Windows |
| AC-017 | A double click or page refresh does not record a sale twice | `test_double_submit_records_one_sale` |

## Requirements coverage

| Area | Requirements | Where |
|---|---|---|
| Login, users, roles, permissions | FR-001 to FR-007 | `app.py` (login, password), `admin.py` (users, role permissions), `core.py` (permission checks) |
| Products, categories | FR-008 to FR-014a | `products.py` |
| Inventory, adjustments, movements, stocktake | FR-015 to FR-025 | `inventory.py`, `rules.py` (`move_stock`, `adjust_stock`, stocktake) |
| Suppliers | FR-026 to FR-029 | `contacts.py` |
| Purchases: order, receive, pay, return | FR-030 to FR-039a | `purchases.py`, `rules.py` |
| POS, sales, receipts, returns, cancellation | FR-040 to FR-053 | `sales.py`, `static/pos.js`, `rules.py` |
| Customers | FR-054 to FR-057 | `contacts.py` |
| Expenses (with reversal for corrections) | FR-058 to FR-062, BR-007, BR-008 | `expenses.py` |
| Dashboard | FR-063 to FR-066 | `reports.py` (`dash`) |
| Reports, Excel, print | FR-067 to FR-070 | `reports.py`, `templates/reports.html` |
| Settings, backup, audit log | FR-071 to FR-073, NFR-013 to NFR-015 | `admin.py`, `core.py` (`daily_backup`, `restore_db`) |
| Business rules | BR-001 to BR-017 | `rules.py`, `schema.sql` (unique codes, money in cents) |
| Security | NFR-008 to NFR-012 | Login on every page, hashed passwords, server-side permissions, parameterized SQL, escaped HTML, CSRF tokens |

## Upgrading an existing installation

1. Stop the app.
2. Replace the program files with the new version, but keep the `data/` and `backups/` folders.
3. Run `install.bat` again, then `start.bat`.

The app updates the database on start (`core.MIGRATIONS`). Your data is kept.
