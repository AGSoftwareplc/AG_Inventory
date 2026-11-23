# AG Inventory: tech stack

AG Inventory is a **web app**. One PC in the shop runs it. Staff use it in a browser on that PC, or on phones, tablets and other PCs on the shop Wi-Fi. No internet, Docker or database server is needed.

## At a glance

| Part | What we use | Why |
|---|---|---|
| Language | **Python 3.10 or newer** | One free install on Windows or Linux. Easy to read and change. |
| Web framework | **Flask 3** | Small and well known, with no heavy architecture. |
| Web server | **Waitress 3** (`serve.py`, 8 threads) | Pure Python, works on Windows, and serves several devices at once. |
| Database | **SQLite** (built into Python), one file: `data/shop.db` | No database server. WAL mode lets several users work at once, and `BEGIN IMMEDIATE` transactions keep stock and money consistent. |
| Pages | Server-rendered **Jinja2** HTML templates (`templates/`) | Works in any browser, with no build step. |
| Look and feel | One plain CSS file (`static/style.css`) | Responsive layout for PC and phone, plus print styles for A4. |
| Scripts in the browser | Plain JavaScript (`static/pos.js` and small inline scripts) | No framework and no CDN, so it works offline. |
| Dashboard graphs | Inline **SVG/HTML** drawn by the server | No chart library to download, so it works offline. |
| Excel export | A small built-in `.xlsx` writer (`reports.py`) | No extra package to install. |
| PDF | The browser's **Print → Save as PDF** with A4 print styles | Nothing to install. |
| Passwords and security | Werkzeug password hashing (it comes with Flask), CSRF tokens on every form, one-time tokens against double submits, permissions per role, and an audit log | |
| Tests | Python `unittest` (`tests/`), run on GitHub Actions on Windows and Ubuntu with Python 3.10 and 3.13 | |

There are only **two packages to install** (`requirements.txt`): `Flask` and `waitress`. Everything else ships with Python.

## How the code is organised

```text
app.py         start-up, login, page setup           serve.py      start for the shop (Waitress)
core.py        database, money parsing, permissions   rules.py      all stock / cost / money rules
products.py    products, categories, bulk entry       inventory.py  stock levels, adjustments, stocktake
purchases.py   purchase -> receive -> pay -> return   sales.py      POS, receipts, returns, cancel
contacts.py    suppliers and customers                expenses.py   expenses
reports.py     dashboard, graphs, reports, Excel      admin.py      users, roles, settings, backup
schema.sql     database tables                        templates/    one HTML file per screen
static/        style.css, pos.js                      tests/        automated tests
docs/          ACCEPTANCE.md, TECH_STACK.md
```

Each screen area is its own Flask *blueprint* (one `.py` file), so a change touches only the files of that area. All rules for stock, cost and money live in `rules.py`.

## Data rules

- Money is stored as **whole cents** (integers), so there are no rounding errors. Quantities can have up to 3 decimals, for example 2.5 kg.
- Product cost is the **weighted average cost**.
- Every stock change is written to `stock_moves` with the user and a reference.
- Nothing that affects money or stock is deleted. Mistakes are corrected with returns, cancellations, reversals and adjustments.

## Businesses and branches

- **Each business has its own SQLite file**, so one business can never see another's data. Business 1 is `data/shop.db`; new ones are `data/biz-<id>.db`. A small `data/platform.db` lists the businesses and which business each username belongs to. The Super Admin adds or closes businesses in **⚙ Admin → Businesses**.
- **Branches** live inside a business. Products, prices, customers, suppliers and users are shared by the business. Stock and average cost are kept per branch (`product_stock`), and sales, purchases, expenses, returns, stocktakes and stock moves record their branch. Document numbers start with the branch code, for example `MAIN-S-000042`.
- **Transfers** move stock between branches at the sending branch's average cost: sent, then received (or cancelled).
- The dashboard and reports show one branch, or all branches for an administrator (`core.report_db`).

## Data, backups and upgrades

| What | Where / how |
|---|---|
| Live data | `data/shop.db`. Set the `AGINV_DB` environment variable to keep it somewhere else. |
| Session key | `data/secret.key`, created on first start. |
| Automatic backup | Once a day, to `backups/shop-YYYYMMDD-HHMMSS.db`. The last 30 are kept. |
| Manual backup and restore | **⚙ Admin → Settings**. |
| Database changes in new versions | Applied automatically on start: `schema.sql` uses `CREATE TABLE IF NOT EXISTS`, and new columns are added by `core.MIGRATIONS`. |

## Install, start and update

| Step | Windows | Linux |
|---|---|---|
| Needs | Python 3.10+ from python.org, with "Add python.exe to PATH" ticked | `python3` and `python3-venv` |
| Install once | `install.bat` creates `.venv` and installs Flask and Waitress | `./install.sh` |
| Start | `start.bat` opens <http://localhost:5000> and shows the address for other devices | `./start.sh` |
| Update | Close the app window(s), replace the program files but keep `data/` and `backups/` (or run `git pull` in a git copy), then run `start.bat` once | the same, with `start.sh` |

Only **one** copy of the app should run at a time. Close the old window before starting a new one.

Source code: <https://github.com/AGSoftwareplc/AG_Inventory>
