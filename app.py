import io
import os
import re
import json
import time
import string
import logging
import datetime
import zipfile
from collections import defaultdict
from logging.handlers import RotatingFileHandler

from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, send_file, session
from werkzeug.middleware.proxy_fix import ProxyFix

import db
import invoice_files
import billing_engine
import bill_pdf
import purchase_pdf
import delivery_pdf
import bills_repo
import migration
import record_room_lookup as rrl
from auth import login_required, role_required, current_user
from data_processor import (
    load_invoices, enrich_invoices, apply_filters, group_by_customer,
    compute_totals, AMOUNT_RANGES,
)
from summary_pdf import build_summary_pdf

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DEFAULT_FILE = os.path.join(DATA_DIR, "sales_log.xlsx")
BILLDESK_FILE = os.path.join(DATA_DIR, "billdesk.xlsx")
SECRET_KEY_FILE = os.path.join(DATA_DIR, "secret_key.txt")

os.makedirs(DATA_DIR, exist_ok=True)


def _load_or_create_secret_key():
    """
    Prefers a SECRET_KEY env var (set this in production). Otherwise persists
    a random key to disk so sessions survive process restarts, rather than
    ever falling back to a hardcoded value.
    """
    env_key = os.environ.get("SECRET_KEY")
    if env_key:
        return env_key
    if os.path.exists(SECRET_KEY_FILE):
        with open(SECRET_KEY_FILE, "r") as f:
            return f.read().strip()
    import secrets
    key = secrets.token_hex(32)
    with open(SECRET_KEY_FILE, "w") as f:
        f.write(key)
    return key


app = Flask(__name__)
app.secret_key = _load_or_create_secret_key()
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # 20 MB upload cap

# --------------------------------------------------------------------------- #
# Production hardening (harmless in dev; the security-sensitive bits only
# activate when FLASK_ENV=production is set, so local http:// testing still
# works without a browser rejecting the session cookie).
# --------------------------------------------------------------------------- #
IS_PRODUCTION = os.environ.get("FLASK_ENV") == "production"

# Trust exactly one reverse-proxy hop's X-Forwarded-* headers (nginx/Caddy in
# front of gunicorn/waitress) so request.remote_addr and url_for(_external=True)
# reflect the real client IP and https:// scheme instead of the proxy's.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=IS_PRODUCTION,  # only send the cookie over https
    PERMANENT_SESSION_LIFETIME=datetime.timedelta(days=7),
)

if not app.debug:
    log_dir = os.path.join(BASE_DIR, "logs")
    os.makedirs(log_dir, exist_ok=True)
    file_handler = RotatingFileHandler(
        os.path.join(log_dir, "app.log"), maxBytes=5 * 1024 * 1024, backupCount=5
    )
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s: %(message)s [%(pathname)s:%(lineno)d]"
    ))
    file_handler.setLevel(logging.INFO)
    app.logger.addHandler(file_handler)
    app.logger.setLevel(logging.INFO)


# --------------------------------------------------------------------------- #
# Login rate limiting - basic brute-force protection. In-memory, so it resets
# on restart and isn't shared across multiple gunicorn workers; good enough
# as a first line of defense, but if you run >1 worker in production and
# want this enforced globally, swap to Flask-Limiter with a shared Redis
# backend instead.
# --------------------------------------------------------------------------- #
_login_attempts = defaultdict(list)
LOGIN_MAX_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 300


def _too_many_login_attempts(ip):
    now = time.time()
    recent = [t for t in _login_attempts[ip] if now - t < LOGIN_WINDOW_SECONDS]
    _login_attempts[ip] = recent
    return len(recent) >= LOGIN_MAX_ATTEMPTS


def _record_failed_login(ip):
    _login_attempts[ip].append(time.time())

db.init_db()


def _auto_migrate_on_startup():
    """
    If the database is empty but the real data files are sitting in data/
    (as they are in this delivery — your actual billdesk.xlsx and
    sales_log.xlsx), load them automatically. The app owns this data; it
    shouldn't need a manual button click before it's usable. The manual
    "Run migration" button in Settings still exists for re-syncing after
    you upload updated files later.

    Production runs multiple worker processes (gunicorn -w N / waitress
    threads), each of which imports this module independently — without a
    guard, every worker would race to run the same migration at once.
    O_CREAT|O_EXCL file creation is atomic at the OS level, so only the
    first worker to reach this wins the race; the rest see FileExistsError
    and skip immediately. (Verified this race is real: running under
    gunicorn -w 2 without this guard, both workers logged conflicting
    in-progress bill counts.)
    """
    if db.bill_count() > 0:
        return
    if not (os.path.exists(BILLDESK_FILE) and os.path.exists(DEFAULT_FILE)):
        return

    lock_path = os.path.join(DATA_DIR, ".migration.lock")
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
    except FileExistsError:
        return  # another worker already claimed the migration (or already ran it)

    try:
        report = migration.run_full_migration(BILLDESK_FILE, DEFAULT_FILE)
        print(f"Auto-migration on startup: {report['catalog']['products']} products, "
              f"{report['catalog']['customers']} customers, {report['catalog']['vendors']} vendors, "
              f"{report['bills']['imported']} bills.")
    except Exception as e:
        print(f"WARNING: auto-migration on startup failed: {e}")


_auto_migrate_on_startup()


@app.context_processor
def inject_user():
    return {"logged_in_user": current_user(), "role_labels": db.ROLE_LABELS}


def get_current_file():
    return app.config.get("CURRENT_FILE", DEFAULT_FILE)


def parse_date_param(s):
    if not s:
        return None
    try:
        return datetime.datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# Auth
