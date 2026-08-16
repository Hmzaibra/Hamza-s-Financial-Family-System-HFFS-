-- 006 an account can belong to more than one person
--
-- `accounts.owner_id` held exactly one user, which does not survive contact with
-- a household: a joint current account belongs to both people, a card on it may
-- belong to one of them, and "shared" (NULL) was the only way to say "both" —
-- which loses the information rather than recording it.
--
-- Ownership here is still what it always was: display, grouping, and now the
-- "My accounts" screen. **It carries no visibility meaning.** Section 4 keys off
-- the transaction's owner, never the account's, and that is deliberate — a
-- shared bank account must not expose one person's spending to everyone else who
-- draws on it. Adding a second owner to an account changes who sees the account
-- in a list. It changes nothing about who sees the purchases made from it.

CREATE TABLE account_owners (
  account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  -- No ON DELETE on the user side: users are soft-deleted (is_active = 0)
  -- precisely because their rows are pointed at, and an account quietly losing
  -- an owner would be the same silent history-rewrite that rule out.
  user_id    INTEGER NOT NULL REFERENCES users(id),
  created_at TEXT NOT NULL,
  PRIMARY KEY (account_id, user_id)
);

-- The shape "My accounts" queries: everything one person can see, in one scan.
CREATE INDEX ix_account_owners_user ON account_owners(user_id, account_id);

-- Everything the single column knew, moved across without interpretation. A NULL
-- owner_id meant "the household's" and becomes no rows, which is the same
-- statement in the new shape: an account nobody has claimed is everybody's.
INSERT INTO account_owners (account_id, user_id, created_at)
SELECT id, owner_id, strftime('%Y-%m-%dT%H:%M:%SZ', 'now')
  FROM accounts WHERE owner_id IS NOT NULL;

UPDATE accounts SET owner_id = NULL;

-- `owner_id` stays on the table for the same reason `merchants.is_system` did in
-- 004: 001 is immutable, and rebuilding a table that six triggers reference is a
-- bigger risk than a column with nothing in it. Nothing writes it any more.
-- These triggers make that a rule rather than a habit — a stray INSERT that sets
-- it would create a second, invisible source of truth about who owns what, and
-- the two would disagree within a week.

CREATE TRIGGER trg_accounts_owner_id_retired_insert BEFORE INSERT ON accounts
WHEN NEW.owner_id IS NOT NULL
BEGIN
  SELECT RAISE(ABORT, 'accounts: ownership lives in account_owners since 006');
END;

CREATE TRIGGER trg_accounts_owner_id_retired_update BEFORE UPDATE OF owner_id ON accounts
WHEN NEW.owner_id IS NOT NULL
BEGIN
  SELECT RAISE(ABORT, 'accounts: ownership lives in account_owners since 006');
END;
