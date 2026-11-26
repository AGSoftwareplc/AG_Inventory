-- Mizan database schema (SQLite).
-- Money columns are INTEGER cents. Quantities are REAL (decimal quantities allowed).
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  full_name TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- Branches of this business (shops, stores, warehouses). Branch 1 = the Main branch.
CREATE TABLE IF NOT EXISTS branches (
  id INTEGER PRIMARY KEY,
  code TEXT NOT NULL UNIQUE COLLATE NOCASE,   -- used in document numbers, e.g. MAIN-S-000042
  name TEXT NOT NULL,
  address TEXT NOT NULL DEFAULT '',
  phone TEXT NOT NULL DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS user_branches (   -- which branches a user may work in
  user_id INTEGER NOT NULL REFERENCES users(id),
  branch_id INTEGER NOT NULL REFERENCES branches(id),
  PRIMARY KEY (user_id, branch_id)
);

CREATE TABLE IF NOT EXISTS product_stock (   -- each branch's own stock and weighted average cost
  product_id INTEGER NOT NULL REFERENCES products(id),
  branch_id INTEGER NOT NULL REFERENCES branches(id),
  stock REAL NOT NULL DEFAULT 0,
  avg_cost REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (product_id, branch_id)
);

CREATE TABLE IF NOT EXISTS transfers (   -- stock sent from one branch to another
  id INTEGER PRIMARY KEY,
  ref_no TEXT NOT NULL UNIQUE,
  from_branch INTEGER NOT NULL REFERENCES branches(id),
  to_branch INTEGER NOT NULL REFERENCES branches(id),
  status TEXT NOT NULL DEFAULT 'In transit',   -- In transit, Received, Cancelled
  date TEXT NOT NULL,
  notes TEXT NOT NULL DEFAULT '',
  user_id INTEGER REFERENCES users(id),
  received_by INTEGER REFERENCES users(id),
  received_at TEXT,
  cancel_reason TEXT
);
CREATE TABLE IF NOT EXISTS transfer_items (
  id INTEGER PRIMARY KEY,
  transfer_id INTEGER NOT NULL REFERENCES transfers(id),
  product_id INTEGER NOT NULL REFERENCES products(id),
  qty REAL NOT NULL,
  unit_cost REAL NOT NULL,          -- average cost at the sending branch
  received_qty REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS role_permissions (
  role TEXT NOT NULL, perm TEXT NOT NULL, PRIMARY KEY (role, perm)
);

CREATE TABLE IF NOT EXISTS categories (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE COLLATE NOCASE,
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS products (
  id INTEGER PRIMARY KEY,
  code TEXT NOT NULL UNIQUE COLLATE NOCASE,
  name TEXT NOT NULL,
  category_id INTEGER REFERENCES categories(id),
  description TEXT NOT NULL DEFAULT '',
  unit TEXT NOT NULL DEFAULT 'pcs',
  barcode TEXT UNIQUE,
  price INTEGER NOT NULL DEFAULT 0,      -- selling price, cents
  avg_cost REAL NOT NULL DEFAULT 0,      -- weighted average cost, cents (fractional allowed)
  stock REAL NOT NULL DEFAULT 0,
  min_stock REAL NOT NULL DEFAULT 0,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS ix_products_name ON products(name);

CREATE TABLE IF NOT EXISTS price_changes (
  id INTEGER PRIMARY KEY,
  product_id INTEGER NOT NULL REFERENCES products(id),
  old_price INTEGER NOT NULL, new_price INTEGER NOT NULL,
  user_id INTEGER REFERENCES users(id),
  at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS stock_moves (
  id INTEGER PRIMARY KEY,
  product_id INTEGER NOT NULL REFERENCES products(id),
  qty REAL NOT NULL,                      -- signed: + in, - out
  move_type TEXT NOT NULL,
  ref_type TEXT NOT NULL DEFAULT '',
  ref_id INTEGER,
  ref_no TEXT NOT NULL DEFAULT '',
  unit_cost REAL NOT NULL DEFAULT 0,
  balance REAL NOT NULL,                  -- stock after this move
  reason TEXT NOT NULL DEFAULT '',
  user_id INTEGER REFERENCES users(id),
  at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS ix_moves_product ON stock_moves(product_id, at);
CREATE INDEX IF NOT EXISTS ix_moves_at ON stock_moves(at);

CREATE TABLE IF NOT EXISTS suppliers (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  contact_person TEXT NOT NULL DEFAULT '',
  phone TEXT NOT NULL DEFAULT '',
  address TEXT NOT NULL DEFAULT '',
  email TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS customers (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  phone TEXT NOT NULL DEFAULT '',
  address TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS purchases (
  id INTEGER PRIMARY KEY,
  ref_no TEXT NOT NULL UNIQUE,
  supplier_id INTEGER NOT NULL REFERENCES suppliers(id),
  date TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'Draft',  -- Draft, Confirmed, Partially Received, Received, Cancelled
  discount INTEGER NOT NULL DEFAULT 0,
  total INTEGER NOT NULL DEFAULT 0,      -- after discount
  returned_value INTEGER NOT NULL DEFAULT 0,
  paid INTEGER NOT NULL DEFAULT 0,
  notes TEXT NOT NULL DEFAULT '',
  cancel_reason TEXT NOT NULL DEFAULT '',
  user_id INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS purchase_items (
  id INTEGER PRIMARY KEY,
  purchase_id INTEGER NOT NULL REFERENCES purchases(id),
  product_id INTEGER NOT NULL REFERENCES products(id),
  qty REAL NOT NULL,
  unit_cost INTEGER NOT NULL,            -- before discount, cents
  received_qty REAL NOT NULL DEFAULT 0,
  returned_qty REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS purchase_payments (
  id INTEGER PRIMARY KEY,
  purchase_id INTEGER NOT NULL REFERENCES purchases(id),
  amount INTEGER NOT NULL,
  method TEXT NOT NULL,
  date TEXT NOT NULL,
  user_id INTEGER REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS sales (
  id INTEGER PRIMARY KEY,
  receipt_no TEXT NOT NULL UNIQUE,
  customer_id INTEGER REFERENCES customers(id),
  date TEXT NOT NULL,
  subtotal INTEGER NOT NULL,
  discount INTEGER NOT NULL DEFAULT 0,
  total INTEGER NOT NULL,
  paid INTEGER NOT NULL DEFAULT 0,       -- payments applied to this sale (never above total)
  tendered INTEGER NOT NULL DEFAULT 0,   -- cash handed over at the till
  change_due INTEGER NOT NULL DEFAULT 0,
  refunded INTEGER NOT NULL DEFAULT 0,
  method TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'Completed', -- Completed, Cancelled
  cancel_reason TEXT NOT NULL DEFAULT '',
  user_id INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS ix_sales_date ON sales(date);

CREATE TABLE IF NOT EXISTS sale_items (
  id INTEGER PRIMARY KEY,
  sale_id INTEGER NOT NULL REFERENCES sales(id),
  product_id INTEGER NOT NULL REFERENCES products(id),
  qty REAL NOT NULL,
  price INTEGER NOT NULL,                -- unit selling price at time of sale
  cost REAL NOT NULL,                    -- unit average cost at time of sale
  returned_qty REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sale_payments (
  id INTEGER PRIMARY KEY,
  sale_id INTEGER NOT NULL REFERENCES sales(id),
  amount INTEGER NOT NULL,
  method TEXT NOT NULL,
  date TEXT NOT NULL,
  user_id INTEGER REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS returns (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL,                    -- sale / purchase
  doc_id INTEGER NOT NULL,
  item_id INTEGER NOT NULL,
  product_id INTEGER NOT NULL REFERENCES products(id),
  qty REAL NOT NULL,
  amount INTEGER NOT NULL,               -- refund to customer / credit from supplier
  cost REAL NOT NULL,                    -- unit cost moved
  reason TEXT NOT NULL,
  date TEXT NOT NULL,
  user_id INTEGER REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS stocktakes (
  id INTEGER PRIMARY KEY,
  date TEXT NOT NULL,
  notes TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'Open',   -- Open, Posted
  user_id INTEGER REFERENCES users(id)
);
CREATE TABLE IF NOT EXISTS stocktake_items (
  stocktake_id INTEGER NOT NULL REFERENCES stocktakes(id),
  product_id INTEGER NOT NULL REFERENCES products(id),
  system_qty REAL NOT NULL,
  counted_qty REAL,
  PRIMARY KEY (stocktake_id, product_id)
);

CREATE TABLE IF NOT EXISTS expense_categories (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE COLLATE NOCASE
);
CREATE TABLE IF NOT EXISTS expenses (
  id INTEGER PRIMARY KEY,
  ref_no TEXT NOT NULL UNIQUE,
  category_id INTEGER NOT NULL REFERENCES expense_categories(id),
  description TEXT NOT NULL DEFAULT '',
  amount INTEGER NOT NULL,
  date TEXT NOT NULL,
  method TEXT NOT NULL,
  user_id INTEGER REFERENCES users(id),
  reversal_of INTEGER REFERENCES expenses(id)   -- set on a correcting (negative) entry
);
CREATE INDEX IF NOT EXISTS ix_expenses_date ON expenses(date);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY,
  user_id INTEGER, action TEXT NOT NULL, details TEXT NOT NULL DEFAULT '',
  at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- FIFO stock batches: every delivery (or other stock coming in) is a batch with its own cost.
-- Sales and other stock going out take from the batch that expires first, then the oldest one.
CREATE TABLE IF NOT EXISTS stock_batches (
  id INTEGER PRIMARY KEY,
  product_id INTEGER NOT NULL REFERENCES products(id),
  branch_id INTEGER NOT NULL DEFAULT 1,
  source TEXT NOT NULL,                  -- purchase, opening, transfer_in, correction, stocktake, ...
  ref_id INTEGER,
  ref_no TEXT NOT NULL DEFAULT '',
  purchase_item_id INTEGER REFERENCES purchase_items(id),
  received TEXT NOT NULL,                -- date the stock came in (FIFO order)
  expiry TEXT,                           -- optional expiry date: sold before batches that expire later
  qty REAL NOT NULL,                     -- quantity that came in
  remaining REAL NOT NULL,               -- quantity still in stock
  unit_cost REAL NOT NULL,               -- cents
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS ix_batches_open ON stock_batches(product_id, branch_id, remaining);
CREATE INDEX IF NOT EXISTS ix_batches_item ON stock_batches(purchase_item_id);

CREATE TABLE IF NOT EXISTS batch_moves (   -- which batches each stock movement used, at which cost
  id INTEGER PRIMARY KEY,
  batch_id INTEGER NOT NULL REFERENCES stock_batches(id),
  move_id INTEGER NOT NULL REFERENCES stock_moves(id),
  qty REAL NOT NULL,                     -- signed: + into the batch, - out of it
  unit_cost REAL NOT NULL,
  back_qty REAL NOT NULL DEFAULT 0       -- for a - line: how much has since come back (return / cancel)
);
CREATE INDEX IF NOT EXISTS ix_bmoves_move ON batch_moves(move_id);
CREATE INDEX IF NOT EXISTS ix_bmoves_batch ON batch_moves(batch_id);

CREATE TABLE IF NOT EXISTS used_tokens (token TEXT PRIMARY KEY);

-- Serial numbers: one row per serial-numbered item (products with serial_tracked = 1).
CREATE TABLE IF NOT EXISTS serials (
  id INTEGER PRIMARY KEY,
  product_id INTEGER NOT NULL REFERENCES products(id),
  serial TEXT NOT NULL UNIQUE COLLATE NOCASE,   -- unique in the whole business
  branch_id INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'In stock',      -- In stock, Sold, Returned to supplier, In transit, Damaged, Expired, Lost, Removed
  batch_id INTEGER REFERENCES stock_batches(id),
  purchase_id INTEGER REFERENCES purchases(id),
  purchase_item_id INTEGER REFERENCES purchase_items(id),
  sale_id INTEGER REFERENCES sales(id),
  sale_item_id INTEGER REFERENCES sale_items(id),
  transfer_id INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS ix_serials_product ON serials(product_id, branch_id, status);
CREATE TABLE IF NOT EXISTS serial_events (   -- the life of each serial-numbered item
  id INTEGER PRIMARY KEY,
  serial_id INTEGER NOT NULL REFERENCES serials(id),
  event TEXT NOT NULL,                   -- Purchased, Registered, Sold, Customer return, Sale cancelled, ...
  ref_type TEXT NOT NULL DEFAULT '',
  ref_id INTEGER,
  ref_no TEXT NOT NULL DEFAULT '',
  branch_id INTEGER,
  user_id INTEGER REFERENCES users(id),
  at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS ix_serial_events ON serial_events(serial_id);

-- A return to the supplier is its own transaction (one or more lines), always tied to the original purchase.
CREATE TABLE IF NOT EXISTS purchase_returns (
  id INTEGER PRIMARY KEY,
  ref_no TEXT NOT NULL UNIQUE,           -- e.g. MAIN-PR-000003
  purchase_id INTEGER NOT NULL REFERENCES purchases(id),
  date TEXT NOT NULL,
  amount INTEGER NOT NULL DEFAULT 0,     -- credit from the supplier, cents
  notes TEXT NOT NULL DEFAULT '',
  user_id INTEGER REFERENCES users(id),
  branch_id INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

-- ---------------------------------------------------------------- pharmacy module (optional)
-- Used only when a business switches on Admin -> Settings -> Pharmacy. A product with a medicines
-- row is a medicine; products without one are general goods (medical supplies, cosmetics, ...).
CREATE TABLE IF NOT EXISTS medicines (
  product_id INTEGER PRIMARY KEY REFERENCES products(id),
  generic_name TEXT NOT NULL DEFAULT '',
  brand TEXT NOT NULL DEFAULT '',
  manufacturer TEXT NOT NULL DEFAULT '',
  dosage_form TEXT NOT NULL DEFAULT '',     -- Tablet, Capsule, Syrup, Injection, Cream, Drops, ...
  strength TEXT NOT NULL DEFAULT '',        -- e.g. 500 mg, 250 mg/5 ml
  rx_class TEXT NOT NULL DEFAULT 'OTC',     -- OTC, POM, Narcotic, Psychotropic, Precursor
  storage TEXT NOT NULL DEFAULT '15-25 °C', -- 15-25 °C, 2-8 °C (cold chain), Frozen, Protect from light
  reg_no TEXT NOT NULL DEFAULT '',          -- EFDA registration (market authorization) number
  reg_expiry TEXT                           -- registration expiry date
);

CREATE TABLE IF NOT EXISTS patients (       -- the person a medicine is for (not always the paying customer)
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  phone TEXT NOT NULL DEFAULT '',
  birth_date TEXT,                          -- or the age typed (stored as an estimated birth year)
  sex TEXT NOT NULL DEFAULT '',
  weight_kg REAL,                           -- required under 12 years
  allergies TEXT NOT NULL DEFAULT '',       -- comma separated medicines / ingredients
  pregnancy TEXT NOT NULL DEFAULT '',       -- '', Pregnant, Breastfeeding
  conditions TEXT NOT NULL DEFAULT '',      -- chronic conditions
  customer_id INTEGER REFERENCES customers(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS prescribers (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  profession TEXT NOT NULL DEFAULT '',
  licence_no TEXT NOT NULL DEFAULT '',
  facility TEXT NOT NULL DEFAULT '',
  phone TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS prescriptions (
  id INTEGER PRIMARY KEY,
  ref_no TEXT NOT NULL UNIQUE,              -- our number, e.g. MAIN-RX-000012
  rx_no TEXT NOT NULL DEFAULT '',           -- number written on the prescription paper
  form_serial TEXT NOT NULL DEFAULT '',     -- serial of the EFDA form (controlled medicines)
  date TEXT NOT NULL,                       -- date the prescriber issued it
  patient_id INTEGER NOT NULL REFERENCES patients(id),
  prescriber_id INTEGER NOT NULL REFERENCES prescribers(id),
  status TEXT NOT NULL DEFAULT 'Registered', -- Registered, Verified, Rejected, Partially dispensed, Dispensed, Cancelled
  scan_file TEXT NOT NULL DEFAULT '',       -- uploaded scan / photo of the paper
  notes TEXT NOT NULL DEFAULT '',
  verified_by INTEGER REFERENCES users(id),
  verified_at TEXT,
  interventions TEXT NOT NULL DEFAULT '',   -- warnings the pharmacist accepted, with the reason
  counselling TEXT NOT NULL DEFAULT '',
  reject_reason TEXT NOT NULL DEFAULT '',
  user_id INTEGER REFERENCES users(id),
  branch_id INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS ix_rx_patient ON prescriptions(patient_id);

CREATE TABLE IF NOT EXISTS rx_items (
  id INTEGER PRIMARY KEY,
  prescription_id INTEGER NOT NULL REFERENCES prescriptions(id),
  product_id INTEGER NOT NULL REFERENCES products(id),
  dose TEXT NOT NULL DEFAULT '',            -- e.g. 1 tablet
  frequency TEXT NOT NULL DEFAULT '',       -- e.g. 3 times a day
  duration TEXT NOT NULL DEFAULT '',        -- e.g. 7 days
  instructions TEXT NOT NULL DEFAULT '',    -- e.g. after food
  qty REAL NOT NULL,                        -- quantity prescribed (selling unit)
  dispensed_qty REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS dispensings (    -- each time medicines of a prescription were handed over
  id INTEGER PRIMARY KEY,
  prescription_id INTEGER NOT NULL REFERENCES prescriptions(id),
  sale_id INTEGER NOT NULL REFERENCES sales(id),
  pharmacist_id INTEGER NOT NULL REFERENCES users(id),
  checked_by INTEGER REFERENCES users(id),  -- second person (controlled medicines)
  at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS recalls (
  id INTEGER PRIMARY KEY,
  ref_no TEXT NOT NULL UNIQUE,
  product_id INTEGER NOT NULL REFERENCES products(id),
  batch_no TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT '',          -- EFDA, Manufacturer, Supplier
  recall_class TEXT NOT NULL DEFAULT '',    -- Class I, II, III
  reference TEXT NOT NULL DEFAULT '',
  action TEXT NOT NULL DEFAULT '',          -- what must be done
  date TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'Open',      -- Open, Closed
  user_id INTEGER REFERENCES users(id),
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS recall_contacts (  -- each attempt to tell a customer / patient about a recall
  id INTEGER PRIMARY KEY,
  recall_id INTEGER NOT NULL REFERENCES recalls(id),
  sale_id INTEGER REFERENCES sales(id),
  note TEXT NOT NULL,
  user_id INTEGER REFERENCES users(id),
  at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS disposals (      -- destruction of expired, damaged or recalled stock
  id INTEGER PRIMARY KEY,
  ref_no TEXT NOT NULL UNIQUE,
  batch_id INTEGER NOT NULL REFERENCES stock_batches(id),
  product_id INTEGER NOT NULL REFERENCES products(id),
  qty REAL NOT NULL,
  method TEXT NOT NULL,
  reason TEXT NOT NULL,
  witness1 INTEGER NOT NULL REFERENCES users(id),
  witness2 INTEGER NOT NULL REFERENCES users(id),
  certificate TEXT NOT NULL DEFAULT '',     -- uploaded disposal certificate
  date TEXT NOT NULL,
  move_id INTEGER REFERENCES stock_moves(id),
  user_id INTEGER REFERENCES users(id),
  branch_id INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
