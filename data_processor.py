"""
Reads the sales excel sheet and builds invoice-line level data for the dashboard.

Column positions used (0-indexed, matches the spec):
    C (2)  -> Date
    D (3)  -> Invoice #
    F (5)  -> Customer Name
    G (6)  -> Total Amount            (the "Amount" family, paired with J)
    H (7)  -> Margin
    I (8)  -> Payment Status          (legacy base filter, see below)
    J (9)  -> Payment Breakup         (splits G into legs, e.g. "560|560|620")
    K (10) -> Taxable Amount          (the "Taxable Amount" family, paired with L)
    L (11) -> Taxable Amount Breakup  (splits K into legs, same leg count as J)
    O (14) -> Candidate               ("Candidate" or blank; new base filter)

Row-selection rule
-------------------
Historically a row was included if column I ("Payment Status") == "unpaid".
Sheets now carry a column O ("Candidate") that is set to "Candidate" on the
rows that should feed the dashboard, and left blank otherwise. When a sheet
has any data at all in column O, that column becomes the sole basis for
inclusion (row kept only when O == "Candidate"; column I is no longer
consulted). When column O is entirely empty/absent — an older-format sheet
— we fall back to the original "column I == unpaid" rule so old files keep
working unchanged.

Margin vs Taxable
------------------
Every invoice line always carries an "amount" (from G, split via J if
present) — that never changes with the toggle. Alongside it we now also
always compute a "taxable_amount" (from K, split via L if present, using
the same leg count as the J-based amount split). The dashboard's second
metric column switches between "margin" and "taxable_amount" depending on
the Margin/Taxable toggle; the underlying data for both is always present
on every invoice line so switching is just a display concern.
"""
import datetime
import string

import openpyxl

AMOUNT_RANGES = {
    "lt1000": ("< 1,000", None, 1000),
    "1000-2000": ("1,000 - 2,000", 1000, 2000),
    "2000-5000": ("2,000 - 5,000", 2000, 5000),
    "5000-10000": ("5,000 - 10,000", 5000, 10000),
    "gt10000": ("Above 10,000", 10000, None),
}

CANDIDATE_COL_INDEX = 14  # column O
ROW_MIN_LEN = CANDIDATE_COL_INDEX + 1


def _split_suffix(index):
    """
    Matches the real invoice-numbering convention used by mtsBills.py /
    orderSummary.py when a bill is split: each part is the original invoice
    number + a single lowercase letter (a, b, c, ... TS2026AA0111 -> 'a' for
    part 1, 'b' for part 2, etc.) — never uppercase, never double letters.
    Caps at 26 parts, matching record_room_lookup.SPLIT_PART_LETTERS.
    """
    letters = string.ascii_lowercase
    if index >= len(letters):
        raise ValueError(f"Split leg index {index} exceeds the 26-letter a-z convention")
    return letters[index]


def _to_float(value, default=0.0):
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", "")
    if s == "":
        return default
    try:
        return float(s)
    except ValueError:
        return default


def _sum_pipe_values(value):
    """Parses a cell that may be a single number or a '|'-delimited list of
    numbers (as seen in some legacy rows of column K) and returns the sum.
    """
    if value is None:
        return 0.0
    s = str(value).strip()
    if s == "":
        return 0.0
    if "|" in s:
        return sum(_to_float(p) for p in s.split("|") if p.strip() != "")
    return _to_float(value)