# --------------------------------------------------------------------------- #
@app.route("/setup", methods=["GET", "POST"])
def setup():
    if db.any_users_exist():
        flash("Setup already completed - please log in.")
        return redirect(url_for("login"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm", "")
        full_name = request.form.get("full_name", "").strip()

        if not username or not password:
            flash("Username and password are required.")
        elif password != confirm:
            flash("Passwords don't match.")
        elif len(password) < 8:
            flash("Password must be at least 8 characters.")
        else:
            db.create_user(username, password, full_name, role="admin")
            flash("Admin account created - please log in.")
            return redirect(url_for("login"))

    return render_template("setup.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if not db.any_users_exist():
        return redirect(url_for("setup"))

    if request.method == "POST":
        client_ip = request.remote_addr or "unknown"
        if _too_many_login_attempts(client_ip):
            flash("Too many failed login attempts. Please wait a few minutes and try again.")
            return render_template("login.html", next=request.args.get("next", ""))

        username = request.form.get("username", "")
        password = request.form.get("password", "")
        user = db.verify_login(username, password)
        if user:
            session.clear()
            session["user_id"] = user["id"]
            session.permanent = True
            next_url = request.args.get("next") or request.form.get("next")
            return redirect(next_url or url_for("home"))
        _record_failed_login(client_ip)
        flash("Invalid username or password.")

    return render_template("login.html", next=request.args.get("next", ""))


@app.route("/home")
@login_required
def home():
    """
    Role-aware landing page. Every login and every access-denial redirect
    lands here rather than a hardcoded page, so a role that can't see the
    receivables dashboard (purchase, field_staff) doesn't hit a redirect
    loop trying to land on it.
    """
    user = current_user()
    role = user["role"] if user else None
    if role in ("admin", "sales", "accountant"):
        return redirect(url_for("dashboard"))
    if role == "purchase":
        return redirect(url_for("purchases_page"))
    if role == "field_staff":
        return redirect(url_for("money_requests_list"))
    return redirect(url_for("dashboard"))


@app.route("/logout")
def logout():
    session.clear()
    flash("Logged out.")
    return redirect(url_for("login"))


@app.route("/admin/users", methods=["GET", "POST"])
@login_required
@role_required("admin")
def admin_users():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        full_name = request.form.get("full_name", "").strip()
        role = request.form.get("role", "")
        if not username or not password or role not in db.ROLES:
            flash("All fields are required and role must be valid.")
        elif len(password) < 8:
            flash("Password must be at least 8 characters.")
        else:
            try:
                db.create_user(username, password, full_name, role)
                flash(f"User '{username}' created.")
            except ValueError as e:
                flash(str(e))
        return redirect(url_for("admin_users"))

    return render_template("admin_users.html", users=db.list_users(), roles=db.ROLES, role_labels=db.ROLE_LABELS)


@app.route("/admin/users/<int:user_id>/toggle-active", methods=["POST"])
@login_required
@role_required("admin")
def admin_users_toggle_active(user_id):
    user = db.get_user(user_id)
    if user:
        db.set_user_active(user_id, not user["active"])
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/employment", methods=["POST"])
@login_required
@role_required("admin")
def admin_users_employment(user_id):
    joining_date = request.form.get("joining_date", "").strip()
    monthly_salary = request.form.get("monthly_salary", "").strip()
    try:
        salary_value = float(monthly_salary) if monthly_salary else None
    except ValueError:
        flash("Monthly salary must be a number.")
        return redirect(url_for("admin_users"))
    db.set_user_employment_info(user_id, joining_date or None, salary_value)
    flash("Employment info updated.")
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<username>/assignments")
@login_required
@role_required("admin")
def admin_user_assignments(username):
    user = db.get_user_by_username(username)
    if not user or user["role"] != "field_staff":
        flash("That user isn't a Service Manager - assignments only apply to that role.")
        return redirect(url_for("admin_users"))

    assigned_customer_ids = set(db.list_assigned_customer_ids(username))
    assigned_vendor_ids = set(db.list_assigned_vendor_ids(username))
    return render_template(
        "admin_assignments.html", target_user=user,
        customers=db.list_customers(), vendors=db.list_vendors(),
        assigned_customer_ids=assigned_customer_ids, assigned_vendor_ids=assigned_vendor_ids,
    )


@app.route("/admin/users/<username>/assignments/customers", methods=["POST"])
@login_required
@role_required("admin")
def admin_assign_customer(username):
    customer_id = request.form.get("customer_id")
    action = request.form.get("action")
    if customer_id and action == "assign":
        db.assign_customer_to_staff(username, customer_id)
    elif customer_id and action == "unassign":
        db.unassign_customer_from_staff(username, customer_id)
    return redirect(url_for("admin_user_assignments", username=username))


@app.route("/admin/users/<username>/assignments/vendors", methods=["POST"])
@login_required
@role_required("admin")
def admin_assign_vendor(username):
    vendor_id = request.form.get("vendor_id")
    action = request.form.get("action")
    if vendor_id and action == "assign":
        db.assign_vendor_to_staff(username, vendor_id)
    elif vendor_id and action == "unassign":
        db.unassign_vendor_from_staff(username, vendor_id)
    return redirect(url_for("admin_user_assignments", username=username))


@app.route("/salary")
@login_required
@role_required("admin")
def salary_dashboard():
    return render_template("salary.html", employees=db.list_employees_for_salary(),
                            payments=db.list_salary_payments())


@app.route("/salary/<username>/<period>/approve", methods=["POST"])
@login_required
@role_required("admin")
def salary_approve(username, period):
    try:
        amount = float(request.form.get("amount") or 0)
    except ValueError:
        amount = 0
    note = request.form.get("note", "")
    user = current_user()
    try:
        db.approve_salary_payment(username, period, amount, note, user["username"] if user else "")
        flash(f"Salary approved for {username} ({period}): ₹{amount:,.2f}")
    except ValueError as e:
        flash(str(e))
    return redirect(url_for("salary_dashboard"))


@app.route("/salary/<int:payment_id>/mark-paid", methods=["POST"])
@login_required
@role_required("admin")
def salary_mark_paid(payment_id):
    user = current_user()
    try:
        db.mark_salary_paid(payment_id, user["username"] if user else "")
        flash("Marked paid — credited to their cash balance.")
    except ValueError as e:
        flash(str(e))
    return redirect(url_for("salary_dashboard"))


@app.route("/")
@login_required
@role_required("admin", "sales", "accountant")
def dashboard():
    if db.bill_count() == 0:
        return render_template("index.html", customers=[], invoices=[], grand_total=None,
                                error="No bills in the database yet. Run the migration from "
                                      "Settings, or create a bill from New Bill.",
                                filename=None, coordinators=[], group_by_options=[],
                                amount_ranges=AMOUNT_RANGES, filters={}, filters_raw={},
                                view="grouped", metric="margin")

    try:
        parties = db.get_all_parties()
        raw_invoices = bills_repo.load_bills()
    except Exception as e:
        return render_template("index.html", customers=[], invoices=[], grand_total=None,
                                error=f"Could not read bills: {e}",
                                filename=None, coordinators=[],
                                group_by_options=[], amount_ranges=AMOUNT_RANGES,
                                filters={}, filters_raw={}, view="grouped", metric="margin")

    customer_names = sorted({inv["customer"] for inv in raw_invoices})
    db.ensure_parties_exist(customer_names)
    parties = db.get_all_parties()  # re-fetch: ensure_parties_exist may have added rows

    invoices = bills_repo.enrich_bills(raw_invoices, parties)

    coordinators = db.list_coordinators()
    group_by_options = db.list_groups()

    view = request.args.get("view", "grouped")
    if view not in ("grouped", "invoice"):
        view = "grouped"

    metric = request.args.get("metric", "margin")
    if metric not in ("margin", "taxable"):
        metric = "margin"

    filters = {
        "status": request.args.get("status", "unpaid"),
        "date_from": parse_date_param(request.args.get("date_from")),
        "date_to": parse_date_param(request.args.get("date_to")),
        "coordinator": request.args.get("coordinator", "all"),
        "group_by": request.args.get("group_by", "all"),
        "amount_range": request.args.get("amount_range", "all"),
        "q": request.args.get("q", ""),
    }

    filtered = apply_filters(invoices, filters)

    customers = group_by_customer(filtered)
    grand_total = compute_totals(filtered, customer_count=len(customers))

    flat_invoices = sorted(filtered, key=lambda x: (x["customer"], x["original_invoice_id"], x["invoice_id"]))

    return render_template(
        "index.html",
        customers=customers,
        invoices=flat_invoices,
        grand_total=grand_total,
        error=None,
        filename="database",
        coordinators=coordinators,
        group_by_options=group_by_options,
        amount_ranges=AMOUNT_RANGES,
        filters=filters,
        filters_raw={
            "status": request.args.get("status", "unpaid"),
            "date_from": request.args.get("date_from", ""),
            "date_to": request.args.get("date_to", ""),
            "coordinator": request.args.get("coordinator", "all"),
            "group_by": request.args.get("group_by", "all"),
            "amount_range": request.args.get("amount_range", "all"),
            "q": request.args.get("q", ""),
            "metric": metric,
        },
        view=view,
        metric=metric,
        parties=parties,
    )


@app.route("/upload", methods=["POST"])
@login_required
@role_required("admin")
def upload():
    file = request.files.get("file")
    if not file or file.filename == "":
        flash("No file selected.")
        return redirect(url_for("dashboard"))

    if not file.filename.lower().endswith((".xlsx", ".xlsm")):
        flash("Please upload a .xlsx or .xlsm file.")
        return redirect(url_for("dashboard"))

    file.save(DEFAULT_FILE)
    flash(f"Saved {file.filename} as sales_log.xlsx — go to Settings and run the migration to load it into the database.")
    return redirect(url_for("settings"))


@app.route("/invoice/<invoice_id>/status", methods=["POST"])
@login_required
@role_required("admin", "sales", "accountant")
def toggle_invoice_status(invoice_id):
    payload = request.get_json(silent=True) if request.is_json else None
    payload = payload or {}
    new_status = payload.get("status") if request.is_json else request.form.get("status")
    payment_date = payload.get("payment_date") if request.is_json else request.form.get("payment_date")
    pay_from_wallet = bool(payload.get("pay_from_wallet")) if request.is_json else (request.form.get("pay_from_wallet") == "true")

    if new_status not in ("paid", "unpaid"):
        return jsonify({"ok": False, "error": "invalid status"}), 400

    if new_status == "paid":
        payment_date = (payment_date or "").strip() or datetime.date.today().isoformat()
        try:
            datetime.datetime.strptime(payment_date, "%Y-%m-%d")
        except ValueError:
            return jsonify({"ok": False, "error": "invalid payment_date, expected YYYY-MM-DD"}), 400

        if pay_from_wallet:
            bill = db.get_bill_by_invoice(invoice_id)
            if not bill:
                return jsonify({"ok": False, "error": "invoice not found"}), 404
            balance = db.get_wallet_balance(bill["customer_id"])
            if balance < bill["total"]:
                return jsonify({
                    "ok": False,
                    "error": f"insufficient wallet balance (₹{balance:,.2f} available, ₹{bill['total']:,.2f} needed)"
                }), 400
            user = current_user()
            db.add_wallet_entry(
                bill["customer_id"], bill["customer_name"], "debit", bill["total"],
                note=f"Applied to invoice {invoice_id}", created_by=user["username"] if user else "",
            )
    else:
        payment_date = None

    db.set_bill_status(invoice_id, new_status, payment_date)
    return jsonify({"ok": True, "invoice_id": invoice_id, "status": new_status, "payment_date": payment_date,
                     "paid_from_wallet": pay_from_wallet and new_status == "paid"})


def get_all_invoices():
    """Loads + enriches every bill line in the database (no filters applied)."""
    parties = db.get_all_parties()
    return bills_repo.enrich_bills(bills_repo.load_bills(), parties)


@app.route("/invoice/<invoice_id>/lineitems")
@login_required
def invoice_lineitems(invoice_id):
    inv = next((i for i in get_all_invoices() if i["invoice_id"] == invoice_id), None)
    if inv is None:
        return jsonify({"ok": False, "error": "invoice not found"}), 404
    data = invoice_files.parse_invoice_json_for(inv)
    if data is None:
        return jsonify({"ok": False, "error": "not found"}), 404
    return jsonify({"ok": True, "data": data})


@app.route("/invoice/<invoice_id>/pdf")
@login_required
def invoice_pdf(invoice_id):
    inv = next((i for i in get_all_invoices() if i["invoice_id"] == invoice_id), None)
    if inv is None:
        return jsonify({"ok": False, "error": "invoice not found"}), 404
    path = invoice_files.find_pdf_path(inv)
    if not path:
        return jsonify({"ok": False, "error": "not found"}), 404
    return send_file(path, mimetype="application/pdf")


@app.route("/invoices/download", methods=["POST"])
@login_required
@role_required("admin", "sales", "accountant")
def download_invoices():
    payload = request.get_json(silent=True) or {}
    invoice_ids = payload.get("invoice_ids") or []
    include_summary = bool(payload.get("include_summary"))
    metric = payload.get("metric", "margin")
    if metric not in ("margin", "taxable"):
        metric = "margin"

    if not invoice_ids:
        return jsonify({"ok": False, "error": "no invoices selected"}), 400

    all_invoices = get_all_invoices()
    if not all_invoices:
        return jsonify({"ok": False, "error": "no data file loaded"}), 400
    by_id = {inv["invoice_id"]: inv for inv in all_invoices}

    selected = [by_id[i] for i in invoice_ids if i in by_id]
    if not selected:
        return jsonify({"ok": False, "error": "selected invoices not found"}), 400

    # One PDF per selected invoice LINE now (split legs each have their own
    # real PDF in record_room), deduplicated only for the (rare) case the
    # same line id appears twice in the request.
    pdf_by_id = {}
    missing_ids = []
    for inv in selected:
        if inv["invoice_id"] in pdf_by_id or inv["invoice_id"] in missing_ids:
            continue
        path = invoice_files.find_pdf_path(inv)
        if path:
            pdf_by_id[inv["invoice_id"]] = path
        else:
            missing_ids.append(inv["invoice_id"])

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for invoice_id, path in pdf_by_id.items():
            zf.write(path, arcname=os.path.basename(path))
        if include_summary:
            zf.writestr("summary.pdf", build_summary_pdf(selected, metric))
        if missing_ids:
            zf.writestr(
                "MISSING_PDFS.txt",
                "PDF not found for these invoices:\n" + "\n".join(missing_ids),
            )

    buf.seek(0)
    return send_file(
        buf, mimetype="application/zip", as_attachment=True, download_name="invoices.zip"
    )


@app.route("/settings")
@login_required
@role_required("admin", "sales")
def settings():
    customer_names = []
    try:
        customer_names = sorted({inv["customer"] for inv in bills_repo.load_bills()})
        db.ensure_parties_exist(customer_names)
    except Exception:
        pass

    parties = db.get_all_parties()
    coordinators = db.list_coordinators()
    groups = db.list_groups()
    db_stats = {
        "bills": db.bill_count(),
        "products": db.product_count(),
        "customers": len(db.list_customers()),
        "vendors": len(db.list_vendors()),
    }
    business_profile = db.get_business_profile()
    return render_template("settings.html", parties=parties, coordinators=coordinators,
                            groups=groups, db_stats=db_stats,
                            billdesk_exists=os.path.exists(BILLDESK_FILE),
                            sales_log_exists=os.path.exists(DEFAULT_FILE),
                            money_request_reasons=db.list_money_request_reasons(),
                            customer_categories=db.list_customer_category_options(),
                            vendor_categories=db.list_vendor_category_options(),
                            business_profile=business_profile)


@app.route("/settings/business-profile", methods=["POST"])
@login_required
@role_required("admin")
def settings_save_business_profile():
    data = {}
    for key in db.BUSINESS_PROFILE_KEYS:
        data[key] = request.form.get(key, "")
    db.set_business_profile(data)
    flash("Business profile saved — PDF invoices will now use these details.")
    return redirect(url_for("settings"))


@app.route("/settings/money-request-reasons", methods=["POST"])
@login_required
@role_required("admin")
def settings_add_reason():
    label = (request.form.get("label") or "").strip()
    if not label:
        flash("Reason label can't be empty.")
    else:
        try:
            db.add_money_request_reason(label)
            flash(f"Added reason: {label}")
        except ValueError as e:
            flash(str(e))
    return redirect(url_for("settings"))


@app.route("/settings/money-request-reasons/<int:reason_id>/delete", methods=["POST"])
@login_required
@role_required("admin")
def settings_delete_reason(reason_id):
    db.delete_money_request_reason(reason_id)
    flash("Reason removed.")
    return redirect(url_for("settings"))


@app.route("/settings/customer-categories", methods=["POST"])
@login_required
@role_required("admin")
def settings_add_customer_category():
    try:
        db.add_customer_category_option(request.form.get("label", ""))
        flash(f"Added customer category: {request.form.get('label')}")
    except ValueError as e:
        flash(str(e))
    return redirect(url_for("settings"))


@app.route("/settings/customer-categories/<int:category_id>/delete", methods=["POST"])
@login_required
@role_required("admin")
def settings_delete_customer_category(category_id):
    db.delete_customer_category_option(category_id)
    flash("Customer category removed.")
    return redirect(url_for("settings"))


@app.route("/settings/vendor-categories", methods=["POST"])
@login_required
@role_required("admin")
def settings_add_vendor_category():
    try:
        db.add_vendor_category_option(request.form.get("label", ""))
        flash(f"Added vendor category: {request.form.get('label')}")
    except ValueError as e:
        flash(str(e))
    return redirect(url_for("settings"))


@app.route("/settings/vendor-categories/<int:category_id>/delete", methods=["POST"])
@login_required
@role_required("admin")
def settings_delete_vendor_category(category_id):
    db.delete_vendor_category_option(category_id)
    flash("Vendor category removed.")
    return redirect(url_for("settings"))


@app.route("/settings/custom-fields")
@login_required
@role_required("admin")
def settings_custom_fields():
    entity_type = request.args.get("type", "product")
    if entity_type not in db.CUSTOM_FIELD_ENTITY_TYPES:
        entity_type = "product"
    return render_template(
        "custom_fields.html", entity_type=entity_type,
        fields=db.list_custom_fields(entity_type),
        entity_types=db.CUSTOM_FIELD_ENTITY_TYPES,
        field_types=db.CUSTOM_FIELD_TYPES,
    )


@app.route("/settings/custom-fields/add", methods=["POST"])
@login_required
@role_required("admin")
def settings_custom_fields_add():
    entity_type = request.form.get("entity_type", "product")
    label = request.form.get("label", "")
    field_type = request.form.get("field_type", "text")
    try:
        db.add_custom_field(entity_type, label, field_type)
        flash(f"Added field: {label}")
    except ValueError as e:
        flash(str(e))
    return redirect(url_for("settings_custom_fields", type=entity_type))


@app.route("/settings/custom-fields/<int:field_id>/delete", methods=["POST"])
@login_required
@role_required("admin")
def settings_custom_fields_delete(field_id):
    entity_type = request.form.get("entity_type", "product")
    db.delete_custom_field(field_id)
    flash("Field removed (and any values stored for it).")
    return redirect(url_for("settings_custom_fields", type=entity_type))


@app.route("/settings/migrate", methods=["POST"])
@login_required
@role_required("admin")
def settings_migrate():
    if not os.path.exists(BILLDESK_FILE):
        flash("Upload billdesk.xlsx first (via New Bill's catalog upload, or Load sheet below).")
        return redirect(url_for("settings"))
    if not os.path.exists(DEFAULT_FILE):
        flash("No sales_log.xlsx found to migrate bills from.")
        return redirect(url_for("settings"))

    report = migration.run_full_migration(BILLDESK_FILE, DEFAULT_FILE)
    msg = (f"Migrated: {report['catalog']['products']} products, "
           f"{report['catalog']['customers']} customers, {report['catalog']['vendors']} vendors, "
           f"{report['bills']['imported']} bills imported "
           f"({report['bills']['skipped_existing']} already present).")
    if report["bills"]["duplicates_skipped"]:
        msg += f" {len(report['bills']['duplicates_skipped'])} duplicate invoice/date rows in the sheet were skipped (kept the latest)."
    if report["catalog"]["errors"] or report["bills"]["errors"]:
        msg += f" Errors: {report['catalog']['errors'] + report['bills']['errors']}"
    flash(msg)
    return redirect(url_for("settings"))


@app.route("/settings/party", methods=["POST"])
@login_required
@role_required("admin", "sales")
def settings_party():
    name = request.form.get("name", "")
    contact_number = request.form.get("contact_number", "")
    group_by = request.form.get("group_by", "")
    location = request.form.get("location", "")
    coordinator = request.form.get("coordinator", "")
    db.upsert_party(name, contact_number, group_by, location, coordinator)
    flash(f"Saved party details for {name}.")
    return redirect(url_for("settings"))


@app.route("/settings/coordinator", methods=["POST"])
@login_required
@role_required("admin", "sales")
def settings_coordinator():
    name = request.form.get("coordinator_name", "")
    if name.strip():
        db.add_coordinator(name)
        flash(f"Added coordinator {name}.")
    return redirect(url_for("settings"))


@app.route("/settings/group", methods=["POST"])
@login_required
@role_required("admin", "sales")
def settings_group():
    name = request.form.get("group_name", "")
    if name.strip():
        db.add_group(name)
        flash(f"Added group {name}.")
    return redirect(url_for("settings"))


@app.route("/bills")
@login_required
@role_required("admin", "sales")
def bills_page():
    catalog_loaded = db.product_count() > 0
    return render_template("bills.html", billdesk_exists=catalog_loaded)


@app.route("/bills/upload-catalog", methods=["POST"])
@login_required
@role_required("admin")
def bills_upload_catalog():
    file = request.files.get("file")
    if not file or file.filename == "":
        flash("No file selected.")
        return redirect(url_for("bills_page"))
    if not file.filename.lower().endswith((".xlsx", ".xlsm")):
        flash("Please upload a .xlsx or .xlsm file.")
        return redirect(url_for("bills_page"))
    file.save(BILLDESK_FILE)
    report = migration.migrate_products_and_parties(BILLDESK_FILE)
    if report["errors"]:
        flash(f"Loaded with errors: {'; '.join(report['errors'])}")
    else:
        flash(f"Loaded stock catalog: {report['products']} products, "
              f"{report['customers']} customers, {report['vendors']} vendors.")
    return redirect(url_for("bills_page"))


@app.route("/bills/api/next-invoice-number")
@login_required
@role_required("admin", "sales")
def bills_api_next_invoice_number():
    return jsonify({"ok": True, "suggested": db.next_invoice_number_db()})


@app.route("/bills/api/invoice-availability/<invoice_no>")
@login_required
@role_required("admin", "sales")
def bills_api_invoice_availability(invoice_no):
    existing = db.get_bill_by_invoice(invoice_no)
    return jsonify({"ok": True, "available": existing is None})


@app.route("/bills/api/customer-prices/<customer_id>")
@login_required
@role_required("admin", "sales", "field_staff")
def bills_api_customer_prices(customer_id):
    """Every product this customer has a recorded last price for - used to
    show a recommended price (accept or override) instead of always
    defaulting to the flat catalog price. A product not in this map means
    the customer has never bought it before, so the UI shows the plain
    catalog price with no history-based suggestion."""
    conn = db.get_conn()
    rows = conn.execute(
        "SELECT product_id, last_price FROM customer_product_prices WHERE customer_id = ?", (customer_id,)
    ).fetchall()
    conn.close()
    return jsonify({"ok": True, "prices": {r["product_id"]: r["last_price"] for r in rows}})


@app.route("/bills/api/customers")
@login_required
@role_required("admin", "sales", "field_staff")
def bills_api_customers():
    user = current_user()
    customers = db.list_customers_for_user(user["username"], user["role"])
    custom_values = db.get_custom_field_values_bulk("customer", [c["customer_id"] for c in customers])
    return jsonify({"ok": True, "customers": [
        {"customer_id": c["customer_id"], "name": c["name"], "contact_number": c["contact_number"],
         "address_details": c["address_details"], "gstn": c["gstn"], "category": c["category"],
         "custom_fields": custom_values.get(c["customer_id"], {})}
        for c in customers
    ]})


@app.route("/bills/api/products")
@login_required
@role_required("admin", "sales", "field_staff")
def bills_api_products():
    user = current_user()
    products = db.list_products_for_user(user["username"])
    custom_values = db.get_custom_field_values_bulk("product", [p["id"] for p in products])
    return jsonify({"ok": True, "products": [
        {"id": p["id"], "sheet": p["sheet"], "item_code": p["item_code"], "item_name": p["item_name"],
         "item_description": p["item_description"], "hsn": p["hsn"], "mrp": p["mrp"],
         "quantity": p["quantity"], "gst_pct": p["gst_pct"], "cost_price": p["cost_price"],
         "sale_price": p["sale_price"], "custom_fields": custom_values.get(str(p["id"]), {}),
         "approved": bool(p["approved"])}
        for p in products
    ]})


@app.route("/bills/api/new", methods=["POST"])
@login_required
@role_required("admin", "sales")
def bills_api_new():
    if db.product_count() == 0:
        return jsonify({"ok": False, "error": "no stock catalog loaded"}), 400

    payload = request.get_json(silent=True) or {}
    customer_id = payload.get("customer_id")
    bill_type = payload.get("bill_type", "Tax Invoice")
    date_str = payload.get("date")  # dd/mm/yyyy
    manual_invoice_no = (payload.get("invoice_no") or "").strip()
    raw_lines = payload.get("lines") or []

    if not customer_id:
        return jsonify({"ok": False, "error": "customer is required"}), 400
    if not raw_lines:
        return jsonify({"ok": False, "error": "at least one line item is required"}), 400
    if not date_str:
        date_str = datetime.date.today().strftime("%d/%m/%Y")

    try:
        datetime.datetime.strptime(date_str, "%d/%m/%Y")
    except ValueError:
        return jsonify({"ok": False, "error": "date must be dd/mm/yyyy"}), 400

    customer = db.get_customer(customer_id)
    if not customer:
        return jsonify({"ok": False, "error": "customer not found"}), 400

    if manual_invoice_no:
        if db.get_bill_by_invoice(manual_invoice_no):
            return jsonify({"ok": False, "error": f"invoice number '{manual_invoice_no}' already exists"}), 400
        invoice_no = manual_invoice_no
    else:
        invoice_no = db.next_invoice_number_db()

    build_lines = []
    for rl in raw_lines:
        product = db.get_product(rl.get("id"))
        if not product:
            return jsonify({"ok": False, "error": f"product not found: id {rl.get('id')}"}), 400
        qty = rl.get("qty")
        try:
            qty = float(qty)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "invalid quantity"}), 400
        if qty <= 0:
            return jsonify({"ok": False, "error": "quantity must be positive"}), 400
        item = dict(product)
        item["qty"] = qty
        item["product_id"] = product["id"]
        # Optional per-line rate override (customer-specific recommended
        # price, accepted or overridden by whoever's creating the bill) -
        # falls back to the catalog sale_price if not provided.
        override_rate = rl.get("rate")
        if override_rate is not None:
            try:
                override_rate = float(override_rate)
                if override_rate > 0:
                    item["sale_price"] = override_rate
            except (TypeError, ValueError):
                pass
        build_lines.append(item)

    try:
        lines, cost_map = billing_engine.build_bill_lines(build_lines, bill_type)
        final_data = billing_engine.build_final_data(
            {"customer_id": customer["customer_id"], "name": customer["name"],
             "contact_number": customer["contact_number"], "address_details": customer["address_details"],
             "gstn": customer["gstn"]},
            lines, invoice_no, date_str, bill_type,
        )
        margin_total = billing_engine.compute_margin(lines, cost_map)
        hsn_summary = billing_engine.compute_hsn_summary(lines)

        billing_engine.write_record_room_json(final_data)
        billing_engine.write_cost_report(final_data, cost_map)

        pdf_bytes = bill_pdf.build_invoice_pdf(final_data, lines, hsn_summary)
        folder = rrl.local_folder_name(final_data["Date"])
        pdf_path = os.path.join(BASE_DIR, "record_room", folder, final_data["fileName"] + ".pdf")
        with open(pdf_path, "wb") as f:
            f.write(pdf_bytes)

        stock_warnings = []
        for line in lines:
            new_qty = db.decrement_product_stock(line["product_id"], line["qty"])
            if new_qty is not None and new_qty < 0:
                stock_warnings.append(f"{line['description']}: stock now {new_qty:g} (oversold)")

        user = current_user()
        bill_id = db.insert_bill(
            file_name=final_data["fileName"],
            invoice_no=final_data["Invoice"],
            original_invoice_no=final_data["Invoice"],
            split_leg="",
            bill_type=bill_type,
            date=final_data["Date"],
            customer_id=customer["customer_id"],
            customer_name=customer["name"],
            total=final_data["total"],
            margin=margin_total,
            taxable_total=final_data.get("ttaxamt", 0),
            status="unpaid",
            payment_date=None,
            is_candidate=True,
            created_by=user["username"] if user else "",
            lines=lines,
        )
        # Remember what this customer was actually charged per item, so
        # next time it's suggested as a recommended price instead of
        # defaulting back to the flat catalog price.
        for line in lines:
            if line.get("product_id"):
                db.set_customer_product_price(customer["customer_id"], line["product_id"], line["rate"])
    except Exception as e:
        return jsonify({"ok": False, "error": f"failed to create bill: {e}"}), 500

    return jsonify({
        "ok": True,
        "invoice_no": invoice_no,
        "file_name": final_data["fileName"],
        "total": final_data["total"],
        "margin": margin_total,
        "bill_id": bill_id,
        "stock_warnings": stock_warnings,
        "pdf_url": url_for("bills_pdf", file_name=final_data["fileName"]),
    })


@app.route("/bills/list")
@login_required
@role_required("admin", "sales", "accountant")
def bills_list():
    q = request.args.get("q", "").strip().lower()
    status = request.args.get("status", "all")

    bills = db.list_bills()
    if status != "all":
        bills = [b for b in bills if b["status"] == status]
    if q:
        bills = [b for b in bills if q in b["invoice_no"].lower() or q in (b["customer_name"] or "").lower()]
    bills.sort(key=lambda b: b["id"], reverse=True)

    total_count = len(bills)
    bills = bills[:500]  # cap render size; search/filter narrows it down

    return render_template("bills_list.html", bills=bills, q=request.args.get("q", ""),
                            status=status, total_count=total_count, shown_count=len(bills))


@app.route("/bills/delete/<invoice_no>", methods=["POST"])
@login_required
@role_required("admin", "sales")
def bills_delete(invoice_no):
    bill = db.get_bill_by_invoice(invoice_no)
    if not bill:
        flash(f"Bill {invoice_no} not found.")
        return redirect(url_for("bills_list"))

    lines = db.get_bill_lines(bill["id"])
    restored = 0
    for line in lines:
        if line["product_id"]:
            db.adjust_product_stock(line["product_id"], line["qty"])
            restored += 1

    db.soft_delete_bill(invoice_no)
    if restored:
        flash(f"Deleted {invoice_no} and restored stock for {restored} line item(s).")
    else:
        flash(f"Deleted {invoice_no}. (No linked line items to restore stock for — "
              f"this bill predates item-level tracking.)")
    return redirect(url_for("bills_list"))


@app.route("/bills/edit/<invoice_no>", methods=["GET", "POST"])
@login_required
@role_required("admin", "sales")
def bills_edit(invoice_no):
    bill = db.get_bill_by_invoice(invoice_no)
    if not bill:
        flash(f"Bill {invoice_no} not found.")
        return redirect(url_for("bills_list"))

    existing_lines = db.get_bill_lines(bill["id"])
    editable = len(existing_lines) > 0 and all(l["product_id"] for l in existing_lines)

    if request.method == "POST":
        if not editable:
            return jsonify({"ok": False, "error": "this bill predates item-level tracking and can't be "
                                                    "recomputed here — only bills created in this app can be edited"}), 400

        payload = request.get_json(silent=True) or {}
        raw_lines = payload.get("lines") or []
        bill_type = payload.get("bill_type", bill["bill_type"])
        if not raw_lines:
            return jsonify({"ok": False, "error": "at least one line item is required"}), 400

        # Date is intentionally not editable here: record_room files are
        # keyed by date, so changing it would relocate/orphan files. Delete
        # and recreate the bill if the date itself was wrong.
        date_str = bill["date"]

        old_qty_by_product = {}
        for l in existing_lines:
            old_qty_by_product[l["product_id"]] = old_qty_by_product.get(l["product_id"], 0) + l["qty"]

        build_lines = []
        for rl in raw_lines:
            product = db.get_product(rl.get("id"))
            if not product:
                return jsonify({"ok": False, "error": f"product not found: id {rl.get('id')}"}), 400
            try:
                qty = float(rl.get("qty"))
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "invalid quantity"}), 400
            if qty <= 0:
                return jsonify({"ok": False, "error": "quantity must be positive"}), 400
            item = dict(product)
            item["qty"] = qty
            item["product_id"] = product["id"]
            build_lines.append(item)

        try:
            lines, cost_map = billing_engine.build_bill_lines(build_lines, bill_type)
            customer = db.get_customer(bill["customer_id"]) or {
                "customer_id": bill["customer_id"], "name": bill["customer_name"],
                "contact_number": "", "address_details": "", "gstn": "",
            }
            final_data = billing_engine.build_final_data(customer, lines, bill["invoice_no"], date_str, bill_type)
            margin_total = billing_engine.compute_margin(lines, cost_map)
            hsn_summary = billing_engine.compute_hsn_summary(lines)

            billing_engine.write_record_room_json(final_data)
            billing_engine.write_cost_report(final_data, cost_map)

            pdf_bytes = bill_pdf.build_invoice_pdf(final_data, lines, hsn_summary)
            folder = rrl.local_folder_name(final_data["Date"])
            pdf_path = os.path.join(BASE_DIR, "record_room", folder, final_data["fileName"] + ".pdf")
            with open(pdf_path, "wb") as f:
                f.write(pdf_bytes)

            # Reverse the old stock impact, then apply the new one.
            for product_id, qty in old_qty_by_product.items():
                db.adjust_product_stock(product_id, qty)
            stock_warnings = []
            for line in lines:
                new_qty = db.decrement_product_stock(line["product_id"], line["qty"])
                if new_qty is not None and new_qty < 0:
                    stock_warnings.append(f"{line['description']}: stock now {new_qty:g} (oversold)")

            db.update_bill_totals(bill["id"], final_data["total"], margin_total, final_data.get("ttaxamt", 0))
            db.update_bill_type(bill["id"], bill_type)
            db.replace_bill_lines(bill["id"], lines)
        except Exception as e:
            return jsonify({"ok": False, "error": f"failed to update bill: {e}"}), 500

        return jsonify({
            "ok": True, "total": final_data["total"], "margin": margin_total,
            "stock_warnings": stock_warnings, "pdf_url": url_for("bills_pdf", file_name=final_data["fileName"]),
        })

    return render_template("bills_edit.html", bill=bill, existing_lines=existing_lines, editable=editable)


