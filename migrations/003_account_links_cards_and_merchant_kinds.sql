-- 003 account links, card details, and merchant kinds
--
-- Three separate things that all land on reference data:
--
--   1. Accounts can be linked. A debit card or an Instapay handle is a *way of
--      reaching* a bank account or wallet, not a second pot of money. The link
--      is a parent pointer, one level deep, and the rules about which types may
--      sit on which end are triggers rather than form validation — the form is
--      not the only thing that will ever write.
--
--   2. Cards carry the details that make them distinguishable in a list and
--      that bound a cash withdrawal: network, colour, withdrawal ceiling, and
--      for credit cards a local and an international limit. Egyptian credit
--      cards routinely carry two different numbers, so they are two columns
--      rather than one with a multiplier.
--
--   3. Merchants belong to spending or to income, never silently to both. The
--      seeded Receipt-less row is the one exception ('both'), because it has to
--      stay one tap away on either side of the form.
--
-- Money stays in minor units here as everywhere else (hard rule 1). The limits
-- are *ceilings*, not balances, so they are plain positive integers.

-- ------------------------------------------------------------- accounts

-- NULL means "this account stands on its own": a bank, a wallet, a credit card,
-- cash. Non-NULL means the money actually lives in the parent.
ALTER TABLE accounts ADD COLUMN parent_account_id        INTEGER REFERENCES accounts(id);

ALTER TABLE accounts ADD COLUMN card_network             TEXT;
-- Free text on purpose: any CSS colour the household likes. It is validated in
-- Python before it is ever rendered, and rendered as an SVG fill attribute
-- rather than an inline style, because the CSP forbids inline styles.
ALTER TABLE accounts ADD COLUMN card_color               TEXT;
ALTER TABLE accounts ADD COLUMN withdrawal_limit_minor   INTEGER;
ALTER TABLE accounts ADD COLUMN credit_limit_local_minor INTEGER;
ALTER TABLE accounts ADD COLUMN credit_limit_intl_minor  INTEGER;

CREATE INDEX ix_accounts_parent ON accounts(parent_account_id);

-- One Instapay handle per bank or wallet; debit cards are unlimited. A partial
-- UNIQUE index is the whole rule — SQLite treats NULLs as distinct, so the
-- WHERE clause is what stops every unlinked account colliding with every other.
CREATE UNIQUE INDEX ux_accounts_one_instapay
  ON accounts(parent_account_id)
  WHERE type = 'instapay' AND parent_account_id IS NOT NULL;

-- ------------------------------------------------------- link legality

CREATE TRIGGER trg_accounts_link_insert BEFORE INSERT ON accounts
WHEN NEW.parent_account_id IS NOT NULL
BEGIN
  SELECT CASE
    WHEN NEW.type NOT IN ('instapay','debit_card')
      THEN RAISE(ABORT, 'accounts: only instapay and debit cards can be linked')
    WHEN (SELECT type FROM accounts WHERE id = NEW.parent_account_id)
           NOT IN ('bank','wallet')
      THEN RAISE(ABORT, 'accounts: only a bank or wallet can hold links')
    WHEN (SELECT parent_account_id FROM accounts WHERE id = NEW.parent_account_id)
           IS NOT NULL
      THEN RAISE(ABORT, 'accounts: links are one level deep')
  END;
END;

CREATE TRIGGER trg_accounts_link_update BEFORE UPDATE OF parent_account_id, type ON accounts
WHEN NEW.parent_account_id IS NOT NULL
BEGIN
  SELECT CASE
    WHEN NEW.parent_account_id = NEW.id
      THEN RAISE(ABORT, 'accounts: an account cannot be linked to itself')
    WHEN NEW.type NOT IN ('instapay','debit_card')
      THEN RAISE(ABORT, 'accounts: only instapay and debit cards can be linked')
    WHEN (SELECT type FROM accounts WHERE id = NEW.parent_account_id)
           NOT IN ('bank','wallet')
      THEN RAISE(ABORT, 'accounts: only a bank or wallet can hold links')
    WHEN (SELECT parent_account_id FROM accounts WHERE id = NEW.parent_account_id)
           IS NOT NULL
      THEN RAISE(ABORT, 'accounts: links are one level deep')
  END;
END;

-- A parent may not be re-typed out from under its children.
CREATE TRIGGER trg_accounts_parent_type_update BEFORE UPDATE OF type ON accounts
WHEN NEW.type NOT IN ('bank','wallet')
 AND EXISTS (SELECT 1 FROM accounts WHERE parent_account_id = NEW.id)
BEGIN
  SELECT RAISE(ABORT, 'accounts: this account still holds linked cards');
END;

-- ------------------------------------------------ opening balance sign

-- Hard rule 2 keeps transaction amounts positive; this is the balance side of
-- the same idea. Only a credit card is a debt instrument, so only a credit card
-- may start below zero. Everything else that would go negative is a data entry
-- mistake worth refusing at the door.
CREATE TRIGGER trg_accounts_opening_sign_insert BEFORE INSERT ON accounts
WHEN NEW.opening_balance_minor < 0 AND NEW.type <> 'credit_card'
BEGIN
  SELECT RAISE(ABORT, 'accounts: only a credit card may open in debt');
END;

