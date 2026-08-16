-- 004 receipts as their own fact, card expiry, instapay handles, cached fx rates
--
-- The largest change here is conceptual rather than structural: "there was no
-- receipt" stops being a merchant and becomes a property of the transaction.
--
-- Receipt-less was seeded in 002 as a system merchant, which quietly welded two
-- independent facts together. A street vendor with no name and no paper is one
-- row; so is a named shop that hands over nothing; so is an unnamed stall that
-- does print a slip. Picking "Receipt-less" from a list of merchants forced a
-- choice between recording who you paid and recording whether you can prove it.
-- They are separate columns now, and either can be true on its own.
--
-- The seeded merchant is retired rather than kept: leaving it in the list would
-- mean two ways to say the same thing, and the one that survives is the one
-- that composes.

-- --------------------------------------------------------- receipts

ALTER TABLE transactions ADD COLUMN receiptless INTEGER NOT NULL DEFAULT 0
  CHECK (receiptless IN (0,1));

-- Everything previously filed under the system merchant meant exactly this.
UPDATE transactions
   SET receiptless = 1
 WHERE merchant_id IN (SELECT id FROM merchants WHERE is_system = 1);

-- ...and said nothing about who was paid, so it stops claiming to.
UPDATE transactions
   SET merchant_id = NULL
 WHERE merchant_id IN (SELECT id FROM merchants WHERE is_system = 1);

UPDATE merchants SET default_account_id = NULL, default_category_id = NULL
 WHERE is_system = 1;

DELETE FROM merchants WHERE is_system = 1;

-- `is_system` stays on the table because 001 is immutable and rewriting an
-- applied migration is the one thing this runner refuses to do. Nothing sets it
-- to 1 any more; it is a column with no rows in it, which is cheaper than a
-- table rebuild to remove.

-- ------------------------------------------------------ card expiry

-- 'YYYY-MM'. A card expires at the end of its printed month, so the day is not
-- information anyone has.
ALTER TABLE accounts ADD COLUMN card_expires_on TEXT;

CREATE TRIGGER trg_accounts_card_expiry_insert BEFORE INSERT ON accounts
WHEN NEW.type IN ('credit_card','debit_card')
BEGIN
  SELECT CASE
    WHEN NEW.card_expires_on IS NULL
      THEN RAISE(ABORT, 'accounts: a card needs an expiry date')
    WHEN NEW.card_expires_on NOT GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]'
      THEN RAISE(ABORT, 'accounts: expiry must look like 2029-07')
  END;
END;

CREATE TRIGGER trg_accounts_card_expiry_update
BEFORE UPDATE OF type, card_expires_on ON accounts
WHEN NEW.type IN ('credit_card','debit_card')
BEGIN
  SELECT CASE
    WHEN NEW.card_expires_on IS NULL
      THEN RAISE(ABORT, 'accounts: a card needs an expiry date')
    WHEN NEW.card_expires_on NOT GLOB '[0-9][0-9][0-9][0-9]-[0-1][0-9]'
      THEN RAISE(ABORT, 'accounts: expiry must look like 2029-07')
  END;
END;

-- --------------------------------------------------- instapay handle

-- Stored apart from the display name so the edit form can show the two boxes it
-- asked for, while `name` stays the single merged string every other screen and
-- the UNIQUE(name) index already work with.
ALTER TABLE accounts ADD COLUMN instapay_handle TEXT;

CREATE TRIGGER trg_accounts_instapay_handle_insert BEFORE INSERT ON accounts
WHEN NEW.type = 'instapay'
 AND (NEW.instapay_handle IS NULL OR NEW.instapay_handle NOT GLOB '@?*')
BEGIN
  SELECT RAISE(ABORT, 'accounts: an instapay account needs a handle like @name');
END;

CREATE TRIGGER trg_accounts_instapay_handle_update
BEFORE UPDATE OF type, instapay_handle ON accounts
WHEN NEW.type = 'instapay'
 AND (NEW.instapay_handle IS NULL OR NEW.instapay_handle NOT GLOB '@?*')
BEGIN
  SELECT RAISE(ABORT, 'accounts: an instapay account needs a handle like @name');
END;

-- ------------------------------------------------------- fx rates

-- A cache, never a source of truth. The rate that matters is the one captured
-- on the transaction at entry (hard rule: the rate on the day is unrecoverable
-- afterwards), and this table only exists so the entry form can offer a sensible
-- number instead of an empty box. Refreshed by `flask fetch-rates`, which is the
-- only part of the app that ever touches the public internet — and it runs from
-- cron, never from a request.
CREATE TABLE fx_rates (
  base         TEXT NOT NULL,
  currency     TEXT NOT NULL,
  -- How many units of `base` one unit of `currency` is worth, which is the
  -- direction transactions.fx_rate_to_base is stored in.
  rate_to_base REAL NOT NULL CHECK (rate_to_base > 0),
  fetched_at   TEXT NOT NULL,
  source       TEXT NOT NULL,
  PRIMARY KEY (base, currency)
);

-- ----------------------------------------------------------- notes
--
-- withdrawal_limit_minor (003) now means "per calendar day", and the two credit
-- limits mean "per calendar month". Both are enforced in transactions.py by
-- summing the period rather than checking one entry, so the column names stayed
-- as they are: renaming a column that triggers already reference is a bigger
-- risk than a comment.