@app.route("/bills/split/<invoice_no>", methods=["GET", "POST"])
@login_required
@role_required("admin", "sales")
def bills_split(invoice_no):
    bill = db.get_bill_by_invoice(invoice_no)
    if not bill:
        flash(f"Bill {invoice_no} not found.")
        return redirect(url_for("bills_list"))
    if bill["split_leg"]:
        flash("This is already a split part — split the original invoice instead.")
        return redirect(url_for("bills_list"))

    existing_lines = db.get_bill_lines(bill["id"])
    item_level = len(existing_lines) > 0 and all(l["product_id"] for l in existing_lines)

    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        user = current_user()

        if item_level:
            # Real per-item split: the user assigns each line to a part
            # (matching how mtsBills.py's get_bucket_store actually works -
            # it's not an optimizer, it reads a manual per-line letter
            # assignment; this is that same assignment as a dropdown instead
            # of typing into an Excel column). Each part's tax/margin is
            # computed for real from its assigned items via the same engine
            # New Bill uses, not estimated proportionally.
            assignments = payload.get("assignments") or {}
            by_letter = {}
            for line in existing_lines:
                letter = assignments.get(str(line["id"]))
                if not letter:
                    return jsonify({"ok": False, "error": f"'{line['description']}' isn't assigned to a part"}), 400
                by_letter.setdefault(letter, []).append(line)

            if len(by_letter) < 2:
                return jsonify({"ok": False, "error": "need at least 2 parts"}), 400
            if len(by_letter) > 26:
                return jsonify({"ok": False, "error": "maximum 26 parts"}), 400
            for letter, lines_in_part in by_letter.items():
                if not lines_in_part:
                    return jsonify({"ok": False, "error": f"part '{letter}' has no items assigned"}), 400

            created = []
            try:
                for letter in sorted(by_letter.keys()):
                    part_lines = []
                    cost_map = {}
                    for l in by_letter[letter]:
                        part_lines.append({
                            "description": l["description"], "hsn": l["hsn"], "mrp": l["mrp"],
                            "qty": l["qty"], "gst_pct": l["gst_pct"], "rate": l["rate"],
                            "taxable_rate": l["taxable_rate"], "amount": l["amount"],
                            "product_id": l["product_id"],
                        })
                        product = db.get_product(l["product_id"])
                        cost_map[l["description"]] = float(product["cost_price"]) if product else 0.0

                    part_margin = billing_engine.compute_margin(part_lines, cost_map)
                    hsn_summary = billing_engine.compute_hsn_summary(part_lines)
                    leg_invoice_no = f"{bill['invoice_no']}{letter}"
                    file_name = f"{leg_invoice_no}_{bill['date'].replace('/', '')}"

                    customer = db.get_customer(bill["customer_id"]) or {
                        "customer_id": bill["customer_id"], "name": bill["customer_name"],
                        "contact_number": "", "address_details": "", "gstn": "",
                    }
                    final_data = billing_engine.build_final_data(
                        customer, part_lines, leg_invoice_no, bill["date"], bill["bill_type"])

                    billing_engine.write_record_room_json(final_data)
                    billing_engine.write_cost_report(final_data, cost_map)
                    pdf_bytes = bill_pdf.build_invoice_pdf(final_data, part_lines, hsn_summary)
                    folder = rrl.local_folder_name(final_data["Date"])
                    pdf_path = os.path.join(BASE_DIR, "record_room", folder, final_data["fileName"] + ".pdf")
                    with open(pdf_path, "wb") as f:
                        f.write(pdf_bytes)

                    # Splitting doesn't move stock again - the original sale
                    # already decremented it once; this only restructures the
                    # invoicing/accounting, not the inventory transaction.
                    db.insert_bill(
                        file_name=file_name, invoice_no=leg_invoice_no, original_invoice_no=bill["invoice_no"],
                        split_leg=letter, bill_type=bill["bill_type"], date=bill["date"],
                        customer_id=bill["customer_id"], customer_name=bill["customer_name"],
                        total=final_data["total"], margin=part_margin, taxable_total=final_data.get("ttaxamt", 0),
                        status="unpaid", payment_date=None, is_candidate=True,
                        created_by=user["username"] if user else "", lines=part_lines,
                    )
                    created.append(leg_invoice_no)

                db.soft_delete_bill(bill["invoice_no"])
            except Exception as e:
                return jsonify({"ok": False, "error": f"split failed: {e}"}), 500

            return jsonify({"ok": True, "created": created})

        # Legacy fallback: bills with no (or incomplete) product-linked line
        # items - migrated bills mostly - can't be split item-by-item since
        # we don't know what was actually on them. Same proportional-amount
        # divider as before.
        raw_parts = payload.get("parts") or []
        if len(raw_parts) < 2:
            return jsonify({"ok": False, "error": "need at least 2 parts to split"}), 400
        if len(raw_parts) > 26:
            return jsonify({"ok": False, "error": "maximum 26 parts"}), 400
        try:
            parts = [round(float(p), 2) for p in raw_parts]
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "invalid part amount"}), 400
        if any(p <= 0 for p in parts):
            return jsonify({"ok": False, "error": "each part must be a positive amount"}), 400
        parts_sum = round(sum(parts), 2)
        if abs(parts_sum - bill["total"]) > 0.02:
            return jsonify({"ok": False, "error": f"parts sum to {parts_sum}, must equal the bill "
                                                    f"total {bill['total']}"}), 400

        created = []
        try:
            for i, part_amount in enumerate(parts):
                letter = string.ascii_lowercase[i]
                ratio = (part_amount / bill["total"]) if bill["total"] else 0
                part_margin = round(bill["margin"] * ratio, 2)
                part_taxable = round(bill["taxable_total"] * ratio, 2)
                leg_invoice_no = f"{bill['invoice_no']}{letter}"
                file_name = f"{leg_invoice_no}_{bill['date'].replace('/', '')}"

                cgst_sgst_each = round((part_amount - part_taxable) / 2, 2) if part_amount > part_taxable else 0
                final_data = {
                    "customerGSTN": "", "customerID": bill["customer_id"], "customerName": bill["customer_name"],
                    "customerContNum": "", "CustomerDetails": "",
                    "Date": bill["date"], "Invoice": leg_invoice_no,
                    "itema": f"Split part {letter.upper()} of {bill['invoice_no']}", "hsa": "", "ma": "",
                    "qa": 1, "ga": 0, "ra": part_amount, "wa": part_taxable, "aa": part_taxable,
                    "ttaxamt": part_taxable, "cgst": cgst_sgst_each, "sgst": cgst_sgst_each,
                    "total": part_amount, "roff": "NA",
                    "finalAmountWord": billing_engine.amount_to_words_inr(part_amount),
                    "hsn": "", "gsh": "", "hsnc_tax": "",
                    "fileName": file_name,
                }
                billing_engine.write_record_room_json(final_data)
                pdf_bytes = bill_pdf.build_invoice_pdf(
                    final_data,
                    [{"description": final_data["itema"], "hsn": "", "qty": 1, "mrp": "",
                      "gst_pct": 0, "rate": part_amount, "amount": part_amount}],
                    [],
                )
                folder = rrl.local_folder_name(final_data["Date"])
                pdf_path = os.path.join(BASE_DIR, "record_room", folder, file_name + ".pdf")
                with open(pdf_path, "wb") as f:
                    f.write(pdf_bytes)

                db.insert_bill(
                    file_name=file_name, invoice_no=leg_invoice_no, original_invoice_no=bill["invoice_no"],
                    split_leg=letter, bill_type=bill["bill_type"], date=bill["date"],
                    customer_id=bill["customer_id"], customer_name=bill["customer_name"],
                    total=part_amount, margin=part_margin, taxable_total=part_taxable,
                    status="unpaid", payment_date=None, is_candidate=True,
                    created_by=user["username"] if user else "", lines=[],
                )
                created.append(leg_invoice_no)

            db.soft_delete_bill(bill["invoice_no"])
        except Exception as e:
            return jsonify({"ok": False, "error": f"split failed: {e}"}), 500

        return jsonify({"ok": True, "created": created})

    return render_template("bills_split.html", bill=bill, existing_lines=existing_lines, item_level=item_level)