def parse_date(value):
    """Best-effort parse of the Date cell into a datetime.date, or None."""
    if value is None:
        return None
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    s = str(value).strip()
    if not s:
        return None
    for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%m/%d/%Y", "%d.%m.%Y"):
        try:
            return datetime.datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def load_invoices(filepath, include_all=False):
    """
    Parses the sheet, keeps only the rows that belong on the dashboard
    (column O == "Candidate" when that column has data anywhere in the
    sheet, else falls back to column I == "unpaid"), applies the split
    logic on column J (amount) and column L (taxable amount), and returns
    a flat list of invoice-line dicts:
        {invoice_id, original_invoice_id, date (date obj), date_str,
         customer, amount, margin, taxable_amount, split, is_candidate_row,
         raw_status}
    No party metadata / status-override / filtering is applied here.

    include_all=True skips the candidate/unpaid filter entirely (returns
    every row, paid or not) - used by migration.py to import full history
    into the database. Each line still carries is_candidate_row/raw_status
    so the caller can set the DB row's status/is_candidate correctly.
    """
    wb = openpyxl.load_workbook(filepath, data_only=True)
    ws = wb.active

    all_rows = list(ws.iter_rows(min_row=2, values_only=True))

    use_candidate_filter = any(
        row is not None
        and len(row) > CANDIDATE_COL_INDEX
        and row[CANDIDATE_COL_INDEX] not in (None, "")
        for row in all_rows
    )

    invoices = []

    for row in all_rows:
        if row is None or len(row) < 8:
            continue
        if len(row) < ROW_MIN_LEN:
            row = list(row) + [None] * (ROW_MIN_LEN - len(row))

        date_val = row[2]
        inv_no = row[3]
        customer_id_val = row[4]
        customer = row[5]
        total_amount = row[6]
        margin = row[7]
        payment_status = row[8]
        payment_breakup = row[9]
        taxable_total_raw = row[10]
        taxable_breakup_raw = row[11]
        candidate_flag = row[CANDIDATE_COL_INDEX]
        file_key = str(row[0]).strip() if row[0] not in (None, "") else ""

        if inv_no is None and customer is None:
            continue

        if use_candidate_filter:
            is_included = (
                candidate_flag is not None
                and str(candidate_flag).strip().lower() == "candidate"
            )
        else:
            status = str(payment_status).strip().lower() if payment_status is not None else ""
            is_included = status == "unpaid"

        if not is_included and not include_all:
            continue

        raw_status = str(payment_status).strip().lower() if payment_status is not None else "unpaid"
        if raw_status not in ("paid", "unpaid"):
            raw_status = "unpaid"

        customer_name = str(customer).strip() if customer is not None else "Unknown Customer"
        inv_no_str = str(inv_no).strip() if inv_no is not None else ""
        amount = _to_float(total_amount)
        margin_val = _to_float(margin)
        taxable_total = _sum_pipe_values(taxable_total_raw)

        parsed_date = parse_date(date_val)
        date_str = parsed_date.strftime("%d/%m/%Y") if parsed_date else (str(date_val) if date_val else "")

        breakup_str = str(payment_breakup).strip() if payment_breakup not in (None, "") else ""
        parts_raw = [p.strip() for p in breakup_str.split("|") if p.strip() != ""] if breakup_str else []

        taxable_breakup_str = str(taxable_breakup_raw).strip() if taxable_breakup_raw not in (None, "") else ""
        taxable_parts_raw = (
            [p.strip() for p in taxable_breakup_str.split("|") if p.strip() != ""]
            if taxable_breakup_str else []
        )

        if parts_raw:
            parts = [_to_float(p) for p in parts_raw]
            total_breakup = sum(parts)

            # Prefer the explicit column-L breakup when it has the same
            # number of legs as the column-J amount breakup. Otherwise fall
            # back to splitting the taxable total proportionally, the same
            # way margin is split today.
            if taxable_parts_raw and len(taxable_parts_raw) == len(parts):
                taxable_leg_values = [_to_float(p) for p in taxable_parts_raw]
            elif taxable_total:
                taxable_leg_values = [
                    (
                        taxable_total * (part_amount / total_breakup)
                        if total_breakup > 0
                        else (taxable_total / len(parts) if parts else 0.0)
                    )
                    for part_amount in parts
                ]
            else:
                taxable_leg_values = [0.0] * len(parts)

            for i, part_amount in enumerate(parts):
                suffix = _split_suffix(i)
                sub_margin = (
                    margin_val * (part_amount / total_breakup)
                    if total_breakup > 0
                    else (margin_val / len(parts) if parts else 0.0)
                )
                invoices.append({
                    "invoice_id": f"{inv_no_str}{suffix}",
                    "original_invoice_id": inv_no_str,
                    "file_key": file_key,
                    "customer_id": str(customer_id_val).strip() if customer_id_val not in (None, "") else "",
                    "date": parsed_date,
                    "date_str": date_str,
                    "customer": customer_name,
                    "amount": round(part_amount, 2),
                    "margin": round(sub_margin, 2),
                    "taxable_amount": round(taxable_leg_values[i], 2),
                    "split": True,
                    "is_candidate_row": is_included,
                    "raw_status": raw_status,
                })
        else:
            invoices.append({
                "invoice_id": inv_no_str,
                "original_invoice_id": inv_no_str,
                "file_key": file_key,
                "customer_id": str(customer_id_val).strip() if customer_id_val not in (None, "") else "",
                "date": parsed_date,
                "date_str": date_str,
                "customer": customer_name,
                "amount": round(amount, 2),
                "margin": round(margin_val, 2),
                "taxable_amount": round(taxable_total, 2),
                "split": False,
                "is_candidate_row": is_included,
                "raw_status": raw_status,
            })

    return invoices


