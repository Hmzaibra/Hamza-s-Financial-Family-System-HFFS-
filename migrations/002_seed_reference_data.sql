-- 002 seed reference data
--
-- Non-secret only, so this file stays committable. The admin user is created by
-- `flask create-admin`, which prompts for a password: a migration containing a
-- password hash is a committed credential.
--
-- Timestamps use strftime rather than datetime() so they match the ISO-8601
-- 'YYYY-MM-DDTHH:MM:SSZ' UTC form the application writes everywhere else.

-- ------------------------------------------------------------- settings

INSERT INTO settings (key, value, updated_at) VALUES
  ('base_currency',    'EGP',           strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  ('default_timezone', 'Europe/Berlin', strftime('%Y-%m-%dT%H:%M:%SZ','now')),
  ('household_name',   'Household',     strftime('%Y-%m-%dT%H:%M:%SZ','now'));

-- ----------------------------------------------------------- categories
-- Explicit ids so children can reference parents deterministically and so a
-- re-run against a fresh database always produces the same tree.

INSERT INTO categories (id, name, parent_id, icon, sort_order) VALUES
  ( 1, 'Groceries',      NULL, '🛒', 10),
  ( 2, 'Eating Out',     NULL, '🍽️', 20),
  ( 3, 'Transport',      NULL, '🚗', 30),
  ( 4, 'Home',           NULL, '🏠', 40),
  ( 5, 'Health',         NULL, '💊', 50),
  ( 6, 'Shopping',       NULL, '🛍️', 60),
  ( 7, 'Family & Gifts', NULL, '🎁', 70),
  ( 8, 'Education',      NULL, '📚', 80),
  ( 9, 'Entertainment',  NULL, '🎬', 90),
  (10, 'Subscriptions',  NULL, '🔁', 100),
  (11, 'Fees & Charges', NULL, '🏦', 110),
  (12, 'Income',         NULL, '💰', 120),
  (13, 'Other',          NULL, '•',  900);

INSERT INTO categories (id, name, parent_id, icon, sort_order) VALUES
  (101, 'Coffee',         2, NULL, 10),
  (102, 'Restaurant',     2, NULL, 20),
  (103, 'Delivery',       2, NULL, 30),

  (111, 'Fuel',           3, NULL, 10),
  (112, 'Ride-hailing',   3, NULL, 20),
  (113, 'Public transit', 3, NULL, 30),
  (114, 'Parking & Tolls', 3, NULL, 40),

  (121, 'Rent',           4, NULL, 10),
  (122, 'Utilities',      4, NULL, 20),
  (123, 'Internet & Mobile', 4, NULL, 30),
  (124, 'Maintenance',    4, NULL, 40),

  (131, 'Pharmacy',       5, NULL, 10),
  (132, 'Doctor',         5, NULL, 20),

  (141, 'Clothes',        6, NULL, 10),
  (142, 'Electronics',    6, NULL, 20),
  (143, 'Household',      6, NULL, 30),

  (151, 'Salary',        12, NULL, 10),
  (152, 'Transfer in',   12, NULL, 20),
  (153, 'Refund',        12, NULL, 30);

-- ------------------------------------------------------------ merchants
-- Receipt-less covers kiosks, street vendors and anything without a paper
-- trail. It is pinned as the first chip on the entry form, not buried in a
-- dropdown, and cannot be deleted.

INSERT INTO merchants (name, default_category_id, default_is_online, default_account_id,
                       is_system, notes, is_active, created_at)
VALUES ('Receipt-less', NULL, 0, NULL, 1,
        'Kiosks, street vendors, anything without a paper trail.',
        1, strftime('%Y-%m-%dT%H:%M:%SZ','now'));
