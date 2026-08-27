"""
bills_repo.py
=============
DB-backed replacement for data_processor.load_invoices() + enrich_invoices().
Returns invoice-line dicts in the exact same shape the Excel-based pipeline
used, so data_processor.apply_filters() / group_by_customer() /
compute_totals() keep working unchanged - only the *source* of the data
changes (bills table instead of sales_log.xlsx).

Status and payment_date now live directly on the bill row (this was the
whole point of the migration - no more separate override table to keep in
sync with an Excel file).
"""
import db
from data_processor import parse_date


def load_bills():
    """Every candidate (dashboard-visible) bill, un-enriched."""
    bills = db.list_bills()
    invoices = []
    for b in bills:
        if not b["is_candidate"]:
            continue
        parsed_date = parse_date(b["date"])
        invoices.append({
            "bill_id": b["id"],
            "invoice_id": b["invoice_no"],
            "original_invoice_id": b["original_invoice_no"],
            "file_key": b["file_name"],
            "date": parsed_date,
            "date_str": b["date"] or "",
            "customer": b["customer_name"],
            "customer_id": b["customer_id"],
            "amount": round(b["total"], 2),
            "margin": round(b["margin"], 2),
            "taxable_amount": round(b["taxable_total"], 2),
            "split": bool(b["split_leg"]),
            "status": b["status"],
            "payment_date": b["payment_date"] or "",
        })
    return invoices


def _format_payment_date(raw_iso):
    if not raw_iso:
        return ""
    try:
        import datetime
        return datetime.datetime.strptime(raw_iso, "%Y-%m-%d").strftime("%d/%m/%Y")
    except ValueError:
        return raw_iso


def enrich_bills(invoices, parties):
    """Attaches party metadata + a display-formatted payment_date_str. Status/
    payment_date are already correct on each dict from load_bills()."""
    enriched = []
    for inv in invoices:
        e = dict(inv)
        e["payment_date_str"] = _format_payment_date(inv["payment_date"])
        party = parties.get(inv["customer"], {})
        e["contact_number"] = party.get("contact_number", "") or ""
        e["group_by"] = party.get("group_by", "") or ""
        e["location"] = party.get("location", "") or ""
        e["coordinator"] = party.get("coordinator", "") or ""
        enriched.append(e)
    return enriched


def get_all_enriched(parties=None):
    if parties is None:
        parties = db.get_all_parties()
    return enrich_bills(load_bills(), parties)