@app.route("/bills/pdf/<file_name>")
@login_required
@role_required("admin", "sales")
def bills_pdf(file_name):
    # file_name is "{Invoice}_{ddmmyyyy}" - derive the mm_dd_yyyy folder from
    # the trailing ddmmyyyy segment.
    if "_" not in file_name or not re.match(r"^.+_\d{8}$", file_name):
        return jsonify({"ok": False, "error": "invalid file name"}), 400
    ddmmyyyy = file_name[-8:]
    dd, mm, yyyy = ddmmyyyy[:2], ddmmyyyy[2:4], ddmmyyyy[4:]
    folder = f"{mm}_{dd}_{yyyy}"
    path = rrl.resolve_case_insensitive(
        os.path.join(BASE_DIR, "record_room", folder, file_name + ".pdf")
    )
    if not os.path.exists(path):
        return jsonify({"ok": False, "error": "not found"}), 404
    return send_file(path, mimetype="application/pdf")


@app.route("/wallet/api/balance/<customer_id>")
@login_required
@role_required("admin", "sales", "accountant")
def wallet_api_balance(customer_id):
    return jsonify({"ok": True, "balance": db.get_wallet_balance(customer_id)})


@app.route("/parties/vendors/api/wallet-balance/<vendor_id>")
@login_required
@role_required("admin", "purchase", "field_staff")
def vendor_wallet_api_balance(vendor_id):
    return jsonify({"ok": True, "balance": db.get_vendor_wallet_balance(vendor_id)})


