-- 005 receipts that agree with the ledger, budgets that can be swept, and a
--     record of what the filesystem still owes us
--
-- Three separate jobs, in one file because they all land at the same moment and
-- a migration per idea would mean three checksums to keep straight for no gain.
--
-- Nothing here adds a column. `attachments`, `limits` and `limit_alerts` were
-- designed in 001 and have been sitting unused since; what they were missing was
-- never structure, it was the rules that keep them honest. Those are triggers.

-- ============================================================== receipts
--
-- A receipt photo and "there was no receipt" cannot both be true. 004 made
-- `receiptless` a property of the transaction rather than a merchant, which is
-- what made this contradiction expressible in the first place.
--
-- The agreed resolution is asymmetric on purpose:
--
--   attaching a photo to a receiptless entry  →  clears the flag
--   ticking receiptless on an entry with photos  →  refused
--
-- because the two acts carry different amounts of information. A photo is
-- evidence: it settles the question, and the flag was simply wrong. Ticking a
-- box is a claim, and a claim that contradicts evidence already in hand is the
-- one worth stopping. receipts.py does the clearing and says so; these triggers
-- are what stops anything writing around it.

CREATE TRIGGER trg_attachments_not_receiptless BEFORE INSERT ON attachments
WHEN (SELECT receiptless FROM transactions WHERE id = NEW.transaction_id) = 1
BEGIN
  SELECT RAISE(ABORT, 'attachments: that entry is marked as having no receipt');
END;

-- BEFORE UPDATE OF fires whenever a statement *names* the column, changed or
-- not — and transactions._prepare() rewrites every column on every edit. The
-- OLD/NEW guard is what keeps this from firing on an ordinary save.
CREATE TRIGGER trg_transactions_receiptless_conflict
BEFORE UPDATE OF receiptless ON transactions
WHEN NEW.receiptless = 1 AND OLD.receiptless = 0
 AND EXISTS (SELECT 1 FROM attachments WHERE transaction_id = OLD.id)
BEGIN
  SELECT RAISE(ABORT, 'transactions: this entry has a receipt photo attached');
END;

-- Two rows must never point at one file, or deleting either would take the
-- other's picture with it.
CREATE UNIQUE INDEX ux_attachments_file  ON attachments(file_path);
CREATE UNIQUE INDEX ux_attachments_thumb ON attachments(thumb_path);

-- Everything is re-encoded by Pillow before it is written, so the stored type is
-- ours rather than the browser's. This is the backstop for the day someone adds
-- a second write path and forgets that.
CREATE TRIGGER trg_attachments_mime BEFORE INSERT ON attachments
WHEN NEW.mime NOT IN ('image/jpeg','image/png')
BEGIN
  SELECT RAISE(ABORT, 'attachments: only JPEG and PNG are stored');
END;

-- ======================================================== orphaned files
--
-- ON DELETE CASCADE removes the row. It does not remove the JPEG.
--
-- Deleting the file from Python right after the DELETE would work until the
-- process dies in between, and then the picture is on disk with nothing left
-- pointing at it — invisible, unreferenced, and there forever. So the debt is
-- recorded in the same transaction that creates it, and `receipts.reap()` pays
-- it off afterwards. A crash costs a delayed unlink, never a leak.
--
-- This trigger only fires for cascaded deletes because db.py sets
-- PRAGMA recursive_triggers = ON. SQLite's foreign-key actions do not fire
-- triggers otherwise, which would silently skip exactly the case this exists
-- for: deleting a transaction that has photos.

CREATE TABLE orphaned_files (
  id          INTEGER PRIMARY KEY,
  -- Relative to UPLOAD_DIR, the same as attachments.file_path.
  path        TEXT NOT NULL,
  orphaned_at TEXT NOT NULL
);

CREATE TRIGGER trg_attachments_orphan AFTER DELETE ON attachments
BEGIN
  INSERT INTO orphaned_files (path, orphaned_at) VALUES
    (OLD.file_path,  strftime('%Y-%m-%dT%H:%M:%SZ','now')),
    (OLD.thumb_path, strftime('%Y-%m-%dT%H:%M:%SZ','now'));
END;

-- ================================================================ limits
--
-- These are budgets: what the household means to spend. They are unrelated to
-- the `credit_limit_*` and `withdrawal_limit_minor` columns on `accounts`, which
-- are ceilings the bank set and which do block a save. A budget never blocks
-- anything — it warns, and the spec is explicit about that.
--
-- A budget is compared against a month of spending converted into the base
-- currency, so a budget denominated in anything else would be a comparison
-- between two different units. Rather than convert a ceiling someone set in
-- their head, this refuses to store one.

CREATE TRIGGER trg_limits_currency_insert BEFORE INSERT ON limits
WHEN NEW.currency <> COALESCE((SELECT value FROM settings WHERE key = 'base_currency'), 'EGP')
BEGIN
  SELECT RAISE(ABORT, 'limits: a budget is set in the household base currency');
END;

CREATE TRIGGER trg_limits_currency_update BEFORE UPDATE OF currency ON limits
WHEN NEW.currency <> COALESCE((SELECT value FROM settings WHERE key = 'base_currency'), 'EGP')
BEGIN
  SELECT RAISE(ABORT, 'limits: a budget is set in the household base currency');
END;

-- 001's CHECK already pairs scope_type = 'household' with a NULL scope_id. The
-- other four directions were left open, and a limit scoped to a category that
-- names no category is a limit that silently measures nothing.
CREATE TRIGGER trg_limits_scope_insert BEFORE INSERT ON limits
WHEN NEW.scope_type <> 'household' AND NEW.scope_id IS NULL
BEGIN
  SELECT RAISE(ABORT, 'limits: that budget has to say what it is about');
END;

CREATE TRIGGER trg_limits_scope_update BEFORE UPDATE OF scope_type, scope_id ON limits
WHEN NEW.scope_type <> 'household' AND NEW.scope_id IS NULL
BEGIN
  SELECT RAISE(ABORT, 'limits: that budget has to say what it is about');
END;

-- The sweep reads active limits and then, per limit, asks limit_alerts what it
-- has already said. That second query is the one that runs once per limit per
-- run, so it is the one worth an index.
CREATE INDEX ix_limit_alerts_period ON limit_alerts(limit_id, period_key);
