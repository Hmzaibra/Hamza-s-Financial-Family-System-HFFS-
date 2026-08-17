-- 007 an account can be asked to compare its months
--
-- Opt-in rather than always on, and the reason is not performance theatre —
-- though it is two extra month queries on a screen that is otherwise cheap.
--
-- It is that the comparison is only *interesting* on some accounts. A current
-- account that most spending goes through has a month worth watching. A cash
-- pocket topped up at random, or a card whose figures are its parent's anyway,
-- produces a number that moves for reasons that are not about spending — and a
-- comparison nobody can act on is a comparison that teaches you to ignore the
-- screen it sits on.
--
-- So the household says which accounts it wants watched, per account, and the
-- summary grows a card only where the answer is yes.

ALTER TABLE accounts ADD COLUMN reporting_enabled INTEGER NOT NULL DEFAULT 0
  CHECK (reporting_enabled IN (0,1));

-- The comparison reads the *settlement* account's figures, the same as every
-- other number on that screen: a card is a way of reaching the account behind
-- it, not a second pot. Turning the flag on for a card would therefore report
-- its parent's months under the card's name, which is true but says the parent
-- twice. Kept out of the schema rather than enforced there — the account form
-- simply does not offer the tick on a linked account, and a household that
-- edits the database by hand gets what it asked for.