@app.route("/wallet")
@login_required
@role_required("admin", "sales", "accountant")
def wallet_page():
    q = request.args.get("customer_id", "").strip()
    history = db.get_wallet_history(q) if q else []
    balance = db.get_wallet_balance(q) if q else None

    customers = db.list_customers()

    return render_template(
        "wallet.html",
        customers=customers,
        selected_customer_id=q,
        balance=balance,
        history=history,
        all_balances=db.list_all_wallet_balances(),
    )


@app.route("/wallet/entry", methods=["POST"])
@login_required
@role_required("admin", "sales", "accountant")
def wallet_add_entry():
    customer_id = request.form.get("customer_id", "").strip()
    customer_name = request.form.get("customer_name", "").strip()
    entry_type = request.form.get("entry_type", "")
    amount = request.form.get("amount", "")
    note = request.form.get("note", "")

    if not customer_id:
        flash("Select a customer first.")
        return redirect(url_for("wallet_page"))
    try:
        user = current_user()
        db.add_wallet_entry(customer_id, customer_name, entry_type, amount, note,
                             created_by=user["username"] if user else "")
        flash("Wallet entry recorded.")
    except ValueError as e:
        flash(str(e))
    return redirect(url_for("wallet_page", customer_id=customer_id))


@app.route("/parties/customers/api/categories")
@login_required
@role_required("admin", "sales", "purchase", "field_staff", "accountant")
def customer_categories_api():
    return jsonify({"ok": True, "categories": [c["label"] for c in db.list_customer_category_options()]})


@app.route("/parties/vendors/api/categories")
@login_required
@role_required("admin", "sales", "purchase", "field_staff", "accountant")
def vendor_categories_api():
    return jsonify({"ok": True, "categories": [c["label"] for c in db.list_vendor_category_options()]})


@app.route("/parties")
@login_required
@role_required("admin", "sales", "purchase")
def parties_page():
    party_type = request.args.get("type", "customer")
    if party_type not in ("customer", "vendor"):
        party_type = "customer"
    q = request.args.get("q", "").strip().lower()
    category = request.args.get("category", "all")

    if party_type == "customer":
        items = db.list_customers()
        categories = [c["label"] for c in db.list_customer_category_options()]
    else:
        items = db.list_vendors()
        categories = [c["label"] for c in db.list_vendor_category_options()]

    if category != "all":
        items = [i for i in items if (i["category"] or "").lower() == category.lower()]
    if q:
        id_key = "customer_id" if party_type == "customer" else "vendor_id"
        items = [i for i in items if q in i["name"].lower() or q in i[id_key].lower()]

    id_key = "customer_id" if party_type == "customer" else "vendor_id"
    custom_fields = db.list_custom_fields(party_type)
    custom_values = db.get_custom_field_values_bulk(party_type, [i[id_key] for i in items])

    return render_template("parties.html", party_type=party_type, items=items,
                            custom_fields=custom_fields, custom_values=custom_values,
                            categories=categories, q=request.args.get("q", ""), category=category)


@app.route("/parties/customers/<customer_id>/ledger")
@login_required
@role_required("admin", "sales", "accountant")
def customer_ledger(customer_id):
    customer = db.get_customer(customer_id)
    if not customer:
        flash(f"Customer {customer_id} not found.")
        return redirect(url_for("parties_page", type="customer"))

    bills = db.list_bills_for_customer(customer_id)
    wallet_history = db.get_wallet_history(customer_id)

    timeline = []
    for b in bills:
        timeline.append({
            "date": b["date"], "type": "Bill", "reference": b["invoice_no"],
            "amount": b["total"], "direction": "owed", "status": b["status"],
            "sort_key": b["id"],
        })
    for w in wallet_history:
        timeline.append({
            "date": w["created_at"][:10], "type": f"Wallet {w['entry_type']}", "reference": w["note"],
            "amount": w["amount"], "direction": w["entry_type"], "status": "",
            "sort_key": w["created_at"],
        })
    timeline.sort(key=lambda t: str(t["sort_key"]), reverse=True)

    total_billed = sum(b["total"] for b in bills)
    total_unpaid = sum(b["total"] for b in bills if b["status"] == "unpaid")
    balance = db.get_wallet_balance(customer_id)

    return render_template("party_ledger.html", party_type="customer", party=customer,
                            timeline=timeline, balance=balance,
                            total_billed=total_billed, total_unpaid=total_unpaid)


@app.route("/parties/vendors/<vendor_id>/ledger")
@login_required
@role_required("admin", "purchase", "accountant")
def vendor_ledger(vendor_id):
    vendor = db.get_vendor(vendor_id)
    if not vendor:
        flash(f"Vendor {vendor_id} not found.")
        return redirect(url_for("parties_page", type="vendor"))

    purchases = db.list_purchase_bills_for_vendor(vendor_id)
    wallet_history = db.get_vendor_wallet_history(vendor_id)

    timeline = []
    for p in purchases:
        timeline.append({
            "date": p["date"], "type": "Purchase", "reference": p["purchase_no"],
            "amount": p["total"], "direction": "owed", "status": p["status"],
            "sort_key": p["id"],
        })
    for w in wallet_history:
        timeline.append({
            "date": w["created_at"][:10], "type": f"Wallet {w['entry_type']}", "reference": w["note"],
            "amount": w["amount"], "direction": w["entry_type"], "status": "",
            "sort_key": w["created_at"],
        })
    timeline.sort(key=lambda t: str(t["sort_key"]), reverse=True)

    total_purchased = sum(p["total"] for p in purchases)
    total_unpaid = sum(p["total"] for p in purchases if p["status"] == "unpaid")
    balance = db.get_vendor_wallet_balance(vendor_id)

    return render_template("party_ledger.html", party_type="vendor", party=vendor,
                            timeline=timeline, balance=balance,
                            total_billed=total_purchased, total_unpaid=total_unpaid)


@app.route("/parties/customers/new", methods=["GET", "POST"])
@app.route("/parties/customers/edit/<customer_id>", methods=["GET", "POST"])
@login_required
@role_required("admin", "sales")
def parties_customer_form(customer_id=None):
    existing = db.get_customer(customer_id) if customer_id else None
    if customer_id and not existing:
        flash(f"Customer {customer_id} not found.")
        return redirect(url_for("parties_page", type="customer"))

    category_options = [c["label"] for c in db.list_customer_category_options()]
    custom_fields = db.list_custom_fields("customer")
    custom_values = db.get_custom_field_values("customer", existing["customer_id"]) if existing else {}

    if request.method == "POST":
        new_id = (request.form.get("customer_id") or "").strip()
        name = (request.form.get("name") or "").strip()
        if not new_id or not name:
            flash("Customer ID and name are required.")
            return render_template("party_form.html", party_type="customer", existing=existing, form=request.form,
                                    custom_fields=custom_fields, custom_values={}, category_options=category_options)
        if not existing and db.get_customer(new_id):
            flash(f"Customer ID '{new_id}' already exists.")
            return render_template("party_form.html", party_type="customer", existing=existing, form=request.form,
                                    custom_fields=custom_fields, custom_values={}, category_options=category_options)

        db.upsert_customer(
            customer_id=new_id, name=name,
            contact_number=request.form.get("contact_number", ""),
            address_details=request.form.get("address_details", ""),
            gstn=request.form.get("gstn", ""),
            category=request.form.get("category", "").strip().lower(),
        )
        custom_field_input = {f["field_key"]: request.form.get(f"cf_{f['field_key']}", "") for f in custom_fields}
        db.set_custom_field_values("customer", new_id, custom_field_input)
        flash(f"Customer {'updated' if existing else 'added'}: {name}")
        return redirect(url_for("parties_page", type="customer"))

    return render_template("party_form.html", party_type="customer", existing=existing, form=None,
                            custom_fields=custom_fields, custom_values=custom_values, category_options=category_options)


@app.route("/parties/vendors/new", methods=["GET", "POST"])
@app.route("/parties/vendors/edit/<vendor_id>", methods=["GET", "POST"])
@login_required
@role_required("admin", "purchase", "field_staff")
def parties_vendor_form(vendor_id=None):
    existing = db.get_vendor(vendor_id) if vendor_id else None
    if vendor_id and not existing:
        flash(f"Vendor {vendor_id} not found.")
        return redirect(url_for("parties_page", type="vendor"))

    category_options = [c["label"] for c in db.list_vendor_category_options()]
    custom_fields = db.list_custom_fields("vendor")
    custom_values = db.get_custom_field_values("vendor", existing["vendor_id"]) if existing else {}

    if request.method == "POST":
        new_id = (request.form.get("vendor_id") or "").strip()
        name = (request.form.get("name") or "").strip()
        if not new_id or not name:
            flash("Vendor ID and name are required.")
            return render_template("party_form.html", party_type="vendor", existing=existing, form=request.form,
                                    custom_fields=custom_fields, custom_values={}, category_options=category_options)
        if not existing and db.get_vendor(new_id):
            flash(f"Vendor ID '{new_id}' already exists.")
            return render_template("party_form.html", party_type="vendor", existing=existing, form=request.form,
                                    custom_fields=custom_fields, custom_values={}, category_options=category_options)

        db.upsert_vendor(
            vendor_id=new_id, name=name,
            contact_number=request.form.get("contact_number", ""),
            address_details=request.form.get("address_details", ""),
            gstn=request.form.get("gstn", ""),
            category=request.form.get("category", "").strip(),
        )
        custom_field_input = {f["field_key"]: request.form.get(f"cf_{f['field_key']}", "") for f in custom_fields}
        db.set_custom_field_values("vendor", new_id, custom_field_input)
        flash(f"Vendor {'updated' if existing else 'added'}: {name}")
        return redirect(url_for("parties_page", type="vendor"))

    return render_template("party_form.html", party_type="vendor", existing=existing, form=None,
                            custom_fields=custom_fields, custom_values=custom_values, category_options=category_options)


@app.route("/parties/vendors/<vendor_id>/products", methods=["GET", "POST"])
@login_required
@role_required("admin", "purchase")
def vendor_products_page(vendor_id):
    vendor = db.get_vendor(vendor_id)
    if not vendor:
        flash(f"Vendor {vendor_id} not found.")
        return redirect(url_for("parties_page", type="vendor"))

    if request.method == "POST":
        product_id = request.form.get("product_id")
        action = request.form.get("action")
        try:
            product_id = int(product_id)
        except (TypeError, ValueError):
            product_id = None
        if product_id and action == "map":
            db.map_product_to_vendor(vendor_id, product_id)
        elif product_id and action == "unmap":
            db.unmap_product_from_vendor(vendor_id, product_id)
        return redirect(url_for("vendor_products_page", vendor_id=vendor_id))

    q = request.args.get("q", "").strip().lower()
    all_products = db.list_products(approved_only=True)
    if q:
        all_products = [p for p in all_products if q in (p["item_name"] or "").lower()
                         or q in (p["sheet"] or "").lower() or q in (p["item_description"] or "").lower()]
    mapped_ids = db.list_mapped_product_ids_for_vendor(vendor_id)

    return render_template("vendor_products.html", vendor=vendor, products=all_products[:500],
                            mapped_ids=mapped_ids, q=request.args.get("q", ""),
                            mapped_count=len(mapped_ids))


@app.route("/products/manage")
@login_required
@role_required("admin", "sales", "purchase")
def products_manage():
    q = request.args.get("q", "").strip().lower()
    sheet = request.args.get("sheet", "all")

    items = db.list_products()
    sheets = db.list_product_sheets()
    if sheet != "all":
        items = [p for p in items if p["sheet"] == sheet]
    if q:
        items = [p for p in items if q in (p["item_name"] or "").lower() or q in (p["item_description"] or "").lower()]

    items = items[:500]
    custom_fields = db.list_custom_fields("product")
    custom_values = db.get_custom_field_values_bulk("product", [p["id"] for p in items])

    return render_template("products_manage.html", items=items, sheets=sheets,
                            q=request.args.get("q", ""), sheet=sheet, total_count=len(items),
                            custom_fields=custom_fields, custom_values=custom_values)