def enrich_invoices(invoices, overrides, parties):
    """Attach status (with override applied), payment date, and party metadata
    to each invoice line.
    overrides: {invoice_id: {"status": "paid"|"unpaid", "payment_date": "YYYY-MM-DD"|None}}
    """
    enriched = []
    for inv in invoices:
        e = dict(inv)
        override = overrides.get(inv["invoice_id"])
        status = override["status"] if override else "unpaid"
        raw_payment_date = override.get("payment_date") if override else None

        payment_date_str = ""
        if raw_payment_date:
            try:
                payment_date_str = datetime.datetime.strptime(
                    raw_payment_date, "%Y-%m-%d"
                ).strftime("%d/%m/%Y")
            except ValueError:
                payment_date_str = raw_payment_date

        e["status"] = status
        e["payment_date"] = raw_payment_date or ""
        e["payment_date_str"] = payment_date_str
        party = parties.get(inv["customer"], {})
        e["contact_number"] = party.get("contact_number", "") or ""
        e["group_by"] = party.get("group_by", "") or ""
        e["location"] = party.get("location", "") or ""
        e["coordinator"] = party.get("coordinator", "") or ""
        enriched.append(e)
    return enriched


def apply_filters(invoices, filters):
    """
    filters: dict with optional keys:
        status ('unpaid' | 'paid' | 'all')
        date_from, date_to (datetime.date or None)
        coordinator (str or 'all')
        group_by (str or 'all')
        amount_range (key from AMOUNT_RANGES or 'all')
        q (customer search substring, lowercase)
    """
    status = filters.get("status", "unpaid")
    date_from = filters.get("date_from")
    date_to = filters.get("date_to")
    coordinator = filters.get("coordinator", "all")
    group_by = filters.get("group_by", "all")
    amount_range = filters.get("amount_range", "all")
    q = (filters.get("q") or "").strip().lower()

    lo = hi = None
    if amount_range and amount_range != "all" and amount_range in AMOUNT_RANGES:
        _, lo, hi = AMOUNT_RANGES[amount_range]

    result = []
    for inv in invoices:
        if status != "all" and inv["status"] != status:
            continue
        if date_from and (not inv["date"] or inv["date"] < date_from):
            continue
        if date_to and (not inv["date"] or inv["date"] > date_to):
            continue
        if coordinator != "all" and inv["coordinator"] != coordinator:
            continue
        if group_by != "all" and inv["group_by"] != group_by:
            continue
        if lo is not None and inv["amount"] < lo:
            continue
        if hi is not None and inv["amount"] >= hi:
            continue
        if q and q not in inv["customer"].lower():
            continue
        result.append(inv)
    return result


def group_by_customer(invoices):
    customers = {}
    for inv in invoices:
        c = customers.setdefault(inv["customer"], {
            "customer": inv["customer"],
            "total_amount": 0.0,
            "total_margin": 0.0,
            "total_taxable": 0.0,
            "invoice_count": 0,
            "invoices": [],
            "coordinator": inv.get("coordinator", ""),
            "group_by": inv.get("group_by", ""),
            "location": inv.get("location", ""),
            "contact_number": inv.get("contact_number", ""),
        })
        c["total_amount"] += inv["amount"]
        c["total_margin"] += inv["margin"]
        c["total_taxable"] += inv.get("taxable_amount", 0.0)
        c["invoice_count"] += 1
        c["invoices"].append(inv)

    for c in customers.values():
        c["total_amount"] = round(c["total_amount"], 2)
        c["total_margin"] = round(c["total_margin"], 2)
        c["total_taxable"] = round(c["total_taxable"], 2)
        c["invoices"].sort(key=lambda x: (x["original_invoice_id"], x["invoice_id"]))

    customer_list = sorted(customers.values(), key=lambda x: x["total_amount"], reverse=True)
    return customer_list


def compute_totals(invoices, customer_count=None):
    total_amount = round(sum(i["amount"] for i in invoices), 2)
    total_margin = round(sum(i["margin"] for i in invoices), 2)
    total_taxable = round(sum(i.get("taxable_amount", 0.0) for i in invoices), 2)
    if customer_count is None:
        customer_count = len({i["customer"] for i in invoices})
    return {
        "total_amount": total_amount,
        "total_margin": total_margin,
        "total_taxable": total_taxable,
        "invoice_count": len(invoices),
        "customer_count": customer_count,
    }