CREATE TRIGGER trg_accounts_opening_sign_update
BEFORE UPDATE OF opening_balance_minor, type ON accounts
WHEN NEW.opening_balance_minor < 0 AND NEW.type <> 'credit_card'
BEGIN
  SELECT RAISE(ABORT, 'accounts: only a credit card may open in debt');
END;

-- Instapay is a rail, not a pot: the money it moves is the parent's money, so
-- giving it an opening balance of its own would double-count the household.
CREATE TRIGGER trg_accounts_instapay_zero_insert BEFORE INSERT ON accounts
WHEN NEW.type = 'instapay' AND NEW.opening_balance_minor <> 0
BEGIN
  SELECT RAISE(ABORT, 'accounts: instapay carries the linked account balance');
END;

CREATE TRIGGER trg_accounts_instapay_zero_update
BEFORE UPDATE OF opening_balance_minor, type ON accounts
WHEN NEW.type = 'instapay' AND NEW.opening_balance_minor <> 0
BEGIN
  SELECT RAISE(ABORT, 'accounts: instapay carries the linked account balance');
END;

-- --------------------------------------------------------- card fields

CREATE TRIGGER trg_accounts_card_fields_insert BEFORE INSERT ON accounts
WHEN NEW.type IN ('credit_card','debit_card')
BEGIN
  SELECT CASE
    WHEN NEW.card_network IS NULL OR TRIM(NEW.card_network) = ''
      THEN RAISE(ABORT, 'accounts: a card needs a network')
    WHEN NEW.card_color IS NULL OR TRIM(NEW.card_color) = ''
      THEN RAISE(ABORT, 'accounts: a card needs a colour')
    WHEN NEW.withdrawal_limit_minor IS NULL OR NEW.withdrawal_limit_minor <= 0
      THEN RAISE(ABORT, 'accounts: a card needs a cash withdrawal limit')
    WHEN NEW.type = 'credit_card'
     AND (NEW.credit_limit_local_minor IS NULL OR NEW.credit_limit_local_minor <= 0)
      THEN RAISE(ABORT, 'accounts: a credit card needs a local limit')
    WHEN NEW.type = 'credit_card'
     AND (NEW.credit_limit_intl_minor IS NULL OR NEW.credit_limit_intl_minor <= 0)
      THEN RAISE(ABORT, 'accounts: a credit card needs an international limit')
  END;
END;

CREATE TRIGGER trg_accounts_card_fields_update
BEFORE UPDATE OF type, card_network, card_color, withdrawal_limit_minor,
                 credit_limit_local_minor, credit_limit_intl_minor ON accounts
WHEN NEW.type IN ('credit_card','debit_card')
BEGIN
  SELECT CASE
    WHEN NEW.card_network IS NULL OR TRIM(NEW.card_network) = ''
      THEN RAISE(ABORT, 'accounts: a card needs a network')
    WHEN NEW.card_color IS NULL OR TRIM(NEW.card_color) = ''
      THEN RAISE(ABORT, 'accounts: a card needs a colour')
    WHEN NEW.withdrawal_limit_minor IS NULL OR NEW.withdrawal_limit_minor <= 0
      THEN RAISE(ABORT, 'accounts: a card needs a cash withdrawal limit')
    WHEN NEW.type = 'credit_card'
     AND (NEW.credit_limit_local_minor IS NULL OR NEW.credit_limit_local_minor <= 0)
      THEN RAISE(ABORT, 'accounts: a credit card needs a local limit')
    WHEN NEW.type = 'credit_card'
     AND (NEW.credit_limit_intl_minor IS NULL OR NEW.credit_limit_intl_minor <= 0)
      THEN RAISE(ABORT, 'accounts: a credit card needs an international limit')
  END;
END;

-- A debit card has no credit line. Storing one would be a number nobody can
-- explain the meaning of six months from now.
CREATE TRIGGER trg_accounts_debit_no_credit_insert BEFORE INSERT ON accounts
WHEN NEW.type <> 'credit_card'
 AND (NEW.credit_limit_local_minor IS NOT NULL OR NEW.credit_limit_intl_minor IS NOT NULL)
BEGIN
  SELECT RAISE(ABORT, 'accounts: only a credit card carries credit limits');
END;

CREATE TRIGGER trg_accounts_debit_no_credit_update
BEFORE UPDATE OF type, credit_limit_local_minor, credit_limit_intl_minor ON accounts
WHEN NEW.type <> 'credit_card'
 AND (NEW.credit_limit_local_minor IS NOT NULL OR NEW.credit_limit_intl_minor IS NOT NULL)
BEGIN
  SELECT RAISE(ABORT, 'accounts: only a credit card carries credit limits');
END;

-- ------------------------------------------------------------ merchants

-- 'both' exists for exactly one row: the seeded Receipt-less merchant, which
-- has to stay reachable in one tap whether the entry is a spend or income.
-- Everything a person adds is one or the other.
ALTER TABLE merchants ADD COLUMN kind TEXT NOT NULL DEFAULT 'spend'
  CHECK (kind IN ('spend','income','both'));

UPDATE merchants SET kind = 'both' WHERE is_system = 1;

CREATE INDEX ix_merchants_kind ON merchants(kind, is_active, name);

-- ----------------------------------------------------------- categories

-- Cash is limited to spending, income and reimbursements; the first two are
-- directions, and this is where the third lands so it can be reported on.
INSERT INTO categories (id, name, parent_id, icon, sort_order)
VALUES (154, 'Reimbursement', 12, NULL, 40);