@app.route("/products/new", methods=["GET", "POST"])
@app.route("/products/edit/<int:product_id>", methods=["GET", "POST"])
@login_required
@role_required("admin", "sales", "purchase", "field_staff")
def products_form(product_id=None):
    user = current_user()
    existing = db.get_product(product_id) if product_id else None
    if product_id and not existing:
        flash(f"Product not found.")
        return redirect(url_for("products_manage"))
    if existing and user["role"] == "field_staff":
        flash("Field staff can add new products but can't edit existing ones.")
        return redirect(url_for("products_manage"))

    custom_fields = db.list_custom_fields("product")
    custom_values = db.get_custom_field_values("product", existing["id"]) if existing else {}

    if request.method == "POST":
        sheet = (request.form.get("sheet") or "").strip()
        item_name = (request.form.get("item_name") or "").strip()
        if not sheet or not item_name:
            flash("Category and item name are required.")
            return render_template("product_form.html", existing=existing, sheets=db.list_product_sheets(),
                                    form=request.form, custom_fields=custom_fields, custom_values={})

        try:
            mrp = request.form.get("mrp", "")
            quantity = float(request.form.get("quantity") or 0)
            gst_pct = float(request.form.get("gst_pct") or 0)
            cost_price = float(request.form.get("cost_price") or 0)
            sale_price = float(request.form.get("sale_price") or 0)
        except ValueError:
            flash("Quantity, GST%, cost price, and sale price must be numbers.")
            return render_template("product_form.html", existing=existing, sheets=db.list_product_sheets(),
                                    form=request.form, custom_fields=custom_fields, custom_values={})

        custom_field_input = {f["field_key"]: request.form.get(f"cf_{f['field_key']}", "") for f in custom_fields}

        try:
            if existing:
                db.update_product_by_id(
                    existing["id"], sheet=sheet, item_code=request.form.get("item_code", ""),
                    item_name=item_name, item_description=request.form.get("item_description", ""),
                    hsn=request.form.get("hsn", ""), mrp=mrp, quantity=quantity, gst_pct=gst_pct,
                    cost_price=cost_price, sale_price=sale_price,
                )
                db.set_custom_field_values("product", existing["id"], custom_field_input)
                flash(f"Product updated: {item_name}")
            elif user["role"] == "field_staff":
                new_id = db.insert_product_pending(
                    sheet=sheet, item_code=request.form.get("item_code", ""), item_name=item_name,
                    item_description=request.form.get("item_description", ""), hsn=request.form.get("hsn", ""),
                    mrp=mrp, quantity=quantity, gst_pct=gst_pct, cost_price=cost_price, sale_price=sale_price,
                    created_by=user["username"],
                )
                db.set_custom_field_values("product", new_id, custom_field_input)
                flash(f"Product submitted for admin approval: {item_name}. It won't appear in New Bill/New Purchase until approved.")
            else:
                new_id = db.insert_product(
                    sheet=sheet, item_code=request.form.get("item_code", ""), item_name=item_name,
                    item_description=request.form.get("item_description", ""), hsn=request.form.get("hsn", ""),
                    mrp=mrp, quantity=quantity, gst_pct=gst_pct, cost_price=cost_price, sale_price=sale_price,
                )
                db.set_custom_field_values("product", new_id, custom_field_input)
                flash(f"Product added: {item_name}")
        except ValueError as e:
            flash(str(e))
            return render_template("product_form.html", existing=existing, sheets=db.list_product_sheets(),
                                    form=request.form, custom_fields=custom_fields, custom_values={})

        return redirect(url_for("products_manage"))

    return render_template("product_form.html", existing=existing, sheets=db.list_product_sheets(), form=None,
                            custom_fields=custom_fields, custom_values=custom_values)


@app.route("/products/pending")
@login_required
@role_required("admin")
def products_pending():
    return render_template("products_pending.html", items=db.list_pending_products())


@app.route("/products/pending/<int:product_id>/approve", methods=["POST"])
@login_required
@role_required("admin")
def products_pending_approve(product_id):
    db.approve_product(product_id)
    flash("Product approved — now available in New Bill / New Purchase.")
    return redirect(url_for("products_pending"))


@app.route("/products/pending/<int:product_id>/reject", methods=["POST"])
@login_required
@role_required("admin")
def products_pending_reject(product_id):
    db.reject_pending_product(product_id)
    flash("Product submission rejected and removed.")
    return redirect(url_for("products_pending"))


@app.route("/deliveries")
@login_required
@role_required("admin", "field_staff")
def deliveries_list():
    receipts = db.list_delivery_receipts()
    for r in receipts:
        r["invoice_status"] = db.get_delivery_receipt_invoice_status(r["id"])
    return render_template("deliveries_list.html", receipts=receipts)


@app.route("/deliveries/new", methods=["GET", "POST"])
@login_required
@role_required("admin", "field_staff")
def deliveries_new():
    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        customer_id = payload.get("customer_id")
        date_str = payload.get("date") or datetime.date.today().strftime("%d/%m/%Y")
        raw_lines = payload.get("lines") or []

        if not customer_id:
            return jsonify({"ok": False, "error": "customer is required"}), 400
        if not raw_lines:
            return jsonify({"ok": False, "error": "at least one item is required"}), 400

        customer = db.get_customer(customer_id)
        if not customer:
            return jsonify({"ok": False, "error": "customer not found"}), 400

        lines = []
        for rl in raw_lines:
            product = db.get_product(rl.get("id"))
            if not product:
                return jsonify({"ok": False, "error": f"product not found: id {rl.get('id')}"}), 400
            try:
                qty = float(rl.get("qty"))
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "invalid quantity"}), 400
            if qty <= 0:
                return jsonify({"ok": False, "error": "quantity must be positive"}), 400
            lines.append({
                "product_id": product["id"],
                "description": billing_engine.build_full_description(
                    product["sheet"], product["item_name"], product["item_description"]),
                "qty": qty,
                "update_stock": bool(rl.get("update_stock", True)),
            })

        try:
            receipt_no = db.next_delivery_receipt_number()
            user = current_user()
            db.insert_delivery_receipt(
                receipt_no=receipt_no, customer_id=customer["customer_id"],
                customer_name=customer["name"], date=date_str,
                created_by=user["username"] if user else "", lines=lines,
            )
            _write_delivery_pdfs(receipt_no, date_str, customer["name"], lines)
        except Exception as e:
            return jsonify({"ok": False, "error": f"failed to create delivery receipt: {e}"}), 500

        return jsonify({
            "ok": True, "receipt_no": receipt_no,
            "pdf_url_customer": url_for("deliveries_pdf", receipt_no=receipt_no, copy="customer"),
            "pdf_url_office": url_for("deliveries_pdf", receipt_no=receipt_no, copy="office"),
        })

    return render_template("deliveries_new.html")


def _write_delivery_pdfs(receipt_no, date_str, customer_name, lines):
    """Writes both the customer-copy and office-copy PDFs for a delivery receipt."""
    folder = rrl.local_folder_name(date_str)
    pdf_dir = os.path.join(BASE_DIR, "delivery_room", folder)
    os.makedirs(pdf_dir, exist_ok=True)
    for copy_type in ("customer", "office"):
        pdf_bytes = delivery_pdf.build_delivery_pdf(receipt_no, date_str, customer_name, lines, copy_type)
        with open(os.path.join(pdf_dir, f"{receipt_no}_{copy_type}.pdf"), "wb") as f:
            f.write(pdf_bytes)


@app.route("/deliveries/pending")
@login_required
@role_required("admin")
def deliveries_pending():
    items = []
    for receipt in db.list_pending_delivery_receipts():
        lines = db.get_delivery_receipt_lines(receipt["id"])
        items.append({"receipt": receipt, "lines": lines})
    return render_template("deliveries_pending.html", items=items)


@app.route("/deliveries/pending/<int:receipt_id>/approve", methods=["POST"])
@login_required
@role_required("admin")
def deliveries_pending_approve(receipt_id):
    user = current_user()
    try:
        db.approve_delivery_receipt(receipt_id, user["username"] if user else "")
        flash("Delivery approved — stock updated for its US-tagged items.")
    except ValueError as e:
        flash(str(e))
    return redirect(url_for("deliveries_pending"))


@app.route("/deliveries/pending/<int:receipt_id>/reject", methods=["POST"])
@login_required
@role_required("admin")
def deliveries_pending_reject(receipt_id):
    try:
        db.reject_delivery_receipt(receipt_id)
        flash("Delivery rejected — no stock was affected.")
    except ValueError as e:
        flash(str(e))
    return redirect(url_for("deliveries_pending"))


@app.route("/deliveries/edit/<receipt_no>", methods=["GET", "POST"])
@login_required
@role_required("admin", "field_staff")
def deliveries_edit(receipt_no):
    receipt = db.get_delivery_receipt(receipt_no)
    if not receipt:
        flash(f"Delivery {receipt_no} not found.")
        return redirect(url_for("deliveries_list"))
    existing_lines = db.get_delivery_receipt_lines(receipt["id"])

    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        raw_lines = payload.get("lines") or []
        if not raw_lines:
            return jsonify({"ok": False, "error": "at least one item is required"}), 400

        was_approved = receipt["status"] == "approved"
        old_us_qty_by_product = {}
        if was_approved:
            for l in existing_lines:
                if l["update_stock"] and l["product_id"]:
                    old_us_qty_by_product[l["product_id"]] = old_us_qty_by_product.get(l["product_id"], 0) + l["qty"]

        lines = []
        for rl in raw_lines:
            product = db.get_product(rl.get("id"))
            if not product:
                return jsonify({"ok": False, "error": f"product not found: id {rl.get('id')}"}), 400
            try:
                qty = float(rl.get("qty"))
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "invalid quantity"}), 400
            if qty <= 0:
                return jsonify({"ok": False, "error": "quantity must be positive"}), 400
            lines.append({
                "product_id": product["id"],
                "description": billing_engine.build_full_description(
                    product["sheet"], product["item_name"], product["item_description"]),
                "qty": qty,
                "update_stock": bool(rl.get("update_stock", True)),
            })

        try:
            if was_approved:
                # Reverse the old approved stock impact, then apply the new one.
                for product_id, qty in old_us_qty_by_product.items():
                    db.increment_product_stock(product_id, qty)
                for line in lines:
                    if line["update_stock"]:
                        db.decrement_product_stock(line["product_id"], line["qty"])

            db.replace_delivery_receipt_lines(receipt["id"], lines)
            _write_delivery_pdfs(receipt_no, receipt["date"], receipt["customer_name"], lines)
        except Exception as e:
            return jsonify({"ok": False, "error": f"failed to update delivery: {e}"}), 500

        return jsonify({"ok": True})

    return render_template("deliveries_edit.html", receipt=receipt, existing_lines=existing_lines)


@app.route("/deliveries/delete/<receipt_no>", methods=["POST"])
@login_required
@role_required("admin", "field_staff")
def deliveries_delete(receipt_no):
    receipt = db.get_delivery_receipt(receipt_no)
    if not receipt:
        flash(f"Delivery {receipt_no} not found.")
        return redirect(url_for("deliveries_list"))
    db.soft_delete_delivery_receipt(receipt_no)
    if receipt["status"] == "approved":
        flash(f"Deleted {receipt_no} and reversed stock for its US-tagged items.")
    else:
        flash(f"Deleted {receipt_no}. (Was still pending approval — no stock was affected.)")
    return redirect(url_for("deliveries_list"))


@app.route("/deliveries/invoice")
@login_required
@role_required("admin")
def deliveries_invoice_page():
    return render_template("deliveries_invoice.html")


@app.route("/deliveries/invoice/api/lines/<customer_id>")
@login_required
@role_required("admin")
def deliveries_invoice_lines(customer_id):
    customer = db.get_customer(customer_id)
    if not customer:
        return jsonify({"ok": False, "error": "customer not found"}), 404

    raw = db.get_uninvoiced_delivery_lines_for_customer(customer_id)
    build_lines = []
    for l in raw:
        product = db.get_product(l["product_id"]) if l["product_id"] else None
        if not product:
            continue
        item = dict(product)
        item["qty"] = l["qty"]
        item["product_id"] = product["id"]
        build_lines.append(item)

    # Reuse the exact same math New Bill uses, so the preview shown here
    # matches what actually gets created to the rupee - not a re-derived
    # approximation of the formula.
    priced_lines, _ = billing_engine.build_bill_lines(build_lines, "Tax Invoice") if build_lines else ([], {})

    result = []
    for src, priced in zip(raw, priced_lines):
        result.append({
            "line_id": src["line_id"], "receipt_no": src["receipt_no"], "delivery_date": src["delivery_date"],
            "product_id": src["product_id"], "description": priced["description"], "qty": priced["qty"],
            "update_stock": bool(src["update_stock"]), "gst_pct": priced["gst_pct"],
            "rate": priced["rate"], "amount": priced["amount"],
        })

    return jsonify({
        "ok": True, "customer_name": customer["name"], "lines": result,
        "pending_count": db.count_pending_delivery_receipts_for_customer(customer_id),
    })


