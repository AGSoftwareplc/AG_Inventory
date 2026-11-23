# AG Inventory

A simple web app for small and medium shops. It covers the whole flow from purchasing to the stock on the shelf: products, purchases and receiving, a point of sale (POS), returns, customers, suppliers, expenses, a dashboard and reports with Excel export.

- **Easy to install:** you only need Python. There's no Docker and no database server. The data lives in one SQLite file.
- **Works offline:** it runs on the shop network without internet access.
- **Several devices:** one PC runs the app, and other PCs, tablets and phones on the shop Wi-Fi use it in a browser.
- **Several businesses and branches:** each business has its own data, profile and logo. Inside a business, each branch has its own stock, sales and expenses, and stock moves between branches with transfers.

The requirements are in *Shop Inventory Management SRS v1.1*. [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md) shows how each acceptance criterion is checked. [docs/TECH_STACK.md](docs/TECH_STACK.md) describes the technology used and how the code is organised.

## Install (one time)

### Windows
1. Install **Python 3.10 or newer** from <https://www.python.org/downloads/>. On the first installer screen, tick **"Add python.exe to PATH"**.
2. Copy the `AG_Inventory` folder to the shop PC, for example `C:\AG_Inventory`.
3. Double-click **`install.bat`** and wait until it says *Installation finished*.

### Linux
```sh
sudo apt install python3 python3-venv   # if Python is not installed yet
./install.sh
```

## Start

- **Windows:** double-click **`start.bat`**. The browser opens at <http://localhost:5000>.
- **Linux:** run `./start.sh`.

Keep the window open while the shop is working. The window shows the address that other devices on the Wi-Fi should use, for example `http://192.168.1.10:5000`. The first time, Windows may ask whether to allow Python through the firewall. Choose **Allow** for private networks.

**Starter logins** (three Sales Attendants for the counter staff):

| Role | Username | Password |
|---|---|---|
| Administrator | `admin` | `admin123` |
| Sales Attendant | `cashier` | `cashier123` |
| Sales Attendant | `cashier2` | `sales222` |
| Sales Attendant | `cashier3` | `sales333` |
| Inventory Manager | `storekeeper` | `store123` |

**Change these passwords right away.** Each user sees a warning until they do. Then go to **⚙ Admin → Settings** to enter the shop name, address and phone.

## Daily use

The **side menu** follows the order of daily work: *Sell (POS)* and *Dashboard* first, then *Selling*, *Buying*, *Stock*, *Money & reports* and *Administration*. Each user sees only what their role allows. **Help** (top right, or the **F1** key) opens the user manual at the topic for the current page. The full manual can be printed from */manual*.


| Task | Where |
|---|---|
| Sell | **Sell (POS)**: scan a barcode or type a name and press Enter. Leave *Amount paid* empty for the exact amount, then **Complete sale**, then print the receipt (A4). |
| Credit sale | In POS, choose a registered customer and enter the part they paid. Take later payments on the sale page. |
| Customer return or cancellation | **Sales** → open the sale → *Customer return* or *Cancel sale*. |
| Buy stock | **Purchases → New purchase** → *Save and confirm order*. When the goods arrive, open the purchase and click **Receive into stock**. Partial deliveries are fine. Record supplier payments and returns on the same page. |
| Damaged, expired or lost stock | **Inventory → Adjust stock**. A reason is required. |
| Physical count | **Inventory → Stocktake** → start → enter counts → **Post**. |
| Expenses | **Expenses**. To correct a wrong entry, use *Reverse* on it. Entries are never deleted. |
| Reports | **Reports**: choose a report and dates. **Excel** downloads a .xlsx file. For a PDF, click **Print / PDF** and choose *Save as PDF*. |
| Users and rights | **Admin → Users** and **Admin → Role permissions**. |

## Backup and restore

- The app copies the database to the `backups` folder automatically once a day and keeps the last 30 copies.
- **Admin → Settings → Make and download a backup now** saves a copy. Keep copies on a USB stick or another computer.
- **To restore:** go to **Admin → Settings → Restore** and choose a `.db` backup file. The app saves a copy of the current data first.
- **Manual restore:** stop the app, replace `data/shop.db` with the backup file, then start the app again.
- A backup covers the business you are logged in to. Each business has its own data file in `data/` (`shop.db` for the first one, `biz-2.db` and so on), plus `platform.db` with the list of businesses.

**Upgrading to a new version:** stop the app, replace the program files but keep `data/` and `backups/`, then run `install.bat` again. The database is updated automatically.

To move the system to another PC, copy the whole folder, including `data/`, and run `install.bat` there.

## How the money and stock rules work

- **Stock** changes only on purchase receiving, sales, returns, cancellations, adjustments and stocktakes. Each change is recorded in *Inventory → Movements* with the user and a reference.
- **Product cost** uses FIFO stock batches: each delivery is a batch with its own cost, sales take from the batch that expires first, then the oldest, and the cost of goods sold is the cost of the batches used. A purchase discount is spread over the purchase lines.
- **Each sale line** stores its selling price and cost, so later price changes don't alter history.
- **Gross profit** = net sales (after discounts and returns) − cost of goods sold.
- **Net operating profit** = gross profit − expenses.
- **Corrections:** nothing with stock or money effect is deleted. Corrections are made with returns, cancellations or adjustments.

## For developers

```text
app.py         start-up, login, page setup          serve.py      production start (Waitress)
core.py        database, money parsing, permissions  rules.py      ALL stock / cost / money rules
products.py    products and categories               inventory.py  stock, adjustments, stocktake
purchases.py   purchase -> receive -> pay -> return  sales.py      POS, receipts, returns, cancel
contacts.py    suppliers and customers               expenses.py   expenses
reports.py     dashboard, reports, Excel export      admin.py      users, roles, settings, backup
schema.sql     database tables                       templates/    one HTML file per screen
static/        style.css, pos.js                     tests/        automated tests
```

- **Run the tests:** `python -m unittest discover tests`
- **Development server:** `python app.py`
- **Database location:** to keep the database somewhere else, set the `AGINV_DB` environment variable.
- **Making changes:** each change should touch only the files of the module concerned. New tables or columns go in `schema.sql` as `CREATE TABLE IF NOT EXISTS`, or in a small migration in `core.init_db`.
