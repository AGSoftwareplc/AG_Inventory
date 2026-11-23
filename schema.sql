-- AG Inventory database schema (SQLite).
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
  user_id INTEGER REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS ix_expenses_date ON expenses(date);

CREATE TABLE IF NOT EXISTS audit_log (
  id INTEGER PRIMARY KEY,
  user_id INTEGER, action TEXT NOT NULL, details TEXT NOT NULL DEFAULT '',
  at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);

CREATE TABLE IF NOT EXISTS used_tokens (token TEXT PRIMARY KEY);