@app.route("/deliveries/invoice/create", methods=["POST"])
@login_required
@role_required("admin")
def deliveries_invoice_create():
    if db.product_count() == 0:
        return jsonify({"ok": False, "error": "no stock catalog loaded"}), 400

    payload = request.get_json(silent=True) or {}
    customer_id = payload.get("customer_id")
    bill_type = payload.get("bill_type", "Tax Invoice")
    date_str = payload.get("date")
    manual_invoice_no = (payload.get("invoice_no") or "").strip()
    delivery_line_ids = payload.get("delivery_line_ids") or []

    if not customer_id:
        return jsonify({"ok": False, "error": "customer is required"}), 400
    if not delivery_line_ids:
        return jsonify({"ok": False, "error": "select at least one delivered item"}), 400
    if not date_str:
        date_str = datetime.date.today().strftime("%d/%m/%Y")
    try:
        datetime.datetime.strptime(date_str, "%d/%m/%Y")
    except ValueError:
        return jsonify({"ok": False, "error": "date must be dd/mm/yyyy"}), 400

    customer = db.get_customer(customer_id)
    if not customer:
        return jsonify({"ok": False, "error": "customer not found"}), 400

    if manual_invoice_no:
        if db.get_bill_by_invoice(manual_invoice_no):
            return jsonify({"ok": False, "error": f"invoice number '{manual_invoice_no}' already exists"}), 400
        invoice_no = manual_invoice_no
    else:
        invoice_no = db.next_invoice_number_db()

    # Only lines that are (a) actually for this customer, (b) still
    # uninvoiced, and (c) came from an approved receipt are eligible -
    # re-checked here server-side rather than trusting the client's
    # selection, since this is what actually moves the invoice number and
    # marks things billed.
    eligible = {l["line_id"]: l for l in db.get_uninvoiced_delivery_lines_for_customer(customer_id)}
    selected = []
    for line_id in delivery_line_ids:
        if line_id not in eligible:
            return jsonify({"ok": False, "error": f"delivery line {line_id} is no longer available "
                                                    f"(already invoiced, or not for this customer)"}), 400
        selected.append(eligible[line_id])

    # Group by product, summing qty - the same item might appear across
    # more than one delivery receipt for this customer.
    by_product = {}
    for l in selected:
        if not l["product_id"]:
            continue
        by_product.setdefault(l["product_id"], 0)
        by_product[l["product_id"]] += l["qty"]

    build_lines = []
    for product_id, qty in by_product.items():
        product = db.get_product(product_id)
        if not product:
            return jsonify({"ok": False, "error": f"product {product_id} no longer exists"}), 400
        item = dict(product)
        item["qty"] = qty
        item["product_id"] = product["id"]
        build_lines.append(item)

    try:
        lines, cost_map = billing_engine.build_bill_lines(build_lines, bill_type)
        final_data = billing_engine.build_final_data(
            {"customer_id": customer["customer_id"], "name": customer["name"],
             "contact_number": customer["contact_number"], "address_details": customer["address_details"],
             "gstn": customer["gstn"]},
            lines, invoice_no, date_str, bill_type,
        )
        margin_total = billing_engine.compute_margin(lines, cost_map)
        hsn_summary = billing_engine.compute_hsn_summary(lines)

        billing_engine.write_record_room_json(final_data)
        billing_engine.write_cost_report(final_data, cost_map)

        pdf_bytes = bill_pdf.build_invoice_pdf(final_data, lines, hsn_summary)
        folder = rrl.local_folder_name(final_data["Date"])
        pdf_path = os.path.join(BASE_DIR, "record_room", folder, final_data["fileName"] + ".pdf")
        with open(pdf_path, "wb") as f:
            f.write(pdf_bytes)

        # Deliberately NOT touching stock here: US-tagged delivery lines
        # already decremented it back when the delivery was approved, and
        # decrementing again here would double-count the same physical
        # goods. Stock only ever moves at delivery-approval time (or
        # New Bill's own direct-sale flow) - this step is purely turning
        # already-delivered goods into a formal billing document.
        user = current_user()
        bill_id = db.insert_bill(
            file_name=final_data["fileName"], invoice_no=final_data["Invoice"],
            original_invoice_no=final_data["Invoice"], split_leg="", bill_type=bill_type,
            date=final_data["Date"], customer_id=customer["customer_id"], customer_name=customer["name"],
            total=final_data["total"], margin=margin_total, taxable_total=final_data.get("ttaxamt", 0),
            status="unpaid", payment_date=None, is_candidate=True,
            created_by=user["username"] if user else "", lines=lines,
        )
        db.mark_delivery_lines_invoiced([l["line_id"] for l in selected], bill_id)
    except Exception as e:
        return jsonify({"ok": False, "error": f"failed to create invoice: {e}"}), 500

    return jsonify({
        "ok": True, "invoice_no": invoice_no, "total": final_data["total"],
        "pdf_url": url_for("bills_pdf", file_name=final_data["fileName"]),
    })


@app.route("/deliveries/pdf/<receipt_no>")
@login_required
@role_required("admin", "field_staff")
def deliveries_pdf(receipt_no):
    copy_type = request.args.get("copy", "customer")
    if copy_type not in ("customer", "office"):
        copy_type = "customer"
    receipt = db.get_delivery_receipt(receipt_no)
    if not receipt:
        return jsonify({"ok": False, "error": "not found"}), 404
    folder = rrl.local_folder_name(receipt["date"])
    path = rrl.resolve_case_insensitive(
        os.path.join(BASE_DIR, "delivery_room", folder, f"{receipt_no}_{copy_type}.pdf")
    )
    if not os.path.exists(path):
        return jsonify({"ok": False, "error": "not found"}), 404
    return send_file(path, mimetype="application/pdf")


@app.route("/money-requests")
@login_required
@role_required("admin", "sales", "purchase", "accountant", "field_staff")
def money_requests_list():
    user = current_user()
    if user["role"] == "admin":
        requests_ = db.list_money_requests()
        cash_history = []
    else:
        requests_ = db.list_money_requests(requested_by=user["username"]) if user["role"] == "field_staff" else []
        cash_history = db.get_cash_ledger_history(
            user["username"],
            entry_type=request.args.get("entry_type") or None,
            date_from=request.args.get("date_from") or None,
            date_to=request.args.get("date_to") or None,
        )
    for r in requests_:
        if r["reason"] == "Purchase" and r["status"] == "approved":
            r["spent_so_far"] = db.get_money_request_spent(r["id"])
            r["purchases"] = db.get_purchases_for_money_request(r["id"])
    balance = db.get_cash_balance(user["username"])
    return render_template("money_requests.html", requests=requests_, balance=balance,
                            is_admin=(user["role"] == "admin"), is_field_staff=(user["role"] == "field_staff"),
                            username=user["username"],
                            cash_history=cash_history,
                            filters={"entry_type": request.args.get("entry_type", ""),
                                     "date_from": request.args.get("date_from", ""),
                                     "date_to": request.args.get("date_to", "")})


@app.route("/money-requests/new", methods=["GET", "POST"])
@login_required
@role_required("field_staff")
def money_requests_new():
    reasons = db.list_money_request_reasons()
    if request.method == "POST":
        try:
            amount = float(request.form.get("amount") or 0)
        except ValueError:
            amount = 0
        reason = request.form.get("reason", "")
        note = request.form.get("note", "")
        is_gst_bill = request.form.get("is_gst_bill") == "on"

        if amount <= 0:
            flash("Amount must be positive.")
            return render_template("money_request_form.html", reasons=reasons)
        if reason not in [r["label"] for r in reasons]:
            flash("Please pick a valid reason.")
            return render_template("money_request_form.html", reasons=reasons)

        user = current_user()
        db.create_money_request(user["username"], amount, reason, note, is_gst_bill)
        flash(f"Money request submitted: ₹{amount:,.2f} for {reason} — waiting for admin approval.")
        return redirect(url_for("money_requests_list"))

    return render_template("money_request_form.html", reasons=reasons)


@app.route("/money-requests/<int:request_id>/approve", methods=["POST"])
@login_required
@role_required("admin")
def money_requests_approve(request_id):
    user = current_user()
    try:
        db.resolve_money_request(request_id, "approved", user["username"])
        flash("Request approved — amount credited to their cash balance.")
    except ValueError as e:
        flash(str(e))
    return redirect(url_for("money_requests_list"))


@app.route("/money-requests/<int:request_id>/reject", methods=["POST"])
@login_required
@role_required("admin")
def money_requests_reject(request_id):
    user = current_user()
    try:
        db.resolve_money_request(request_id, "rejected", user["username"])
        flash("Request rejected.")
    except ValueError as e:
        flash(str(e))
    return redirect(url_for("money_requests_list"))


@app.route("/purchases")
@login_required
@role_required("admin", "purchase", "field_staff")
def purchases_page():
    catalog_loaded = db.product_count() > 0
    return render_template("purchases.html", catalog_loaded=catalog_loaded)


@app.route("/purchases/api/vendors")
@login_required
@role_required("admin", "purchase", "field_staff")
def purchases_api_vendors():
    user = current_user()
    vendors = db.list_vendors_for_user(user["username"], user["role"])
    custom_values = db.get_custom_field_values_bulk("vendor", [v["vendor_id"] for v in vendors])
    return jsonify({"ok": True, "vendors": [
        {"vendor_id": v["vendor_id"], "name": v["name"], "contact_number": v["contact_number"],
         "address_details": v["address_details"], "gstn": v["gstn"], "category": v["category"],
         "custom_fields": custom_values.get(v["vendor_id"], {})}
        for v in vendors
    ]})


@app.route("/purchases/api/products")
@login_required
@role_required("admin", "purchase", "field_staff")
def purchases_api_products():
    user = current_user()
    vendor_id = request.args.get("vendor_id")
    if vendor_id:
        # Once a vendor is picked, only offer what's actually mapped to that
        # vendor, plus whatever this user has personally added themselves
        # (list_products_for_vendor_purchase handles both).
        products = db.list_products_for_vendor_purchase(vendor_id, user["username"])
    else:
        products = db.list_products_for_user(user["username"])
    custom_values = db.get_custom_field_values_bulk("product", [p["id"] for p in products])
    return jsonify({"ok": True, "products": [
        {"id": p["id"], "sheet": p["sheet"], "item_code": p["item_code"], "item_name": p["item_name"],
         "item_description": p["item_description"], "hsn": p["hsn"], "quantity": p["quantity"],
         "gst_pct": p["gst_pct"], "cost_price": p["cost_price"], "custom_fields": custom_values.get(str(p["id"]), {}),
         "approved": bool(p["approved"])}
        for p in products
    ]})


