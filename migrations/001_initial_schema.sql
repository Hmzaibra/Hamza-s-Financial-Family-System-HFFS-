-- 001 initial schema
--
-- Foreign keys are enabled per-connection in db.py (SQLite defaults them OFF and
-- the pragma is not persistent), so ON DELETE CASCADE below only bites because
-- every connection this app opens sets it.
--
-- schema_migrations is created by the runner itself, not here, so that the
-- runner has somewhere to record this file having been applied.

-- ---------------------------------------------------------------- users

CREATE TABLE users (
  id               INTEGER PRIMARY KEY,
  username         TEXT NOT NULL COLLATE NOCASE,
  display_name     TEXT NOT NULL,
  password_hash    TEXT NOT NULL,
  role             TEXT NOT NULL CHECK (role IN ('admin','member')),
  default_shared   INTEGER NOT NULL DEFAULT 1 CHECK (default_shared IN (0,1)),
  -- "Today" on the entry form is derived from the logged-in person's zone, so a
  -- 1am purchase files under their calendar day wherever the server sits.
  timezone         TEXT NOT NULL DEFAULT 'Europe/Berlin',
  telegram_chat_id TEXT,
  -- Soft delete only. A user with transactions is never hard-deleted; doing so
  -- would orphan the rows that visibility depends on.
  is_active        INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
  created_at       TEXT NOT NULL,
  UNIQUE (username)
);

-- ------------------------------------------------------------- accounts

CREATE TABLE accounts (
  id                    INTEGER PRIMARY KEY,
  name                  TEXT NOT NULL COLLATE NOCASE,
  type                  TEXT NOT NULL CHECK (type IN
                          ('bank','credit_card','debit_card','instapay','cash','wallet')),
  currency              TEXT NOT NULL DEFAULT 'EGP',
  -- NULL means shared/household. This is for display and filtering only; it
  -- carries no visibility meaning (section 4 keys off the transaction's owner).
  owner_id              INTEGER REFERENCES users(id),
  -- Deliberately allowed to be negative: a credit card opens in debt.
  opening_balance_minor INTEGER NOT NULL DEFAULT 0,
  is_active             INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
  sort_order            INTEGER NOT NULL DEFAULT 100,
  created_at            TEXT NOT NULL,
  UNIQUE (name)
);

CREATE INDEX ix_accounts_order ON accounts(is_active, sort_order, name);

-- ----------------------------------------------------------- categories

CREATE TABLE categories (
  id         INTEGER PRIMARY KEY,
  name       TEXT NOT NULL COLLATE NOCASE,
  parent_id  INTEGER REFERENCES categories(id),
  icon       TEXT,
  is_active  INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
  sort_order INTEGER NOT NULL DEFAULT 100
);

-- SQLite treats NULLs as distinct in a UNIQUE constraint, so a plain
-- UNIQUE(parent_id, name) would happily allow two top-level "Transport" rows.
CREATE UNIQUE INDEX ux_categories_child ON categories(parent_id, name) WHERE parent_id IS NOT NULL;
CREATE UNIQUE INDEX ux_categories_root  ON categories(name)            WHERE parent_id IS NULL;
CREATE INDEX ix_categories_parent ON categories(parent_id, sort_order, name);

-- Section 2: one level of nesting, no deeper. Enforced here rather than only in
-- the form, because the form is not the only thing that will ever write.
CREATE TRIGGER trg_categories_depth_insert BEFORE INSERT ON categories
WHEN NEW.parent_id IS NOT NULL
  AND (SELECT parent_id FROM categories WHERE id = NEW.parent_id) IS NOT NULL
BEGIN
  SELECT RAISE(ABORT, 'categories: max one level of nesting');
END;

CREATE TRIGGER trg_categories_depth_update BEFORE UPDATE OF parent_id ON categories
WHEN NEW.parent_id IS NOT NULL
  AND (SELECT parent_id FROM categories WHERE id = NEW.parent_id) IS NOT NULL
BEGIN
  SELECT RAISE(ABORT, 'categories: max one level of nesting');
END;

-- A parent may not be reparented under one of its own children, and nothing may
-- be its own parent.
CREATE TRIGGER trg_categories_no_self_parent BEFORE UPDATE OF parent_id ON categories
WHEN NEW.parent_id IS NOT NULL AND NEW.parent_id = NEW.id
BEGIN
  SELECT RAISE(ABORT, 'categories: a category cannot be its own parent');
END;

-- ------------------------------------------------------------ merchants

CREATE TABLE merchants (
  id                  INTEGER PRIMARY KEY,
  name                TEXT NOT NULL COLLATE NOCASE,
  default_category_id INTEGER REFERENCES categories(id),
  default_is_online   INTEGER NOT NULL DEFAULT 0 CHECK (default_is_online IN (0,1)),
  default_account_id  INTEGER REFERENCES accounts(id),
  -- 1 for the seeded Receipt-less row. System merchants cannot be deleted.
  is_system           INTEGER NOT NULL DEFAULT 0 CHECK (is_system IN (0,1)),
  notes               TEXT,
  is_active           INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
  created_at          TEXT NOT NULL,
  -- NOCASE: inline "Add <name>" from the entry form must not be able to create
  -- both "Carrefour" and "carrefour".
  UNIQUE (name)
);

CREATE INDEX ix_merchants_active ON merchants(is_active, name);

-- --------------------------------------------------------- transactions

CREATE TABLE transactions (
  id                   INTEGER PRIMARY KEY,
  user_id              INTEGER NOT NULL REFERENCES users(id),
  occurred_on          TEXT NOT NULL
                         CHECK (occurred_on GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  direction            TEXT NOT NULL CHECK (direction IN ('spend','income','transfer')),
  -- Minor units, always positive. Direction carries the sign (hard rules 1, 2).
  amount_minor         INTEGER NOT NULL CHECK (amount_minor > 0),
  currency             TEXT NOT NULL DEFAULT 'EGP',
  -- Captured at entry because the rate on the day of purchase is the only
  -- correct one and it is unrecoverable afterwards. NULL when currency == base.
  fx_rate_to_base      REAL,
  account_id           INTEGER NOT NULL REFERENCES accounts(id),
  counter_account_id   INTEGER REFERENCES accounts(id),
  -- What actually landed in counter_account. Always populated on transfers,
  -- even same-currency ones, so balance maths never has to branch on NULL.
  counter_amount_minor INTEGER,
  counter_currency     TEXT,
  merchant_id          INTEGER REFERENCES merchants(id),
  category_id          INTEGER REFERENCES categories(id),
  is_online            INTEGER NOT NULL DEFAULT 0 CHECK (is_online IN (0,1)),
  note                 TEXT,
  is_shared            INTEGER NOT NULL CHECK (is_shared IN (0,1)),
  created_at           TEXT NOT NULL,
  updated_at           TEXT NOT NULL,

  CHECK (direction = 'transfer' OR counter_account_id   IS NULL),
  CHECK (direction = 'transfer' OR counter_amount_minor IS NULL),
  CHECK (direction = 'transfer' OR counter_currency     IS NULL),
  CHECK (direction <> 'transfer' OR (counter_account_id   IS NOT NULL
                                 AND counter_amount_minor IS NOT NULL
                                 AND counter_currency     IS NOT NULL)),
  CHECK (counter_amount_minor IS NULL OR counter_amount_minor > 0),
  CHECK (counter_account_id IS NULL OR counter_account_id <> account_id)
);

CREATE INDEX ix_txn_occurred ON transactions(occurred_on);
CREATE INDEX ix_txn_user     ON transactions(user_id);
CREATE INDEX ix_txn_category ON transactions(category_id);
CREATE INDEX ix_txn_account  ON transactions(account_id);
CREATE INDEX ix_txn_counter  ON transactions(counter_account_id);
CREATE INDEX ix_txn_merchant ON transactions(merchant_id);
-- The shape the section 4 visibility filter actually queries.
CREATE INDEX ix_txn_vis      ON transactions(is_shared, user_id, occurred_on);

-- ---------------------------------------------------------- attachments

CREATE TABLE attachments (
  id             INTEGER PRIMARY KEY,
  transaction_id INTEGER NOT NULL REFERENCES transactions(id) ON DELETE CASCADE,
  -- Relative to uploads/, e.g. 2026/08/<uuid>.jpg. Server-generated UUID names;
  -- a client-supplied filename is never trusted or reused.
  file_path      TEXT NOT NULL,
  thumb_path     TEXT NOT NULL,
  mime           TEXT NOT NULL,
  byte_size      INTEGER NOT NULL,
  created_at     TEXT NOT NULL
);

CREATE INDEX ix_attachments_txn ON attachments(transaction_id);

-- --------------------------------------------------------------- limits

CREATE TABLE limits (
  id           INTEGER PRIMARY KEY,
  name         TEXT NOT NULL,
  scope_type   TEXT NOT NULL CHECK (scope_type IN
                 ('household','user','category','account','merchant')),
  scope_id     INTEGER,
  period       TEXT NOT NULL CHECK (period IN ('weekly','monthly')),
  amount_minor INTEGER NOT NULL CHECK (amount_minor > 0),
  currency     TEXT NOT NULL DEFAULT 'EGP',
  warn_pct     INTEGER NOT NULL DEFAULT 80 CHECK (warn_pct BETWEEN 1 AND 100),
  is_active    INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0,1)),
  created_at   TEXT NOT NULL,
  CHECK ((scope_type = 'household') = (scope_id IS NULL))
);

