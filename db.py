import sqlite3
import os
import re
import secrets
import datetime

from werkzeug.security import generate_password_hash, check_password_hash

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "app.db")

DEFAULT_COORDINATORS = ["Anil Kumar", "Binay Kumar"]

ROLES = ["admin", "sales", "purchase", "accountant", "field_staff"]
ROLE_LABELS = {
    "admin": "Owner / Admin",
    "sales": "Sales staff",
    "purchase": "Purchase staff",
    "accountant": "Accountant / Viewer",
    "field_staff": "Service Manager",
}


def get_conn():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    # busy_timeout: wait up to 10s for a lock instead of failing immediately
    # with "database is locked" - matters once there's real concurrent
    # traffic from multiple gunicorn/waitress workers.
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def init_db():
    conn = get_conn()
    # WAL mode: readers don't block writers and vice versa (SQLite's default
    # rollback-journal mode serializes all writers). This is a persistent,
    # one-time setting stored in the database file itself, not something
    # that needs to be re-applied per connection.
    conn.execute("PRAGMA journal_mode = WAL")
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS coordinators (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS parties (
            name TEXT PRIMARY KEY,
            contact_number TEXT DEFAULT '',
            group_by TEXT DEFAULT '',
            location TEXT DEFAULT '',
            coordinator TEXT DEFAULT ''
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS invoice_status (
            invoice_id TEXT PRIMARY KEY,
            status TEXT NOT NULL CHECK(status IN ('paid','unpaid')),
            payment_date TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            full_name TEXT DEFAULT '',
            role TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1,
            joining_date TEXT,
            monthly_salary REAL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Migrate DBs created before the role CHECK constraint was removed (it
    # hardcoded the role list at table-creation time, so every new role
    # added since - like field_staff - would fail with a misleading
    # "already exists" error, since SQLite reports CHECK violations as the
    # same IntegrityError type as UNIQUE violations). SQLite can't drop a
    # CHECK constraint via ALTER TABLE, so rebuild the table if the old
    # constraint is detected.
    old_schema = cur.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='users'"
    ).fetchone()
    if old_schema and old_schema["sql"] and "CHECK(role IN" in old_schema["sql"]:
        cur.execute("ALTER TABLE users RENAME TO users_old")
        cur.execute("""
            CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                full_name TEXT DEFAULT '',
                role TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            INSERT INTO users (id, username, password_hash, full_name, role, active, created_at)
            SELECT id, username, password_hash, full_name, role, active, created_at FROM users_old
        """)
        cur.execute("DROP TABLE users_old")
        conn.commit()

    for stmt in (
        "ALTER TABLE users ADD COLUMN joining_date TEXT",
        "ALTER TABLE users ADD COLUMN monthly_salary REAL",
    ):
        try:
            cur.execute(stmt)
            conn.commit()
        except sqlite3.OperationalError:
            pass

    cur.execute("""
        CREATE TABLE IF NOT EXISTS wallet_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id TEXT NOT NULL,
            customer_name TEXT DEFAULT '',
            entry_type TEXT NOT NULL CHECK(entry_type IN ('credit','debit')),
            amount REAL NOT NULL,
            note TEXT DEFAULT '',
            created_by TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS vendor_wallet_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vendor_id TEXT NOT NULL,
            vendor_name TEXT DEFAULT '',
            entry_type TEXT NOT NULL CHECK(entry_type IN ('credit','debit')),
            amount REAL NOT NULL,
            note TEXT DEFAULT '',
            created_by TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ------------------------------------------------------------------ #
    # Core transactional tables (replaces sales_log.xlsx / billdesk.xlsx
    # as the live store - see migration.py). Excel remains an import/export
    # format, not the source of truth, once migrated.
    # ------------------------------------------------------------------ #
    cur.execute("""
        CREATE TABLE IF NOT EXISTS customers (
            customer_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            contact_number TEXT DEFAULT '',
            address_details TEXT DEFAULT '',
            gstn TEXT DEFAULT '',
            category TEXT DEFAULT ''
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS vendors (
            vendor_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            contact_number TEXT DEFAULT '',
            address_details TEXT DEFAULT '',
            gstn TEXT DEFAULT '',
            category TEXT DEFAULT ''
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sheet TEXT NOT NULL,
            item_code TEXT DEFAULT '',
            item_name TEXT NOT NULL,
            item_description TEXT DEFAULT '',
            hsn TEXT DEFAULT '',
            mrp TEXT DEFAULT '',
            quantity REAL DEFAULT 0,
            gst_pct REAL DEFAULT 0,
            cost_price REAL DEFAULT 0,
            sale_price REAL DEFAULT 0,
            approved INTEGER NOT NULL DEFAULT 1,
            created_by TEXT DEFAULT '',
            UNIQUE(sheet, item_name, item_description)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_name TEXT UNIQUE NOT NULL,
            invoice_no TEXT NOT NULL,
            original_invoice_no TEXT NOT NULL,
            split_leg TEXT DEFAULT '',
            bill_type TEXT DEFAULT 'Tax Invoice',
            date TEXT NOT NULL,
            customer_id TEXT DEFAULT '',
            customer_name TEXT DEFAULT '',
            total REAL DEFAULT 0,
            margin REAL DEFAULT 0,
            taxable_total REAL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'unpaid' CHECK(status IN ('paid','unpaid')),
            payment_date TEXT,
            is_candidate INTEGER NOT NULL DEFAULT 1,
            deleted INTEGER NOT NULL DEFAULT 0,
            created_by TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS bill_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bill_id INTEGER NOT NULL REFERENCES bills(id),
            product_id INTEGER REFERENCES products(id),
            description TEXT,
            hsn TEXT,
            mrp TEXT,
            qty REAL,
            gst_pct REAL,
            rate REAL,
            taxable_rate REAL,
            amount REAL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS purchase_bills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_name TEXT UNIQUE NOT NULL,
            purchase_no TEXT NOT NULL,
            vendor_id TEXT DEFAULT '',
            vendor_name TEXT DEFAULT '',
            date TEXT NOT NULL,
            total REAL DEFAULT 0,
            taxable_total REAL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'unpaid',
            amount_paid REAL NOT NULL DEFAULT 0,
            payment_date TEXT,
            money_request_id INTEGER,
            deleted INTEGER NOT NULL DEFAULT 0,
            created_by TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Migrate DBs created before partial-payment support existed on
    # purchase_bills - same issue as the users table earlier: a
    # CHECK(status IN ('paid','unpaid')) baked in at creation time would
    # reject the new 'partial' status with the same misleading error class
    # as a uniqueness violation. Rebuild if the old constraint is present.
    old_pb_schema = cur.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='purchase_bills'"
    ).fetchone()
    if old_pb_schema and old_pb_schema["sql"] and "CHECK(status IN" in old_pb_schema["sql"]:
        cur.execute("ALTER TABLE purchase_bills RENAME TO purchase_bills_old")
        cur.execute("""
            CREATE TABLE purchase_bills (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_name TEXT UNIQUE NOT NULL,
                purchase_no TEXT NOT NULL,
                vendor_id TEXT DEFAULT '',
                vendor_name TEXT DEFAULT '',
                date TEXT NOT NULL,
                total REAL DEFAULT 0,
                taxable_total REAL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'unpaid',
                amount_paid REAL NOT NULL DEFAULT 0,
                payment_date TEXT,
                money_request_id INTEGER,
                deleted INTEGER NOT NULL DEFAULT 0,
                created_by TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cur.execute("""
            INSERT INTO purchase_bills (id, file_name, purchase_no, vendor_id, vendor_name, date,
                                         total, taxable_total, status, amount_paid, payment_date,
                                         deleted, created_by, created_at, updated_at)
            SELECT id, file_name, purchase_no, vendor_id, vendor_name, date,
                   total, taxable_total, status,
                   CASE WHEN status = 'paid' THEN total ELSE 0 END,
                   payment_date, deleted, created_by, created_at, updated_at
            FROM purchase_bills_old
        """)
        cur.execute("DROP TABLE purchase_bills_old")
        conn.commit()

    for stmt in (
        "ALTER TABLE purchase_bills ADD COLUMN amount_paid REAL NOT NULL DEFAULT 0",
        "ALTER TABLE purchase_bills ADD COLUMN money_request_id INTEGER",
    ):
        try:
            cur.execute(stmt)
            conn.commit()
        except sqlite3.OperationalError:
            pass

    cur.execute("""
        CREATE TABLE IF NOT EXISTS purchase_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            purchase_id INTEGER NOT NULL REFERENCES purchase_bills(id),
            amount REAL NOT NULL,
            paid_via TEXT NOT NULL DEFAULT 'cash',
            payment_date TEXT NOT NULL,
            note TEXT DEFAULT '',
            created_by TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS purchase_bill_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            purchase_bill_id INTEGER NOT NULL REFERENCES purchase_bills(id),
            product_id INTEGER REFERENCES products(id),
            description TEXT,
            hsn TEXT,
            qty REAL,
            gst_pct REAL,
            cost_rate REAL,
            amount REAL
        )
    """)
    conn.commit()

    # Migrate DBs created before payment_date existed; no-op (caught) if it's
    # already there.
    try:
        cur.execute("ALTER TABLE invoice_status ADD COLUMN payment_date TEXT")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    # Migrate DBs created before category existed on customers/vendors.
    for table in ("customers", "vendors"):
        try:
            cur.execute(f"ALTER TABLE {table} ADD COLUMN category TEXT DEFAULT ''")
            conn.commit()
        except sqlite3.OperationalError:
            pass

    # Migrate DBs created before approved/created_by existed on products.
    # New column defaults to 1 (approved) so pre-existing rows - your real
    # catalog - aren't retroactively hidden; only products a field_staff
    # user creates from now on default to 0 (pending).
    try:
        cur.execute("ALTER TABLE products ADD COLUMN approved INTEGER NOT NULL DEFAULT 1")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE products ADD COLUMN created_by TEXT DEFAULT ''")
        conn.commit()
    except sqlite3.OperationalError:
        pass

    cur.execute("""
        CREATE TABLE IF NOT EXISTS delivery_receipts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receipt_no TEXT UNIQUE NOT NULL,
            customer_id TEXT DEFAULT '',
            customer_name TEXT DEFAULT '',
            date TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            approved_by TEXT,
            approved_at TEXT,
            deleted INTEGER NOT NULL DEFAULT 0,
            created_by TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS delivery_receipt_lines (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            receipt_id INTEGER NOT NULL REFERENCES delivery_receipts(id),
            product_id INTEGER REFERENCES products(id),
            description TEXT,
            qty REAL,
            update_stock INTEGER NOT NULL DEFAULT 1,
            invoiced_bill_id INTEGER
        )
    """)

    # Migrate DBs created before the approval workflow / US-DU stock toggle
    # existed on deliveries. Existing delivery rows default to 'approved'
    # (they were created back when deliveries never touched stock at all,
    # so there's nothing to reconcile) and their lines default to
    # update_stock=1, matching the pre-this-feature behavior as closely as
    # possible for anything already on record.
    for stmt in (
        "ALTER TABLE delivery_receipts ADD COLUMN status TEXT NOT NULL DEFAULT 'approved'",
        "ALTER TABLE delivery_receipts ADD COLUMN approved_by TEXT",
        "ALTER TABLE delivery_receipts ADD COLUMN approved_at TEXT",
    ):
        try:
            cur.execute(stmt)
            conn.commit()
        except sqlite3.OperationalError:
            pass
    try:
        cur.execute("ALTER TABLE delivery_receipt_lines ADD COLUMN update_stock INTEGER NOT NULL DEFAULT 1")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    try:
        cur.execute("ALTER TABLE delivery_receipt_lines ADD COLUMN invoiced_bill_id INTEGER")
        conn.commit()
    except sqlite3.OperationalError:
        pass
    cur.execute("""
        CREATE TABLE IF NOT EXISTS money_request_reasons (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT UNIQUE NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS customer_categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT UNIQUE NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS vendor_categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            label TEXT UNIQUE NOT NULL
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS field_staff_customers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            customer_id TEXT NOT NULL,
            UNIQUE(username, customer_id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS field_staff_vendors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            vendor_id TEXT NOT NULL,
            UNIQUE(username, vendor_id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS vendor_products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vendor_id TEXT NOT NULL,
            product_id INTEGER NOT NULL,
            UNIQUE(vendor_id, product_id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS customer_product_prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id TEXT NOT NULL,
            product_id INTEGER NOT NULL,
            last_price REAL NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(customer_id, product_id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS money_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            requested_by TEXT NOT NULL,
            amount REAL NOT NULL,
            reason TEXT NOT NULL,
            note TEXT DEFAULT '',
            is_gst_bill INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
            linked_purchase_no TEXT,
            resolved_by TEXT,
            resolved_at TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS cash_ledger (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            holder_username TEXT NOT NULL,
            entry_type TEXT NOT NULL CHECK(entry_type IN ('credit','debit')),
            amount REAL NOT NULL,
            note TEXT DEFAULT '',
            linked_money_request_id INTEGER,
            linked_purchase_no TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS salary_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            period TEXT NOT NULL,
            amount REAL,
            status TEXT NOT NULL DEFAULT 'approved',
            note TEXT DEFAULT '',
            approved_by TEXT,
            approved_at TEXT DEFAULT CURRENT_TIMESTAMP,
            paid_by TEXT,
            paid_at TEXT,
            UNIQUE(username, period)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS custom_fields (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            entity_type TEXT NOT NULL,
            field_key TEXT NOT NULL,
            field_label TEXT NOT NULL,
            field_type TEXT NOT NULL DEFAULT 'text',
            display_order INTEGER DEFAULT 0,
            UNIQUE(entity_type, field_key)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS custom_field_values (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            field_id INTEGER NOT NULL REFERENCES custom_fields(id),
            entity_id TEXT NOT NULL,
            value TEXT DEFAULT '',
            UNIQUE(field_id, entity_id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS business_profile (
            key TEXT PRIMARY KEY,
            value TEXT DEFAULT ''
        )
    """)
    conn.commit()
    # Seed a starter set of reasons if none exist yet - admin can add/remove
    # more later (see money_request_reasons CRUD below).
    if conn.execute("SELECT COUNT(*) AS c FROM money_request_reasons").fetchone()["c"] == 0:
        for label in ("Purchase", "Fuel", "Delivery expense", "Repair", "Miscellaneous"):
            conn.execute("INSERT OR IGNORE INTO money_request_reasons (label) VALUES (?)", (label,))
        conn.commit()

    # Seed the canonical customer/vendor category options admin can filter
    # by - separate from the free-text category value stored on each
    # customer/vendor row (which came from the original Excel import and
    # may include values outside this curated list, like a customer whose
    # bank wasn't in the standard set - matching is done case-insensitively
    # so existing lowercase import data like 'sbi' still matches 'SBI' here).
    if conn.execute("SELECT COUNT(*) AS c FROM customer_categories").fetchone()["c"] == 0:
        for label in ("SBI", "BOB", "PNB", "BOI", "UBI", "IB", "Canara", "Gramin", "Agency", "Office", "Gem"):
            conn.execute("INSERT OR IGNORE INTO customer_categories (label) VALUES (?)", (label,))
        conn.commit()
    if conn.execute("SELECT COUNT(*) AS c FROM vendor_categories").fetchone()["c"] == 0:
        for label in ("Domes", "Infinity", "Kangaro", "CobraFiles", "Cetntury"):
            conn.execute("INSERT OR IGNORE INTO vendor_categories (label) VALUES (?)", (label,))
        conn.commit()

    # One-time correction for databases already seeded with the earlier
    # mistake: "Century" (corrected spelling) was seeded instead of
    # "Cetntury" (the actual typo baked into the original Excel import),
    # so that filter option silently matched zero real vendors. Only
    # touches it if 'Century' exists and 'Cetntury' doesn't yet, so it
    # won't clobber anything if admin has since added both deliberately.
    has_wrong = conn.execute("SELECT 1 FROM vendor_categories WHERE label = 'Century'").fetchone()
    has_right = conn.execute("SELECT 1 FROM vendor_categories WHERE label = 'Cetntury'").fetchone()
    if has_wrong and not has_right:
        conn.execute("UPDATE vendor_categories SET label = 'Cetntury' WHERE label = 'Century'")
        conn.commit()

    cur.execute("SELECT COUNT(*) AS c FROM coordinators")
    if cur.fetchone()["c"] == 0:
        for name in DEFAULT_COORDINATORS:
            cur.execute("INSERT OR IGNORE INTO coordinators (name) VALUES (?)", (name,))
        conn.commit()

    conn.close()


def list_coordinators():
    conn = get_conn()
    rows = conn.execute("SELECT name FROM coordinators ORDER BY name").fetchall()
    conn.close()
    return [r["name"] for r in rows]


def add_coordinator(name):
    name = (name or "").strip()
    if not name:
        return
    conn = get_conn()
    conn.execute("INSERT OR IGNORE INTO coordinators (name) VALUES (?)", (name,))
    conn.commit()
    conn.close()


def list_groups():
    conn = get_conn()
    rows = conn.execute("SELECT name FROM groups ORDER BY name").fetchall()
    conn.close()
    return [r["name"] for r in rows]


def add_group(name):
    name = (name or "").strip()
    if not name:
        return
    conn = get_conn()
    conn.execute("INSERT OR IGNORE INTO groups (name) VALUES (?)", (name,))
    conn.commit()
    conn.close()


def ensure_parties_exist(names):
    """Make sure every customer name from the sheet has a row in parties (blank fields ok)."""
    conn = get_conn()
    for name in names:
        conn.execute("INSERT OR IGNORE INTO parties (name) VALUES (?)", (name,))
    conn.commit()
    conn.close()


def get_all_parties():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM parties ORDER BY name").fetchall()
    conn.close()
    return {r["name"]: dict(r) for r in rows}


def upsert_party(name, contact_number="", group_by="", location="", coordinator=""):
    name = (name or "").strip()
    if not name:
        return
    conn = get_conn()
    conn.execute("""
        INSERT INTO parties (name, contact_number, group_by, location, coordinator)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET
            contact_number=excluded.contact_number,
            group_by=excluded.group_by,
            location=excluded.location,
            coordinator=excluded.coordinator
    """, (name, contact_number.strip(), group_by.strip(), location.strip(), coordinator.strip()))
    conn.commit()
    conn.close()


def delete_party(name):
    conn = get_conn()
    conn.execute("DELETE FROM parties WHERE name=?", (name,))
    conn.commit()
    conn.close()


def get_status_overrides():
    """Returns {invoice_id: {"status": "paid"|"unpaid", "payment_date": "YYYY-MM-DD"|None}}"""
    conn = get_conn()
    rows = conn.execute("SELECT invoice_id, status, payment_date FROM invoice_status").fetchall()
    conn.close()
    return {r["invoice_id"]: {"status": r["status"], "payment_date": r["payment_date"]} for r in rows}


def set_status(invoice_id, status, payment_date=None):
    if status not in ("paid", "unpaid"):
        return
    if status == "unpaid":
        payment_date = None
    conn = get_conn()
    conn.execute("""
        INSERT INTO invoice_status (invoice_id, status, payment_date) VALUES (?, ?, ?)
        ON CONFLICT(invoice_id) DO UPDATE SET status=excluded.status, payment_date=excluded.payment_date
    """, (invoice_id, status, payment_date))
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------- #
# Users / auth
# --------------------------------------------------------------------------- #
def any_users_exist():
    conn = get_conn()
    row = conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()
    conn.close()
    return row["c"] > 0


def create_user(username, password, full_name, role):
    username = (username or "").strip().lower()
    if not username or not password or role not in ROLES:
        raise ValueError("invalid username, password, or role")
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO users (username, password_hash, full_name, role) VALUES (?, ?, ?, ?)",
            (username, generate_password_hash(password), (full_name or "").strip(), role),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        raise ValueError(f"username '{username}' already exists")
    finally:
        conn.close()


def verify_login(username, password):
    """Returns the user row (dict) on success, or None."""
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM users WHERE username = ? AND active = 1", ((username or "").strip().lower(),)
    ).fetchone()
    conn.close()
    if row and check_password_hash(row["password_hash"], password):
        return dict(row)
    return None


def get_user(user_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_users():
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, username, full_name, role, active, joining_date, monthly_salary, created_at "
        "FROM users ORDER BY username"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_user_employment_info(user_id, joining_date, monthly_salary):
    conn = get_conn()
    conn.execute(
        "UPDATE users SET joining_date = ?, monthly_salary = ? WHERE id = ?",
        (joining_date or None, monthly_salary, user_id),
    )
    conn.commit()
    conn.close()


def get_user_by_username(username):
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()
    return dict(row) if row else None


def set_user_active(user_id, active):
    conn = get_conn()
    conn.execute("UPDATE users SET active = ? WHERE id = ?", (1 if active else 0, user_id))
    conn.commit()
    conn.close()


def set_user_role(user_id, role):
    if role not in ROLES:
        raise ValueError("invalid role")
    conn = get_conn()
    conn.execute("UPDATE users SET role = ? WHERE id = ?", (role, user_id))
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------- #
# Wallet (customer credit/advance balance)
# --------------------------------------------------------------------------- #
def add_wallet_entry(customer_id, customer_name, entry_type, amount, note, created_by):
    if entry_type not in ("credit", "debit"):
        raise ValueError("entry_type must be 'credit' or 'debit'")
    amount = float(amount)
    if amount <= 0:
        raise ValueError("amount must be positive")
    conn = get_conn()
    conn.execute(
        "INSERT INTO wallet_ledger (customer_id, customer_name, entry_type, amount, note, created_by) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (customer_id, customer_name, entry_type, amount, note or "", created_by or ""),
    )
    conn.commit()
    conn.close()


def get_wallet_balance(customer_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT "
        "  COALESCE(SUM(CASE WHEN entry_type='credit' THEN amount ELSE 0 END), 0) - "
        "  COALESCE(SUM(CASE WHEN entry_type='debit' THEN amount ELSE 0 END), 0) AS balance "
        "FROM wallet_ledger WHERE customer_id = ?",
        (customer_id,),
    ).fetchone()
    conn.close()
    return round(row["balance"], 2)


def get_wallet_history(customer_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM wallet_ledger WHERE customer_id = ? ORDER BY created_at DESC", (customer_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_all_wallet_balances():
    conn = get_conn()
    rows = conn.execute(
        "SELECT customer_id, MAX(customer_name) AS customer_name, "
        "  SUM(CASE WHEN entry_type='credit' THEN amount ELSE -amount END) AS balance "
        "FROM wallet_ledger GROUP BY customer_id HAVING balance != 0 ORDER BY balance DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Vendor wallet (mirrors customer wallet_ledger exactly, separate table so
# neither side's queries/balances can ever cross-contaminate)
# --------------------------------------------------------------------------- #
def add_vendor_wallet_entry(vendor_id, vendor_name, entry_type, amount, note, created_by):
    if entry_type not in ("credit", "debit"):
        raise ValueError("entry_type must be 'credit' or 'debit'")
    amount = float(amount)
    if amount <= 0:
        raise ValueError("amount must be positive")
    conn = get_conn()
    conn.execute(
        "INSERT INTO vendor_wallet_ledger (vendor_id, vendor_name, entry_type, amount, note, created_by) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (vendor_id, vendor_name, entry_type, amount, note or "", created_by or ""),
    )
    conn.commit()
    conn.close()


def get_vendor_wallet_balance(vendor_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT "
        "  COALESCE(SUM(CASE WHEN entry_type='credit' THEN amount ELSE 0 END), 0) - "
        "  COALESCE(SUM(CASE WHEN entry_type='debit' THEN amount ELSE 0 END), 0) AS balance "
        "FROM vendor_wallet_ledger WHERE vendor_id = ?",
        (vendor_id,),
    ).fetchone()
    conn.close()
    return round(row["balance"], 2)


def get_vendor_wallet_history(vendor_id, entry_type=None, date_from=None, date_to=None):
    conn = get_conn()
    q = "SELECT * FROM vendor_wallet_ledger WHERE vendor_id = ?"
    params = [vendor_id]
    if entry_type in ("credit", "debit"):
        q += " AND entry_type = ?"
        params.append(entry_type)
    if date_from:
        q += " AND date(created_at) >= date(?)"
        params.append(date_from)
    if date_to:
        q += " AND date(created_at) <= date(?)"
        params.append(date_to)
    q += " ORDER BY created_at DESC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_all_vendor_wallet_balances():
    conn = get_conn()
    rows = conn.execute(
        "SELECT vendor_id, MAX(vendor_name) AS vendor_name, "
        "  SUM(CASE WHEN entry_type='credit' THEN amount ELSE -amount END) AS balance "
        "FROM vendor_wallet_ledger GROUP BY vendor_id HAVING balance != 0 ORDER BY balance DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Customers / vendors
# --------------------------------------------------------------------------- #
def upsert_customer(customer_id, name, contact_number="", address_details="", gstn="", category=""):
    conn = get_conn()
    conn.execute("""
        INSERT INTO customers (customer_id, name, contact_number, address_details, gstn, category)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(customer_id) DO UPDATE SET
            name=excluded.name, contact_number=excluded.contact_number,
            address_details=excluded.address_details, gstn=excluded.gstn,
            category=excluded.category
    """, (customer_id, name, contact_number or "", address_details or "", gstn or "", category or ""))
    conn.commit()
    conn.close()


def list_customers():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM customers ORDER BY name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_customer(customer_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM customers WHERE customer_id = ?", (customer_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_customer_categories():
    conn = get_conn()
    rows = conn.execute(
        "SELECT DISTINCT category FROM customers WHERE category != '' ORDER BY category"
    ).fetchall()
    conn.close()
    return [r["category"] for r in rows]


def upsert_vendor(vendor_id, name, contact_number="", address_details="", gstn="", category=""):
    conn = get_conn()
    conn.execute("""
        INSERT INTO vendors (vendor_id, name, contact_number, address_details, gstn, category)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(vendor_id) DO UPDATE SET
            name=excluded.name, contact_number=excluded.contact_number,
            address_details=excluded.address_details, gstn=excluded.gstn,
            category=excluded.category
    """, (vendor_id, name, contact_number or "", address_details or "", gstn or "", category or ""))
    conn.commit()
    conn.close()


def list_vendors():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM vendors ORDER BY name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_vendor(vendor_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM vendors WHERE vendor_id = ?", (vendor_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_vendor_categories():
    conn = get_conn()
    rows = conn.execute(
        "SELECT DISTINCT category FROM vendors WHERE category != '' ORDER BY category"
    ).fetchall()
    conn.close()
    return [r["category"] for r in rows]


# --------------------------------------------------------------------------- #
# Products (replaces billdesk.xlsx product sheets as the live store)
# --------------------------------------------------------------------------- #
def upsert_product(sheet, item_code, item_name, item_description, hsn, mrp,
                    quantity, gst_pct, cost_price, sale_price):
    conn = get_conn()
    conn.execute("""
        INSERT INTO products (sheet, item_code, item_name, item_description, hsn, mrp,
                               quantity, gst_pct, cost_price, sale_price)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(sheet, item_name, item_description) DO UPDATE SET
            item_code=excluded.item_code, hsn=excluded.hsn, mrp=excluded.mrp,
            quantity=excluded.quantity, gst_pct=excluded.gst_pct,
            cost_price=excluded.cost_price, sale_price=excluded.sale_price
    """, (sheet, item_code or "", item_name, item_description or "", hsn or "", mrp,
          quantity or 0, gst_pct or 0, cost_price or 0, sale_price or 0))
    conn.commit()
    conn.close()


def get_product(product_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def decrement_product_stock(product_id, qty):
    """Returns the new quantity (can go negative - same tolerance as the legacy sheet)."""
    conn = get_conn()
    conn.execute("UPDATE products SET quantity = quantity - ? WHERE id = ?", (qty, product_id))
    row = conn.execute("SELECT quantity FROM products WHERE id = ?", (product_id,)).fetchone()
    conn.commit()
    conn.close()
    return row["quantity"] if row else None


def product_count():
    conn = get_conn()
    row = conn.execute("SELECT COUNT(*) AS c FROM products").fetchone()
    conn.close()
    return row["c"]


def adjust_product_stock(product_id, delta):
    """delta > 0 adds back to stock (e.g. on delete/edit), < 0 removes it."""
    if not product_id:
        return None
    conn = get_conn()
    conn.execute("UPDATE products SET quantity = quantity + ? WHERE id = ?", (delta, product_id))
    row = conn.execute("SELECT quantity FROM products WHERE id = ?", (product_id,)).fetchone()
    conn.commit()
    conn.close()
    return row["quantity"] if row else None


# --------------------------------------------------------------------------- #
# Bills / bill_lines (replaces sales_log.xlsx as the live store)
# --------------------------------------------------------------------------- #
def bill_count():
    conn = get_conn()
    row = conn.execute("SELECT COUNT(*) AS c FROM bills").fetchone()
    conn.close()
    return row["c"]


def next_invoice_number_db(series_prefix_hint=None):
    """DB-native version of billing_engine.next_invoice_number - same
    front/back-increment logic, sourced from the bills table's most recent
    original_invoice_no instead of scanning an xlsx file."""
    conn = get_conn()
    row = conn.execute(
        "SELECT original_invoice_no FROM bills WHERE deleted = 0 "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    if not row or not row["original_invoice_no"] or len(row["original_invoice_no"]) <= 4:
        return (series_prefix_hint or "TS2026AA") + "0001"
    last = row["original_invoice_no"]
    front, back = last[:-4], last[-4:]
    try:
        back_num = int(back) + 1
    except ValueError:
        back_num = 1
    return front + str(back_num).zfill(4)


def insert_bill(file_name, invoice_no, original_invoice_no, split_leg, bill_type, date,
                 customer_id, customer_name, total, margin, taxable_total, status,
                 payment_date, is_candidate, created_by, lines):
    """lines: list of {description, hsn, mrp, qty, gst_pct, rate, taxable_rate, amount, product_id}"""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO bills (file_name, invoice_no, original_invoice_no, split_leg, bill_type,
                                date, customer_id, customer_name, total, margin, taxable_total,
                                status, payment_date, is_candidate, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (file_name, invoice_no, original_invoice_no, split_leg, bill_type, date,
              customer_id, customer_name, total, margin, taxable_total, status,
              payment_date, 1 if is_candidate else 0, created_by))
        bill_id = cur.lastrowid
        for line in lines:
            cur.execute("""
                INSERT INTO bill_lines (bill_id, product_id, description, hsn, mrp, qty,
                                         gst_pct, rate, taxable_rate, amount)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (bill_id, line.get("product_id"), line["description"], line["hsn"], line.get("mrp"),
                  line["qty"], line["gst_pct"], line["rate"], line["taxable_rate"], line["amount"]))
        conn.commit()
        return bill_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_bills(include_deleted=False):
    conn = get_conn()
    q = "SELECT * FROM bills"
    if not include_deleted:
        q += " WHERE deleted = 0"
    q += " ORDER BY id"
    rows = conn.execute(q).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_bills_for_customer(customer_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM bills WHERE customer_id = ? AND deleted = 0 ORDER BY id DESC", (customer_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_bill(bill_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM bills WHERE id = ?", (bill_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_bill_by_invoice(invoice_no):
    conn = get_conn()
    row = conn.execute("SELECT * FROM bills WHERE invoice_no = ? AND deleted = 0", (invoice_no,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_bill_lines(bill_id):
    conn = get_conn()
    rows = conn.execute("SELECT * FROM bill_lines WHERE bill_id = ?", (bill_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_bill_status(invoice_no, status, payment_date=None):
    if status not in ("paid", "unpaid"):
        return
    if status == "unpaid":
        payment_date = None
    conn = get_conn()
    conn.execute(
        "UPDATE bills SET status = ?, payment_date = ?, updated_at = CURRENT_TIMESTAMP WHERE invoice_no = ?",
        (status, payment_date, invoice_no),
    )
    conn.commit()
    conn.close()


def soft_delete_bill(invoice_no):
    conn = get_conn()
    conn.execute(
        "UPDATE bills SET deleted = 1, updated_at = CURRENT_TIMESTAMP WHERE invoice_no = ?",
        (invoice_no,),
    )
    conn.commit()
    conn.close()


def update_bill_totals(bill_id, total, margin, taxable_total):
    conn = get_conn()
    conn.execute(
        "UPDATE bills SET total=?, margin=?, taxable_total=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (total, margin, taxable_total, bill_id),
    )
    conn.commit()
    conn.close()


def update_bill_type(bill_id, bill_type):
    conn = get_conn()
    conn.execute(
        "UPDATE bills SET bill_type=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (bill_type, bill_id),
    )
    conn.commit()
    conn.close()


def replace_bill_lines(bill_id, lines):
    """lines: same shape as insert_bill's `lines` param. Replaces all existing lines for this bill."""
    conn = get_conn()
    try:
        conn.execute("DELETE FROM bill_lines WHERE bill_id = ?", (bill_id,))
        for line in lines:
            conn.execute("""
                INSERT INTO bill_lines (bill_id, product_id, description, hsn, mrp, qty,
                                         gst_pct, rate, taxable_rate, amount)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (bill_id, line.get("product_id"), line["description"], line["hsn"], line.get("mrp"),
                  line["qty"], line["gst_pct"], line["rate"], line["taxable_rate"], line["amount"]))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# Purchase bills / purchase_bill_lines (vendor-side, mirrors bills/bill_lines)
# --------------------------------------------------------------------------- #
def increment_product_stock(product_id, qty, new_cost_price=None):
    """Adds qty to a product's stock (purchases increase stock, the opposite
    of a sale). Optionally overwrites cost_price with the latest purchase
    cost (simple latest-cost model, not weighted-average)."""
    if not product_id:
        return None
    conn = get_conn()
    if new_cost_price is not None:
        conn.execute("UPDATE products SET quantity = quantity + ?, cost_price = ? WHERE id = ?",
                     (qty, new_cost_price, product_id))
    else:
        conn.execute("UPDATE products SET quantity = quantity + ? WHERE id = ?", (qty, product_id))
    row = conn.execute("SELECT quantity FROM products WHERE id = ?", (product_id,)).fetchone()
    conn.commit()
    conn.close()
    return row["quantity"] if row else None


def next_purchase_number():
    conn = get_conn()
    row = conn.execute(
        "SELECT purchase_no FROM purchase_bills WHERE deleted = 0 ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()
    year = __import__("datetime").date.today().year
    if not row:
        return f"PB{year}0001"
    last = row["purchase_no"]
    # Expect PB<yyyy><NNNN>; fall back to a fresh series if the format doesn't match.
    if len(last) >= 8 and last[:2] == "PB" and last[-4:].isdigit():
        prefix, seq = last[:-4], last[-4:]
        return prefix + str(int(seq) + 1).zfill(4)
    return f"PB{year}0001"


def insert_purchase_bill(file_name, purchase_no, vendor_id, vendor_name, date, total,
                          taxable_total, status, payment_date, created_by, lines, money_request_id=None):
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO purchase_bills (file_name, purchase_no, vendor_id, vendor_name, date,
                                         total, taxable_total, status, payment_date, created_by, money_request_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (file_name, purchase_no, vendor_id, vendor_name, date, total, taxable_total,
              status, payment_date, created_by, money_request_id))
        purchase_bill_id = cur.lastrowid
        for line in lines:
            cur.execute("""
                INSERT INTO purchase_bill_lines (purchase_bill_id, product_id, description, hsn,
                                                  qty, gst_pct, cost_rate, amount)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (purchase_bill_id, line.get("product_id"), line["description"], line["hsn"],
                  line["qty"], line["gst_pct"], line["cost_rate"], line["amount"]))
        conn.commit()
        return purchase_bill_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_purchase_bills(include_deleted=False):
    conn = get_conn()
    q = "SELECT * FROM purchase_bills"
    if not include_deleted:
        q += " WHERE deleted = 0"
    q += " ORDER BY id DESC"
    rows = conn.execute(q).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_purchase_bills_for_vendor(vendor_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM purchase_bills WHERE vendor_id = ? AND deleted = 0 ORDER BY id DESC", (vendor_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_purchase_bill(purchase_bill_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM purchase_bills WHERE id = ?", (purchase_bill_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_purchase_bill_by_no(purchase_no):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM purchase_bills WHERE purchase_no = ? AND deleted = 0", (purchase_no,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_purchase_bill_lines(purchase_bill_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM purchase_bill_lines WHERE purchase_bill_id = ?", (purchase_bill_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_purchase_bill_status(purchase_no, status, payment_date=None):
    if status not in ("paid", "unpaid"):
        return
    if status == "unpaid":
        payment_date = None
    conn = get_conn()
    conn.execute(
        "UPDATE purchase_bills SET status=?, payment_date=?, updated_at=CURRENT_TIMESTAMP WHERE purchase_no=?",
        (status, payment_date, purchase_no),
    )
    conn.commit()
    conn.close()


def record_purchase_payment(purchase_id, amount, paid_via, payment_date, note, created_by):
    """Appends a payment against a purchase and recomputes its status from
    amount_paid vs total ('unpaid' / 'partial' / 'paid') - this is how
    partial vendor payments work, which is the normal case for small supply
    businesses, not the exception. paid_via: 'cash' or 'vendor_wallet' - if
    'vendor_wallet', debits that vendor's wallet by the same amount (with
    the usual balance check)."""
    if amount is None or amount <= 0:
        raise ValueError("amount must be positive")
    purchase = get_purchase_bill_by_id(purchase_id)
    if not purchase:
        raise ValueError("purchase not found")
    remaining = round(purchase["total"] - purchase["amount_paid"], 2)
    if amount > remaining + 0.01:
        raise ValueError(f"amount (₹{amount:,.2f}) exceeds the remaining balance (₹{remaining:,.2f})")

    if paid_via == "vendor_wallet":
        balance = get_vendor_wallet_balance(purchase["vendor_id"])
        if balance < amount:
            raise ValueError(f"insufficient vendor wallet balance (₹{balance:,.2f} available, ₹{amount:,.2f} needed)")

    conn = get_conn()
    try:
        conn.execute("""
            INSERT INTO purchase_payments (purchase_id, amount, paid_via, payment_date, note, created_by)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (purchase_id, amount, paid_via, payment_date, note or "", created_by or ""))

        new_amount_paid = round(purchase["amount_paid"] + amount, 2)
        if new_amount_paid >= purchase["total"] - 0.01:
            new_status = "paid"
        elif new_amount_paid > 0:
            new_status = "partial"
        else:
            new_status = "unpaid"

        conn.execute("""
            UPDATE purchase_bills SET amount_paid=?, status=?,
                   payment_date=CASE WHEN ? = 'paid' THEN ? ELSE payment_date END,
                   updated_at=CURRENT_TIMESTAMP
            WHERE id=?
        """, (new_amount_paid, new_status, new_status, payment_date, purchase_id))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    if paid_via == "vendor_wallet":
        add_vendor_wallet_entry(
            purchase["vendor_id"], purchase["vendor_name"], "debit", amount,
            note=f"Payment on purchase {purchase['purchase_no']}", created_by=created_by,
        )


def get_purchase_bill_by_id(purchase_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM purchase_bills WHERE id = ?", (purchase_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_purchase_payments(purchase_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM purchase_payments WHERE purchase_id = ? ORDER BY created_at DESC", (purchase_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_purchases_for_money_request(money_request_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM purchase_bills WHERE money_request_id = ? AND deleted = 0 ORDER BY id", (money_request_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_money_request_spent(money_request_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT COALESCE(SUM(total), 0) AS spent FROM purchase_bills WHERE money_request_id = ? AND deleted = 0",
        (money_request_id,),
    ).fetchone()
    conn.close()
    return round(row["spent"], 2)


def soft_delete_purchase_bill(purchase_no):
    conn = get_conn()
    conn.execute(
        "UPDATE purchase_bills SET deleted = 1, updated_at = CURRENT_TIMESTAMP WHERE purchase_no = ?",
        (purchase_no,),
    )
    conn.commit()
    conn.close()


def purchase_bill_count():
    conn = get_conn()
    row = conn.execute("SELECT COUNT(*) AS c FROM purchase_bills").fetchone()
    conn.close()
    return row["c"]


def update_purchase_bill_totals(purchase_bill_id, total, taxable_total):
    conn = get_conn()
    conn.execute(
        "UPDATE purchase_bills SET total=?, taxable_total=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (total, taxable_total, purchase_bill_id),
    )
    conn.commit()
    conn.close()


def replace_purchase_bill_lines(purchase_bill_id, lines):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM purchase_bill_lines WHERE purchase_bill_id = ?", (purchase_bill_id,))
        for line in lines:
            conn.execute("""
                INSERT INTO purchase_bill_lines (purchase_bill_id, product_id, description, hsn,
                                                  qty, gst_pct, cost_rate, amount)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (purchase_bill_id, line.get("product_id"), line["description"], line["hsn"],
                  line["qty"], line["gst_pct"], line["cost_rate"], line["amount"]))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def insert_product(sheet, item_code, item_name, item_description, hsn, mrp,
                    quantity, gst_pct, cost_price, sale_price):
    """Distinct from upsert_product: fails (raises ValueError) if a product
    with the same sheet+name+description already exists, rather than
    silently updating it - appropriate for an explicit 'Add product' form."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO products (sheet, item_code, item_name, item_description, hsn, mrp,
                                   quantity, gst_pct, cost_price, sale_price)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (sheet, item_code or "", item_name, item_description or "", hsn or "", mrp,
              quantity or 0, gst_pct or 0, cost_price or 0, sale_price or 0))
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        conn.rollback()
        raise ValueError("A product with this sheet + name + description already exists")
    finally:
        conn.close()


def update_product_by_id(product_id, sheet, item_code, item_name, item_description, hsn, mrp,
                          quantity, gst_pct, cost_price, sale_price):
    conn = get_conn()
    try:
        conn.execute("""
            UPDATE products SET sheet=?, item_code=?, item_name=?, item_description=?, hsn=?,
                                 mrp=?, quantity=?, gst_pct=?, cost_price=?, sale_price=?
            WHERE id=?
        """, (sheet, item_code or "", item_name, item_description or "", hsn or "", mrp,
              quantity or 0, gst_pct or 0, cost_price or 0, sale_price or 0, product_id))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        raise ValueError("Another product with this sheet + name + description already exists")
    finally:
        conn.close()


def list_product_sheets():
    """All distinct sheet/category values currently in use - for the Add
    Product form's dropdown and the Browse-products category filter."""
    conn = get_conn()
    rows = conn.execute("SELECT DISTINCT sheet FROM products ORDER BY sheet").fetchall()
    conn.close()
    return [r["sheet"] for r in rows]


# --------------------------------------------------------------------------- #
# Product approval workflow (field_staff-created products need admin sign-off)
# --------------------------------------------------------------------------- #
def insert_product_pending(sheet, item_code, item_name, item_description, hsn, mrp,
                            quantity, gst_pct, cost_price, sale_price, created_by):
    """Same as insert_product but always lands as approved=0 - used when a
    field_staff user creates a product (see insert_product for the
    admin/sales/purchase path, which stays approved=1)."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO products (sheet, item_code, item_name, item_description, hsn, mrp,
                                   quantity, gst_pct, cost_price, sale_price, approved, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
        """, (sheet, item_code or "", item_name, item_description or "", hsn or "", mrp,
              quantity or 0, gst_pct or 0, cost_price or 0, sale_price or 0, created_by or ""))
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        conn.rollback()
        raise ValueError("A product with this sheet + name + description already exists")
    finally:
        conn.close()


def list_pending_products():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM products WHERE approved = 0 ORDER BY id DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def approve_product(product_id):
    conn = get_conn()
    conn.execute("UPDATE products SET approved = 1 WHERE id = ?", (product_id,))
    conn.commit()
    conn.close()


def reject_pending_product(product_id):
    """Pending products that get rejected are just deleted - they were never
    live inventory, so there's nothing to reverse."""
    conn = get_conn()
    conn.execute("DELETE FROM products WHERE id = ? AND approved = 0", (product_id,))
    conn.commit()
    conn.close()


def list_products(approved_only=False):
    conn = get_conn()
    q = "SELECT * FROM products"
    if approved_only:
        q += " WHERE approved = 1"
    q += " ORDER BY sheet, item_name"
    rows = conn.execute(q).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_products_for_user(username):
    """
    Every approved product, PLUS this user's own still-pending submissions -
    so a field_staff user can immediately use a product they just added in
    their own Purchase/Delivery, without waiting for admin approval, while
    everyone else (and other users' pending submissions) still can't see it
    until it's approved. Stock/cost on the product row update normally
    either way (decrement/increment never check the approved flag) - only
    *visibility in search* was ever gated, so nothing else needed fixing
    once this exists.
    """
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM products WHERE approved = 1 OR created_by = ? ORDER BY sheet, item_name",
        (username,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Delivery receipts
# --------------------------------------------------------------------------- #
def next_delivery_receipt_number():
    conn = get_conn()
    row = conn.execute("SELECT receipt_no FROM delivery_receipts ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    year = __import__("datetime").date.today().year
    if not row:
        return f"DR{year}0001"
    last = row["receipt_no"]
    if len(last) >= 8 and last[:2] == "DR" and last[-4:].isdigit():
        prefix, seq = last[:-4], last[-4:]
        return prefix + str(int(seq) + 1).zfill(4)
    return f"DR{year}0001"


def insert_delivery_receipt(receipt_no, customer_id, customer_name, date, created_by, lines):
    """lines: [{product_id, description, qty, update_stock}]. Always starts
    'pending' - stock isn't touched until an admin approves it (see
    approve_delivery_receipt), regardless of what a migrated old DB's
    schema default says for this column."""
    conn = get_conn()
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO delivery_receipts (receipt_no, customer_id, customer_name, date, status, created_by)
            VALUES (?, ?, ?, ?, 'pending', ?)
        """, (receipt_no, customer_id, customer_name, date, created_by))
        receipt_id = cur.lastrowid
        for line in lines:
            cur.execute("""
                INSERT INTO delivery_receipt_lines (receipt_id, product_id, description, qty, update_stock)
                VALUES (?, ?, ?, ?, ?)
            """, (receipt_id, line.get("product_id"), line["description"], line["qty"],
                  1 if line.get("update_stock", True) else 0))
        conn.commit()
        return receipt_id
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def replace_delivery_receipt_lines(receipt_id, lines):
    conn = get_conn()
    try:
        conn.execute("DELETE FROM delivery_receipt_lines WHERE receipt_id = ?", (receipt_id,))
        for line in lines:
            conn.execute("""
                INSERT INTO delivery_receipt_lines (receipt_id, product_id, description, qty, update_stock)
                VALUES (?, ?, ?, ?, ?)
            """, (receipt_id, line.get("product_id"), line["description"], line["qty"],
                  1 if line.get("update_stock", True) else 0))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_delivery_receipts():
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM delivery_receipts WHERE deleted = 0 ORDER BY id DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_pending_delivery_receipts():
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM delivery_receipts WHERE deleted = 0 AND status = 'pending' ORDER BY id DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def count_pending_delivery_receipts_for_customer(customer_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM delivery_receipts WHERE customer_id = ? AND status = 'pending' AND deleted = 0",
        (customer_id,),
    ).fetchone()
    conn.close()
    return row["c"]


def get_delivery_receipt(receipt_no):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM delivery_receipts WHERE receipt_no = ? AND deleted = 0", (receipt_no,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_delivery_receipt_by_id(receipt_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM delivery_receipts WHERE id = ?", (receipt_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_delivery_receipt_lines(receipt_id):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM delivery_receipt_lines WHERE receipt_id = ?", (receipt_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def approve_delivery_receipt(receipt_id, approved_by):
    """Decrements stock only for lines tagged update_stock=1 ('US') - 'DU'
    lines were delivered but never affect inventory. Only callable on a
    still-pending receipt."""
    receipt = get_delivery_receipt_by_id(receipt_id)
    if not receipt or receipt["status"] != "pending":
        raise ValueError("Receipt not found or already resolved")

    for line in get_delivery_receipt_lines(receipt_id):
        if line["update_stock"] and line["product_id"]:
            decrement_product_stock(line["product_id"], line["qty"])

    conn = get_conn()
    conn.execute(
        "UPDATE delivery_receipts SET status='approved', approved_by=?, approved_at=CURRENT_TIMESTAMP WHERE id=?",
        (approved_by, receipt_id),
    )
    conn.commit()
    conn.close()


def reject_delivery_receipt(receipt_id):
    """Pending receipts never touched stock, so rejecting is a plain
    soft-delete - nothing to reverse."""
    receipt = get_delivery_receipt_by_id(receipt_id)
    if not receipt or receipt["status"] != "pending":
        raise ValueError("Receipt not found or already resolved")
    conn = get_conn()
    conn.execute("UPDATE delivery_receipts SET deleted = 1 WHERE id = ?", (receipt_id,))
    conn.commit()
    conn.close()


def soft_delete_delivery_receipt(receipt_no):
    """If the receipt was already approved, reverses stock for whichever
    lines had update_stock=1 before deleting (mirrors Purchase Delete)."""
    receipt = get_delivery_receipt(receipt_no)
    if not receipt:
        return
    if receipt["status"] == "approved":
        for line in get_delivery_receipt_lines(receipt["id"]):
            if line["update_stock"] and line["product_id"]:
                increment_product_stock(line["product_id"], line["qty"])
    conn = get_conn()
    conn.execute("UPDATE delivery_receipts SET deleted = 1 WHERE receipt_no = ?", (receipt_no,))
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------- #
# Money requests + reasons + cash ledger (field_staff cash-in-hand tracking)
# --------------------------------------------------------------------------- #
def list_money_request_reasons():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM money_request_reasons ORDER BY label").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_money_request_reason(label):
    conn = get_conn()
    try:
        conn.execute("INSERT INTO money_request_reasons (label) VALUES (?)", (label,))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        raise ValueError(f"Reason '{label}' already exists")
    finally:
        conn.close()


def delete_money_request_reason(reason_id):
    conn = get_conn()
    conn.execute("DELETE FROM money_request_reasons WHERE id = ?", (reason_id,))
    conn.commit()
    conn.close()


def create_money_request(requested_by, amount, reason, note, is_gst_bill):
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO money_requests (requested_by, amount, reason, note, is_gst_bill)
        VALUES (?, ?, ?, ?, ?)
    """, (requested_by, amount, reason, note or "", 1 if is_gst_bill else 0))
    conn.commit()
    request_id = cur.lastrowid
    conn.close()
    return request_id


def list_money_requests(requested_by=None, status=None):
    conn = get_conn()
    q = "SELECT * FROM money_requests WHERE 1=1"
    params = []
    if requested_by:
        q += " AND requested_by = ?"
        params.append(requested_by)
    if status:
        q += " AND status = ?"
        params.append(status)
    q += " ORDER BY id DESC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_money_request(request_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM money_requests WHERE id = ?", (request_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def resolve_money_request(request_id, status, resolved_by):
    """status: 'approved' or 'rejected'. On approval, credits the requester's
    cash ledger with the disbursed amount."""
    request = get_money_request(request_id)
    if not request or request["status"] != "pending":
        raise ValueError("Request not found or already resolved")

    conn = get_conn()
    conn.execute("""
        UPDATE money_requests SET status = ?, resolved_by = ?, resolved_at = CURRENT_TIMESTAMP
        WHERE id = ?
    """, (status, resolved_by, request_id))
    conn.commit()
    conn.close()

    if status == "approved":
        add_cash_ledger_entry(
            request["requested_by"], "credit", request["amount"],
            note=f"Money request #{request_id} approved ({request['reason']})",
            linked_money_request_id=request_id,
        )


def link_purchase_to_money_request(request_id, purchase_no):
    conn = get_conn()
    conn.execute("UPDATE money_requests SET linked_purchase_no = ? WHERE id = ?", (purchase_no, request_id))
    conn.commit()
    conn.close()


def add_cash_ledger_entry(holder_username, entry_type, amount, note="",
                           linked_money_request_id=None, linked_purchase_no=None):
    if entry_type not in ("credit", "debit"):
        raise ValueError("entry_type must be 'credit' or 'debit'")
    conn = get_conn()
    conn.execute("""
        INSERT INTO cash_ledger (holder_username, entry_type, amount, note,
                                  linked_money_request_id, linked_purchase_no)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (holder_username, entry_type, amount, note, linked_money_request_id, linked_purchase_no))
    conn.commit()
    conn.close()


def get_cash_balance(holder_username):
    conn = get_conn()
    row = conn.execute("""
        SELECT COALESCE(SUM(CASE WHEN entry_type='credit' THEN amount ELSE 0 END), 0) -
               COALESCE(SUM(CASE WHEN entry_type='debit' THEN amount ELSE 0 END), 0) AS balance
        FROM cash_ledger WHERE holder_username = ?
    """, (holder_username,)).fetchone()
    conn.close()
    return round(row["balance"], 2)


def get_cash_ledger_history(holder_username, entry_type=None, date_from=None, date_to=None):
    conn = get_conn()
    q = "SELECT * FROM cash_ledger WHERE holder_username = ?"
    params = [holder_username]
    if entry_type in ("credit", "debit"):
        q += " AND entry_type = ?"
        params.append(entry_type)
    if date_from:
        q += " AND date(created_at) >= date(?)"
        params.append(date_from)
    if date_to:
        q += " AND date(created_at) <= date(?)"
        params.append(date_to)
    q += " ORDER BY created_at DESC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --------------------------------------------------------------------------- #
# Custom field attributes (admin-configurable, per entity type)
#
# entity_type/field_type are validated here in Python, not via a DB-level
# CHECK constraint - see the note above the custom_fields CREATE TABLE for
# why (a CHECK baked in at table-creation time silently breaks the moment
# you need to add a new value later, and SQLite reports that as the same
# error type as a genuine uniqueness violation, which is exactly what bit
# the users.role column earlier in this project).
# --------------------------------------------------------------------------- #
CUSTOM_FIELD_ENTITY_TYPES = ("product", "customer", "vendor")
CUSTOM_FIELD_TYPES = ("text", "number")


def _slugify_field_key(label):
    slug = re.sub(r"[^a-z0-9]+", "_", label.strip().lower()).strip("_")
    return slug or "field"


def list_custom_fields(entity_type):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM custom_fields WHERE entity_type = ? ORDER BY display_order, id",
        (entity_type,),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_custom_field(entity_type, label, field_type="text"):
    if entity_type not in CUSTOM_FIELD_ENTITY_TYPES:
        raise ValueError(f"invalid entity_type '{entity_type}'")
    if field_type not in CUSTOM_FIELD_TYPES:
        raise ValueError(f"invalid field_type '{field_type}'")
    label = (label or "").strip()
    if not label:
        raise ValueError("field label can't be empty")
    key = _slugify_field_key(label)

    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT COALESCE(MAX(display_order), -1) + 1 AS next_order FROM custom_fields WHERE entity_type = ?",
            (entity_type,),
        ).fetchone()
        conn.execute(
            "INSERT INTO custom_fields (entity_type, field_key, field_label, field_type, display_order) "
            "VALUES (?, ?, ?, ?, ?)",
            (entity_type, key, label, field_type, row["next_order"]),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        raise ValueError(f"A field named '{label}' already exists for {entity_type}")
    finally:
        conn.close()


def delete_custom_field(field_id):
    conn = get_conn()
    conn.execute("DELETE FROM custom_field_values WHERE field_id = ?", (field_id,))
    conn.execute("DELETE FROM custom_fields WHERE id = ?", (field_id,))
    conn.commit()
    conn.close()


def get_custom_field_values(entity_type, entity_id):
    """Returns {field_key: value} for every custom field defined on this
    entity type, using '' for fields with no value stored yet."""
    fields = list_custom_fields(entity_type)
    conn = get_conn()
    rows = conn.execute(
        "SELECT field_id, value FROM custom_field_values WHERE entity_id = ? "
        "AND field_id IN (SELECT id FROM custom_fields WHERE entity_type = ?)",
        (str(entity_id), entity_type),
    ).fetchall()
    conn.close()
    by_field_id = {r["field_id"]: r["value"] for r in rows}
    return {f["field_key"]: by_field_id.get(f["id"], "") for f in fields}


def set_custom_field_values(entity_type, entity_id, values_by_key):
    """values_by_key: {field_key: value} - only keys matching a defined
    field for this entity_type are saved; everything else is ignored."""
    fields = {f["field_key"]: f["id"] for f in list_custom_fields(entity_type)}
    conn = get_conn()
    try:
        for key, value in values_by_key.items():
            field_id = fields.get(key)
            if field_id is None:
                continue
            conn.execute("""
                INSERT INTO custom_field_values (field_id, entity_id, value) VALUES (?, ?, ?)
                ON CONFLICT(field_id, entity_id) DO UPDATE SET value=excluded.value
            """, (field_id, str(entity_id), value or ""))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_custom_field_values_bulk(entity_type, entity_ids):
    """Same as get_custom_field_values but for many entities at once (list
    pages) - one query instead of N. Returns {entity_id_str: {field_key: value}}."""
    entity_ids_str = [str(e) for e in entity_ids]
    fields = list_custom_fields(entity_type)
    result = {e: {f["field_key"]: "" for f in fields} for e in entity_ids_str}
    if not fields or not entity_ids_str:
        return result

    field_by_id = {f["id"]: f["field_key"] for f in fields}
    id_placeholders = ",".join("?" * len(entity_ids_str))
    field_placeholders = ",".join("?" * len(field_by_id))
    conn = get_conn()
    rows = conn.execute(
        f"SELECT field_id, entity_id, value FROM custom_field_values "
        f"WHERE entity_id IN ({id_placeholders}) AND field_id IN ({field_placeholders})",
        entity_ids_str + list(field_by_id.keys()),
    ).fetchall()
    conn.close()
    for r in rows:
        if r["entity_id"] in result:
            result[r["entity_id"]][field_by_id[r["field_id"]]] = r["value"]
    return result


# --------------------------------------------------------------------------- #
# Salary expense (owner <-> employee, tied into the same cash_ledger used for
# field_staff money requests - "wallet" for a person's cash-in-hand balance
# is one concept regardless of whether the credit came from a money request
# or a salary payment).
# --------------------------------------------------------------------------- #
def current_salary_period():
    """'YYYY-MM' for the current month - one salary record per employee per period."""
    return datetime.date.today().strftime("%Y-%m")


def is_salary_due(user, period=None):
    """An employee is 'due' for a period if they have a joining_date on file
    and no salary_payments row exists yet for that period. (Doesn't try to
    guess partial-month proration - just flags the period as needing a
    decision once it's underway.)"""
    if not user.get("joining_date"):
        return False
    period = period or current_salary_period()
    return get_salary_payment(user["username"], period) is None


def list_employees_for_salary():
    """Every user with a joining_date on file (i.e. opted into salary
    tracking) plus this period's payment status, if any."""
    period = current_salary_period()
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, username, full_name, role, joining_date, monthly_salary FROM users "
        "WHERE joining_date IS NOT NULL AND joining_date != '' ORDER BY joining_date"
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        u = dict(r)
        u["current_period"] = period
        u["current_payment"] = get_salary_payment(u["username"], period)
        u["due"] = u["current_payment"] is None
        result.append(u)
    return result


def get_salary_payment(username, period):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM salary_payments WHERE username = ? AND period = ?", (username, period)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def get_salary_payment_by_id(payment_id):
    conn = get_conn()
    row = conn.execute("SELECT * FROM salary_payments WHERE id = ?", (payment_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_salary_payments(username=None):
    conn = get_conn()
    if username:
        rows = conn.execute(
            "SELECT * FROM salary_payments WHERE username = ? ORDER BY period DESC", (username,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM salary_payments ORDER BY period DESC, username").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def approve_salary_payment(username, period, amount, note, approved_by):
    """Decides/confirms the amount for this period - does NOT touch the
    cash ledger yet. That only happens at mark_salary_paid, once the
    employee has actually received the money."""
    if amount is None or amount <= 0:
        raise ValueError("amount must be positive")
    conn = get_conn()
    try:
        conn.execute("""
            INSERT INTO salary_payments (username, period, amount, status, note, approved_by, approved_at)
            VALUES (?, ?, ?, 'approved', ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(username, period) DO UPDATE SET
                amount=excluded.amount, note=excluded.note, approved_by=excluded.approved_by,
                approved_at=CURRENT_TIMESTAMP
            WHERE salary_payments.status = 'approved'
        """, (username, period, amount, note or "", approved_by))
        conn.commit()
    finally:
        conn.close()


def mark_salary_paid(payment_id, paid_by):
    payment = get_salary_payment_by_id(payment_id)
    if not payment or payment["status"] != "approved":
        raise ValueError("Payment not found or already paid")
    conn = get_conn()
    conn.execute(
        "UPDATE salary_payments SET status='paid', paid_by=?, paid_at=CURRENT_TIMESTAMP WHERE id=?",
        (paid_by, payment_id),
    )
    conn.commit()
    conn.close()

    add_cash_ledger_entry(
        payment["username"], "credit", payment["amount"],
        note=f"Salary for {payment['period']}",
    )


# --------------------------------------------------------------------------- #
# Customer/Vendor category options (admin-managed canonical lists used for
# filter dropdowns and the Add/Edit party forms). Separate from the
# free-text category value actually stored on each customer/vendor row.
# --------------------------------------------------------------------------- #
def list_customer_category_options():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM customer_categories ORDER BY label").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_customer_category_option(label):
    label = (label or "").strip()
    if not label:
        raise ValueError("category can't be empty")
    conn = get_conn()
    try:
        conn.execute("INSERT INTO customer_categories (label) VALUES (?)", (label,))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        raise ValueError(f"'{label}' already exists")
    finally:
        conn.close()


def delete_customer_category_option(category_id):
    conn = get_conn()
    conn.execute("DELETE FROM customer_categories WHERE id = ?", (category_id,))
    conn.commit()
    conn.close()


def list_vendor_category_options():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM vendor_categories ORDER BY label").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_vendor_category_option(label):
    label = (label or "").strip()
    if not label:
        raise ValueError("category can't be empty")
    conn = get_conn()
    try:
        conn.execute("INSERT INTO vendor_categories (label) VALUES (?)", (label,))
        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        raise ValueError(f"'{label}' already exists")
    finally:
        conn.close()


def delete_vendor_category_option(category_id):
    conn = get_conn()
    conn.execute("DELETE FROM vendor_categories WHERE id = ?", (category_id,))
    conn.commit()
    conn.close()


# --------------------------------------------------------------------------- #
# Delivery -> Invoice conversion (admin reviews everything delivered to a
# customer across possibly several delivery receipts, groups it however
# makes sense, and turns each group into a real GST invoice)
# --------------------------------------------------------------------------- #
def get_uninvoiced_delivery_lines_for_customer(customer_id):
    """Every line, across every APPROVED (not pending, not deleted) delivery
    receipt for this customer, that hasn't already been folded into an
    invoice. US/DU status doesn't affect eligibility - that flag is about
    inventory bookkeeping, not about whether the customer owes for the
    item, so both kinds of lines are billable."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT drl.id AS line_id, drl.product_id, drl.description, drl.qty, drl.update_stock,
               dr.id AS receipt_id, dr.receipt_no, dr.date AS delivery_date
        FROM delivery_receipt_lines drl
        JOIN delivery_receipts dr ON dr.id = drl.receipt_id
        WHERE dr.customer_id = ? AND dr.status = 'approved' AND dr.deleted = 0
              AND drl.invoiced_bill_id IS NULL
        ORDER BY dr.id, drl.id
    """, (customer_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def mark_delivery_lines_invoiced(line_ids, bill_id):
    if not line_ids:
        return
    conn = get_conn()
    placeholders = ",".join("?" * len(line_ids))
    conn.execute(
        f"UPDATE delivery_receipt_lines SET invoiced_bill_id = ? WHERE id IN ({placeholders})",
        [bill_id] + list(line_ids),
    )
    conn.commit()
    conn.close()


def get_delivery_receipt_invoice_status(receipt_id):
    """{'total': N, 'invoiced': N, 'invoices': [{'invoice_no':..,'file_name':..}]}
    for one receipt - used to show admin and the delivering field-staff
    member whether (and into which invoice(s)) a delivery has been billed."""
    conn = get_conn()
    lines = conn.execute(
        "SELECT invoiced_bill_id FROM delivery_receipt_lines WHERE receipt_id = ?", (receipt_id,)
    ).fetchall()
    total = len(lines)
    invoiced = sum(1 for l in lines if l["invoiced_bill_id"])
    bill_ids = sorted(set(l["invoiced_bill_id"] for l in lines if l["invoiced_bill_id"]))
    invoices = []
    if bill_ids:
        placeholders = ",".join("?" * len(bill_ids))
        rows = conn.execute(
            f"SELECT DISTINCT invoice_no, file_name FROM bills WHERE id IN ({placeholders})", bill_ids
        ).fetchall()
        invoices = [{"invoice_no": r["invoice_no"], "file_name": r["file_name"]} for r in rows]
    conn.close()
    return {"total": total, "invoiced": invoiced, "invoices": invoices}


# --------------------------------------------------------------------------- #
# Field-staff scoping: admin assigns which specific customers/vendors a
# field_staff user can see and work with, instead of the full list.
# Fallback behavior: a field_staff user with NO assignments yet sees
# everything (so existing/freshly-created accounts aren't locked out by
# default) - the restriction only kicks in once admin assigns at least one.
# --------------------------------------------------------------------------- #
def assign_customer_to_staff(username, customer_id):
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO field_staff_customers (username, customer_id) VALUES (?, ?)",
        (username, customer_id),
    )
    conn.commit()
    conn.close()


def unassign_customer_from_staff(username, customer_id):
    conn = get_conn()
    conn.execute(
        "DELETE FROM field_staff_customers WHERE username = ? AND customer_id = ?", (username, customer_id)
    )
    conn.commit()
    conn.close()


def list_assigned_customer_ids(username):
    conn = get_conn()
    rows = conn.execute(
        "SELECT customer_id FROM field_staff_customers WHERE username = ?", (username,)
    ).fetchall()
    conn.close()
    return [r["customer_id"] for r in rows]


def assign_vendor_to_staff(username, vendor_id):
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO field_staff_vendors (username, vendor_id) VALUES (?, ?)",
        (username, vendor_id),
    )
    conn.commit()
    conn.close()


def unassign_vendor_from_staff(username, vendor_id):
    conn = get_conn()
    conn.execute(
        "DELETE FROM field_staff_vendors WHERE username = ? AND vendor_id = ?", (username, vendor_id)
    )
    conn.commit()
    conn.close()


def list_assigned_vendor_ids(username):
    conn = get_conn()
    rows = conn.execute(
        "SELECT vendor_id FROM field_staff_vendors WHERE username = ?", (username,)
    ).fetchall()
    conn.close()
    return [r["vendor_id"] for r in rows]


def list_customers_for_user(username, role):
    """Every customer, unless this is a field_staff user with at least one
    explicit assignment - then only their assigned customers."""
    customers = list_customers()
    if role == "field_staff":
        assigned = set(list_assigned_customer_ids(username))
        if assigned:
            customers = [c for c in customers if c["customer_id"] in assigned]
    return customers


def list_vendors_for_user(username, role):
    vendors = list_vendors()
    if role == "field_staff":
        assigned = set(list_assigned_vendor_ids(username))
        if assigned:
            vendors = [v for v in vendors if v["vendor_id"] in assigned]
    return vendors


# --------------------------------------------------------------------------- #
# Vendor <-> Product mapping: admin curates which products belong to which
# vendor, so New Purchase only offers a relevant, deliberate list instead of
# the entire catalog - plus whatever a field_staff user has personally added
# (pending or since-approved), since those genuinely came from that vendor
# but haven't been formally mapped yet.
# --------------------------------------------------------------------------- #
def map_product_to_vendor(vendor_id, product_id):
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO vendor_products (vendor_id, product_id) VALUES (?, ?)", (vendor_id, product_id)
    )
    conn.commit()
    conn.close()


def unmap_product_from_vendor(vendor_id, product_id):
    conn = get_conn()
    conn.execute(
        "DELETE FROM vendor_products WHERE vendor_id = ? AND product_id = ?", (vendor_id, product_id)
    )
    conn.commit()
    conn.close()


def list_products_for_vendor(vendor_id):
    conn = get_conn()
    rows = conn.execute("""
        SELECT p.* FROM products p
        JOIN vendor_products vp ON vp.product_id = p.id
        WHERE vp.vendor_id = ?
        ORDER BY p.sheet, p.item_name
    """, (vendor_id,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_mapped_product_ids_for_vendor(vendor_id):
    conn = get_conn()
    rows = conn.execute("SELECT product_id FROM vendor_products WHERE vendor_id = ?", (vendor_id,)).fetchall()
    conn.close()
    return set(r["product_id"] for r in rows)


def list_products_for_vendor_purchase(vendor_id, username):
    """What New Purchase should actually offer once a vendor is picked:
    products mapped to that vendor, plus this user's own field_staff
    submissions (pending or approved) regardless of mapping - they know
    where those came from even if admin hasn't formally mapped it yet."""
    mapped = list_products_for_vendor(vendor_id)
    mapped_ids = {p["id"] for p in mapped}
    conn = get_conn()
    own_rows = conn.execute(
        "SELECT * FROM products WHERE created_by = ? AND id NOT IN (SELECT product_id FROM vendor_products WHERE vendor_id = ?)",
        (username, vendor_id),
    ).fetchall()
    conn.close()
    return mapped + [dict(r) for r in own_rows if r["id"] not in mapped_ids]


# --------------------------------------------------------------------------- #
# Customer-specific price memory: what this customer was actually charged
# for this product last time, shown as a recommended price (admin/sales can
# accept or override) rather than always defaulting to the flat catalog
# price. Only shown for items this customer has actually bought before -
# a first-time item always shows the plain catalog price, never a price
# inferred from someone else's history.
# --------------------------------------------------------------------------- #
def get_customer_product_price(customer_id, product_id):
    conn = get_conn()
    row = conn.execute(
        "SELECT last_price FROM customer_product_prices WHERE customer_id = ? AND product_id = ?",
        (customer_id, product_id),
    ).fetchone()
    conn.close()
    return row["last_price"] if row else None


def set_customer_product_price(customer_id, product_id, price):
    conn = get_conn()
    conn.execute("""
        INSERT INTO customer_product_prices (customer_id, product_id, last_price, updated_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(customer_id, product_id) DO UPDATE SET
            last_price = excluded.last_price, updated_at = CURRENT_TIMESTAMP
    """, (customer_id, product_id, price))
    conn.commit()
    conn.close()


def get_customer_product_prices_bulk(customer_id, product_ids):
    if not product_ids:
        return {}
    conn = get_conn()
    placeholders = ",".join("?" * len(product_ids))
    rows = conn.execute(
        f"SELECT product_id, last_price FROM customer_product_prices "
        f"WHERE customer_id = ? AND product_id IN ({placeholders})",
        [customer_id] + list(product_ids),
    ).fetchall()
    conn.close()
    return {r["product_id"]: r["last_price"] for r in rows}


# --------------------------------------------------------------------------- #
# Business profile (admin-configurable, used by PDF generators)
# --------------------------------------------------------------------------- #
BUSINESS_PROFILE_KEYS = [
    "business_name", "tagline", "gstin", "address_line1", "address_line2",
    "state", "state_code", "phone", "email",
    "bank_name", "bank_account", "bank_ifsc", "bank_branch",
]

BUSINESS_PROFILE_DEFAULTS = {
    "business_name": "TRUE STORE",
    "tagline": "Complete Office Solutions",
    "state": "Jharkhand",
    "state_code": "20",
}


def get_business_profile():
    """Returns a dict with all profile keys (missing keys get defaults)."""
    conn = get_conn()
    rows = conn.execute("SELECT key, value FROM business_profile").fetchall()
    conn.close()
    stored = {r["key"]: r["value"] for r in rows}
    result = {}
    for k in BUSINESS_PROFILE_KEYS:
        result[k] = stored.get(k) or BUSINESS_PROFILE_DEFAULTS.get(k, "")
    return result


def set_business_profile(data):
    """Upsert all provided keys into the business_profile table."""
    conn = get_conn()
    for k in BUSINESS_PROFILE_KEYS:
        val = (data.get(k) or "").strip()
        conn.execute("""
            INSERT INTO business_profile (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (k, val))
    conn.commit()
    conn.close()


def change_user_password(user_id, new_password):
    """Update a user's password hash."""
    conn = get_conn()
    conn.execute(
        "UPDATE users SET password_hash = ? WHERE id = ?",
        (generate_password_hash(new_password), user_id),
    )
    conn.commit()
    conn.close()


def admin_reset_user_password(user_id, new_password):
    """Admin resets another user's password."""
    return change_user_password(user_id, new_password)