@app.route("/purchases/api/new", methods=["POST"])
@login_required
@role_required("admin", "purchase", "field_staff")
def purchases_api_new():
    if db.product_count() == 0:
        return jsonify({"ok": False, "error": "no stock catalog loaded"}), 400

    payload = request.get_json(silent=True) or {}
    vendor_id = payload.get("vendor_id")
    date_str = payload.get("date")
    manual_purchase_no = (payload.get("purchase_no") or "").strip()
    raw_lines = payload.get("lines") or []
    is_gst_bill = payload.get("is_gst_bill", True)
    money_request_id = payload.get("money_request_id")

    if not vendor_id:
        return jsonify({"ok": False, "error": "vendor is required"}), 400
    if not raw_lines:
        return jsonify({"ok": False, "error": "at least one line item is required"}), 400
    if not date_str:
        date_str = datetime.date.today().strftime("%d/%m/%Y")
    try:
        datetime.datetime.strptime(date_str, "%d/%m/%Y")
    except ValueError:
        return jsonify({"ok": False, "error": "date must be dd/mm/yyyy"}), 400

    vendor = next((v for v in db.list_vendors() if v["vendor_id"] == vendor_id), None)
    if not vendor:
        return jsonify({"ok": False, "error": "vendor not found"}), 400

    linked_request = None
    if money_request_id:
        linked_request = db.get_money_request(money_request_id)
        if not linked_request or linked_request["status"] != "approved" or linked_request["reason"] != "Purchase":
            return jsonify({"ok": False, "error": "linked money request is invalid or not an approved Purchase request"}), 400
        # Multiple purchases can be made against one request (a field agent
        # might split one cash withdrawal across several small buys, which
        # is the normal case, not an exception) - not blocking on budget,
        # just surfaced as a warning in the response so the UI can flag it.

    if manual_purchase_no:
        if db.get_purchase_bill_by_no(manual_purchase_no):
            return jsonify({"ok": False, "error": f"purchase number '{manual_purchase_no}' already exists"}), 400
        purchase_no = manual_purchase_no
    else:
        purchase_no = db.next_purchase_number()

    lines = []
    for rl in raw_lines:
        product = db.get_product(rl.get("id"))
        if not product:
            return jsonify({"ok": False, "error": f"product not found: id {rl.get('id')}"}), 400
        try:
            qty = float(rl.get("qty"))
            cost_rate = float(rl.get("cost_rate"))
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "invalid quantity or cost rate"}), 400
        if qty <= 0 or cost_rate < 0:
            return jsonify({"ok": False, "error": "quantity must be positive, cost rate can't be negative"}), 400
        gst_pct = float(product["gst_pct"] or 0) if is_gst_bill else 0.0
        amount = round(cost_rate * qty, 2)
        lines.append({
            "product_id": product["id"],
            "description": billing_engine.build_full_description(
                product["sheet"], product["item_name"], product["item_description"]),
            "hsn": str(product["hsn"] or "")[:4],
            "qty": qty, "gst_pct": gst_pct, "cost_rate": cost_rate, "amount": amount,
        })

    try:
        tax_fields, totals = billing_engine.compute_tax(
            [{"amount": l["amount"], "gst_pct": l["gst_pct"]} for l in lines], "Tax Invoice"
        )
        file_name = f"{purchase_no}_{date_str.replace('/', '')}"
        purchase_data = {
            "purchase_no": purchase_no, "date": date_str,
            "vendor_id": vendor["vendor_id"], "vendor_name": vendor["name"],
            "taxable_total": totals["ttaxamt"], "cgst": totals["cgst"], "sgst": totals["sgst"],
            "total": totals["total"], "amount_in_words": totals["finalAmountWord"],
        }

        pdf_bytes = purchase_pdf.build_purchase_pdf(purchase_data, lines)
        folder = rrl.local_folder_name(date_str)
        pdf_dir = os.path.join(BASE_DIR, "purchase_room", folder)
        os.makedirs(pdf_dir, exist_ok=True)
        with open(os.path.join(pdf_dir, file_name + ".pdf"), "wb") as f:
            f.write(pdf_bytes)
        with open(os.path.join(pdf_dir, file_name + ".json"), "w") as f:
            json.dump({**purchase_data, "lines": lines, "fileName": file_name}, f)

        for line in lines:
            db.increment_product_stock(line["product_id"], line["qty"], new_cost_price=line["cost_rate"])

        user = current_user()
        purchase_bill_id = db.insert_purchase_bill(
            file_name=file_name, purchase_no=purchase_no, vendor_id=vendor["vendor_id"],
            vendor_name=vendor["name"], date=date_str, total=totals["total"],
            taxable_total=totals["ttaxamt"], status="unpaid", payment_date=None,
            created_by=user["username"] if user else "", lines=lines,
            money_request_id=linked_request["id"] if linked_request else None,
        )

        over_budget_warning = None
        if linked_request:
            db.add_cash_ledger_entry(
                linked_request["requested_by"], "debit", totals["total"],
                note=f"Purchase {purchase_no} (from money request #{linked_request['id']})",
                linked_purchase_no=purchase_no,
            )
            spent = db.get_money_request_spent(linked_request["id"])
            if spent > linked_request["amount"] + 0.01:
                over_budget_warning = (
                    f"Note: total spent against this request is now ₹{spent:,.2f}, "
                    f"over the approved ₹{linked_request['amount']:,.2f}."
                )
    except Exception as e:
        return jsonify({"ok": False, "error": f"failed to create purchase bill: {e}"}), 500

    return jsonify({
        "ok": True, "purchase_no": purchase_no, "total": totals["total"],
        "purchase_bill_id": purchase_bill_id,
        "pdf_url": url_for("purchases_pdf", file_name=file_name),
        "warning": over_budget_warning,
    })


@app.route("/purchases/list")
@login_required
@role_required("admin", "purchase", "field_staff", "accountant")
def purchases_list():
    q = request.args.get("q", "").strip().lower()
    status = request.args.get("status", "all")

    bills = db.list_purchase_bills()
    if status != "all":
        bills = [b for b in bills if b["status"] == status]
    if q:
        bills = [b for b in bills if q in b["purchase_no"].lower() or q in (b["vendor_name"] or "").lower()]

    return render_template("purchases_list.html", bills=bills[:500], q=request.args.get("q", ""),
                            status=status, total_count=len(db.list_purchase_bills()))


@app.route("/purchases/<purchase_no>/record-payment", methods=["POST"])
@login_required
@role_required("admin", "purchase", "field_staff")
def purchases_record_payment(purchase_no):
    payload = request.get_json(silent=True) or {}
    try:
        amount = float(payload.get("amount") or 0)
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "invalid amount"}), 400
    paid_via = payload.get("paid_via", "cash")
    if paid_via not in ("cash", "vendor_wallet"):
        paid_via = "cash"
    payment_date = (payload.get("payment_date") or "").strip() or datetime.date.today().isoformat()
    note = payload.get("note", "")

    purchase = db.get_purchase_bill_by_no(purchase_no)
    if not purchase:
        return jsonify({"ok": False, "error": "purchase not found"}), 404

    user = current_user()
    try:
        db.record_purchase_payment(purchase["id"], amount, paid_via, payment_date, note,
                                    user["username"] if user else "")
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    updated = db.get_purchase_bill_by_id(purchase["id"])
    return jsonify({"ok": True, "status": updated["status"], "amount_paid": updated["amount_paid"],
                     "total": updated["total"]})


@app.route("/purchases/<purchase_no>/payments")
@login_required
@role_required("admin", "purchase", "field_staff", "accountant")
def purchases_payment_history(purchase_no):
    purchase = db.get_purchase_bill_by_no(purchase_no)
    if not purchase:
        return jsonify({"ok": False, "error": "not found"}), 404
    return jsonify({"ok": True, "payments": db.get_purchase_payments(purchase["id"])})


@app.route("/purchases/<purchase_no>/status", methods=["POST"])
@login_required
@role_required("admin", "purchase", "field_staff")
def purchases_toggle_status(purchase_no):
    """Legacy 'mark fully paid in one click' entry point - kept for anyone
    still using the old binary flow, but now routes through the same
    record_purchase_payment path as partial payments, so amount_paid and
    payment history stay consistent regardless of which UI path was used.
    Un-marking paid isn't supported here anymore - once a payment is
    recorded, reversing it means recording a correcting entry, not erasing
    history (same reasoning as why a real ledger doesn't let you delete a
    posted transaction)."""
    payload = request.get_json(silent=True) or {}
    new_status = payload.get("status")
    payment_date = payload.get("payment_date")
    pay_from_wallet = bool(payload.get("pay_from_vendor_wallet"))

    if new_status != "paid":
        return jsonify({"ok": False, "error": "Marking a purchase back to unpaid isn't supported once "
                                                "a payment is recorded - use Record Payment instead."}), 400

    payment_date = (payment_date or "").strip() or datetime.date.today().isoformat()
    try:
        datetime.datetime.strptime(payment_date, "%Y-%m-%d")
    except ValueError:
        return jsonify({"ok": False, "error": "invalid payment_date, expected YYYY-MM-DD"}), 400

    purchase = db.get_purchase_bill_by_no(purchase_no)
    if not purchase:
        return jsonify({"ok": False, "error": "purchase not found"}), 404
    remaining = round(purchase["total"] - purchase["amount_paid"], 2)
    if remaining <= 0:
        return jsonify({"ok": False, "error": "already fully paid"}), 400

    user = current_user()
    try:
        db.record_purchase_payment(purchase["id"], remaining, "vendor_wallet" if pay_from_wallet else "cash",
                                    payment_date, "Marked fully paid", user["username"] if user else "")
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    return jsonify({"ok": True, "purchase_no": purchase_no, "status": "paid", "payment_date": payment_date})


@app.route("/purchases/delete/<purchase_no>", methods=["POST"])
@login_required
@role_required("admin", "purchase", "field_staff")
def purchases_delete(purchase_no):
    bill = db.get_purchase_bill_by_no(purchase_no)
    if not bill:
        flash(f"Purchase {purchase_no} not found.")
        return redirect(url_for("purchases_list"))

    lines = db.get_purchase_bill_lines(bill["id"])
    reverted = 0
    for line in lines:
        if line["product_id"]:
            db.increment_product_stock(line["product_id"], -line["qty"])  # remove the stock this purchase added
            reverted += 1

    db.soft_delete_purchase_bill(purchase_no)
    if reverted:
        flash(f"Deleted {purchase_no} and reversed stock for {reverted} line item(s). "
              f"(Cost price on affected products was not reverted — it reflects the latest known purchase cost.)")
    else:
        flash(f"Deleted {purchase_no}.")
    return redirect(url_for("purchases_list"))


@app.route("/purchases/edit/<purchase_no>", methods=["GET", "POST"])
@login_required
@role_required("admin", "purchase", "field_staff")
def purchases_edit(purchase_no):
    bill = db.get_purchase_bill_by_no(purchase_no)
    if not bill:
        flash(f"Purchase {purchase_no} not found.")
        return redirect(url_for("purchases_list"))

    existing_lines = db.get_purchase_bill_lines(bill["id"])

    if request.method == "POST":
        payload = request.get_json(silent=True) or {}
        raw_lines = payload.get("lines") or []
        if not raw_lines:
            return jsonify({"ok": False, "error": "at least one line item is required"}), 400

        # Date is intentionally not editable here, same rationale as Sales
        # Edit: purchase_room files are keyed by date, so changing it would
        # relocate/orphan files.
        old_qty_by_product = {}
        for l in existing_lines:
            old_qty_by_product[l["product_id"]] = old_qty_by_product.get(l["product_id"], 0) + l["qty"]

        lines = []
        for rl in raw_lines:
            product = db.get_product(rl.get("id"))
            if not product:
                return jsonify({"ok": False, "error": f"product not found: id {rl.get('id')}"}), 400
            try:
                qty = float(rl.get("qty"))
                cost_rate = float(rl.get("cost_rate"))
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "invalid quantity or cost rate"}), 400
            if qty <= 0 or cost_rate < 0:
                return jsonify({"ok": False, "error": "quantity must be positive, cost rate can't be negative"}), 400
            gst_pct = float(product["gst_pct"] or 0)
            amount = round(cost_rate * qty, 2)
            lines.append({
                "product_id": product["id"],
                "description": billing_engine.build_full_description(
                    product["sheet"], product["item_name"], product["item_description"]),
                "hsn": str(product["hsn"] or "")[:4],
                "qty": qty, "gst_pct": gst_pct, "cost_rate": cost_rate, "amount": amount,
            })

        try:
            tax_fields, totals = billing_engine.compute_tax(
                [{"amount": l["amount"], "gst_pct": l["gst_pct"]} for l in lines], "Tax Invoice"
            )

            purchase_data = {
                "purchase_no": bill["purchase_no"], "date": bill["date"],
                "vendor_id": bill["vendor_id"], "vendor_name": bill["vendor_name"],
                "taxable_total": totals["ttaxamt"], "cgst": totals["cgst"], "sgst": totals["sgst"],
                "total": totals["total"], "amount_in_words": totals["finalAmountWord"],
            }
            pdf_bytes = purchase_pdf.build_purchase_pdf(purchase_data, lines)
            folder = rrl.local_folder_name(bill["date"])
            pdf_dir = os.path.join(BASE_DIR, "purchase_room", folder)
            os.makedirs(pdf_dir, exist_ok=True)
            with open(os.path.join(pdf_dir, bill["file_name"] + ".pdf"), "wb") as f:
                f.write(pdf_bytes)
            with open(os.path.join(pdf_dir, bill["file_name"] + ".json"), "w") as f:
                json.dump({**purchase_data, "lines": lines, "fileName": bill["file_name"]}, f)

            # Reverse the old stock impact (this purchase's old lines added
            # stock; remove that first), then apply the new one.
            for product_id, qty in old_qty_by_product.items():
                db.increment_product_stock(product_id, -qty)
            for line in lines:
                db.increment_product_stock(line["product_id"], line["qty"], new_cost_price=line["cost_rate"])

            db.update_purchase_bill_totals(bill["id"], totals["total"], totals["ttaxamt"])
            db.replace_purchase_bill_lines(bill["id"], lines)
        except Exception as e:
            return jsonify({"ok": False, "error": f"failed to update purchase: {e}"}), 500

        return jsonify({
            "ok": True, "total": totals["total"],
            "pdf_url": url_for("purchases_pdf", file_name=bill["file_name"]),
        })

    return render_template("purchases_edit.html", bill=bill, existing_lines=existing_lines)


@app.route("/purchases/pdf/<file_name>")
@login_required
@role_required("admin", "purchase", "field_staff")
def purchases_pdf(file_name):
    if "_" not in file_name or not re.match(r"^.+_\d{8}$", file_name):
        return jsonify({"ok": False, "error": "invalid file name"}), 400
    ddmmyyyy = file_name[-8:]
    dd, mm, yyyy = ddmmyyyy[:2], ddmmyyyy[2:4], ddmmyyyy[4:]
    folder = f"{mm}_{dd}_{yyyy}"
    path = rrl.resolve_case_insensitive(
        os.path.join(BASE_DIR, "purchase_room", folder, file_name + ".pdf")
    )
    if not os.path.exists(path):
        return jsonify({"ok": False, "error": "not found"}), 404
    return send_file(path, mimetype="application/pdf")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