CREATE INDEX ix_limits_active ON limits(is_active, scope_type, scope_id);

-- --------------------------------------------------------- limit_alerts

CREATE TABLE limit_alerts (
  id            INTEGER PRIMARY KEY,
  limit_id      INTEGER NOT NULL REFERENCES limits(id) ON DELETE CASCADE,
  period_key    TEXT NOT NULL,   -- '2026-08' or '2026-W33'
  threshold_pct INTEGER NOT NULL,
  spent_minor   INTEGER NOT NULL,
  sent_at       TEXT NOT NULL,
  -- This constraint is the whole point of the table: fire once per threshold
  -- per period, never nag twice.
  UNIQUE (limit_id, period_key, threshold_pct)
);

-- ------------------------------------------------------------- settings

CREATE TABLE settings (
  key        TEXT PRIMARY KEY,
  value      TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- ------------------------------------------------------- login_attempts

-- Tenth table (spec section 2 said nine). An in-memory counter is wrong the
-- moment gunicorn runs more than one worker, and resets on every restart.
CREATE TABLE login_attempts (
  id           INTEGER PRIMARY KEY,
  username     TEXT NOT NULL COLLATE NOCASE,
  ip           TEXT NOT NULL,
  attempted_at TEXT NOT NULL,
  succeeded    INTEGER NOT NULL CHECK (succeeded IN (0,1))
);

CREATE INDEX ix_login_attempts_user ON login_attempts(username, attempted_at);
CREATE INDEX ix_login_attempts_ip   ON login_attempts(ip, attempted_at);
