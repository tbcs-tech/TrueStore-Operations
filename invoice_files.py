"""
Locates and parses the PDF/JSON backing an invoice line shown on the
dashboard.

Two lookup strategies, tried in order:

1. Real record_room tree (matches mtsBills.py / orderSummary.py exactly):
       record_room/<mm_dd_yyyy>/<Invoice>_<ddmmyyyy>.json           (non-split)
       record_room/<mm_dd_yyyy>/<Invoice>_<ddmmyyyy>/
           <Invoice><letter>_<ddmmyyyy>.json                        (split, one per part)
   Split-part letters are lowercase a, b, c, ... — matching the real
   invoice-numbering convention (see record_room_lookup.py and
   data_processor._split_suffix). All path segments are resolved
   case-insensitively via record_room_lookup, since invoice-number casing
   has been observed to drift between where a bill was created and where
   it's later looked up.
   PDFs live next to their .json sibling with the same stem (this module
   derives the .pdf path from the .json path since record_room_lookup.py
   only handles json/CostReport lookups).

2. Flat data/invoices/{fileName}.json / .pdf pair — a simpler fallback for
   setups that don't have a full record_room tree (e.g. a handful of
   invoices dropped in directly, keyed by column A "fileName").

JSON parsing: lettered fields per line item, e.g. for item "a": itema
(description), hsa (HSN/SAC), ma (MRP), qa (qty), wa (per-unit taxable
value), ga (GST %), ra (rate), aa (line amount) - repeated for item b, c...
"""
import json
import os
import string

import record_room_lookup as rrl

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LEGACY_INVOICES_DIR = os.path.join(BASE_DIR, "data", "invoices")
RECORD_ROOM_BASE = BASE_DIR  # record_room/ is expected directly under the project


def _safe_component(value):
    """Prevents path traversal; only allow plausible fileName/invoice characters."""
    if not value:
        return None
    if "/" in value or "\\" in value or ".." in value:
        return None
    return value


def _legacy_path(file_key, ext):
    key = _safe_component(file_key)
    if not key:
        return None
    path = os.path.join(LEGACY_INVOICES_DIR, f"{key}.{ext}")
    return path if os.path.exists(path) else None


def _record_room_json_path(inv):
    """Returns the record_room .json path for this invoice line, or None."""
    original = inv.get("original_invoice_id") or ""
    date_val = inv.get("date")
    if not _safe_component(original) or not date_val:
        return None

    try:
        if inv.get("split"):
            leg = inv.get("invoice_id", "")[len(original):]
            if leg not in rrl.SPLIT_PART_LETTERS:
                return None
            leg_index = rrl.SPLIT_PART_LETTERS.index(leg)
            paths = rrl.locate_invoice_json_paths(
                RECORD_ROOM_BASE, original, date_val, split=True, n_parts=leg_index + 1
            )
            path = paths[leg_index]
        else:
            path = rrl.locate_invoice_json_paths(
                RECORD_ROOM_BASE, original, date_val, split=False
            )[0]
    except Exception:
        return None

    return path if os.path.exists(path) else None


def _record_room_pdf_path(inv):
    """PDF lives next to its .json sibling with the same stem."""
    json_path = _record_room_json_path(inv)
    if not json_path or not json_path.endswith(".json"):
        return None
    pdf_path = rrl.resolve_case_insensitive(json_path[: -len(".json")] + ".pdf")
    return pdf_path if os.path.exists(pdf_path) else None


def find_json_path(inv):
    return _record_room_json_path(inv) or _legacy_path(inv.get("file_key"), "json")


def find_pdf_path(inv):
    return _record_room_pdf_path(inv) or _legacy_path(inv.get("file_key"), "pdf")


def _num(value):
    if value is None:
        return None
    s = str(value).strip()
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_invoice_json_at(path):
    """
    Parses a bill json already located at `path`. Returns None if the file
    doesn't exist. See module docstring for the expected field layout.
    """
    if not path or not os.path.exists(path):
        return None

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)

    line_items = []
    for letter in rrl.ITEM_LETTERS:
        desc_key = f"item{letter}"
        if desc_key not in data or not str(data.get(desc_key) or "").strip():
            continue
        line_items.append({
            "description": data.get(desc_key, ""),
            "hsn": data.get(f"hs{letter}", ""),
            "mrp": data.get(f"m{letter}", ""),
            "qty": data.get(f"q{letter}", ""),
            "gst_pct": data.get(f"g{letter}", ""),
            "rate": data.get(f"r{letter}", ""),
            "amount": data.get(f"a{letter}", ""),
        })

    hsn_list = [x.strip() for x in str(data.get("hsn", "")).split("\n") if x.strip()]
    gst_list = [x.strip() for x in str(data.get("gsh", "")).split("\n") if x.strip()]
    tax_list = [x.strip() for x in str(data.get("hsnc_tax", "")).split("\n") if x.strip()]
    hsn_summary = []
    for i in range(max(len(hsn_list), len(gst_list), len(tax_list))):
        hsn_summary.append({
            "hsn": hsn_list[i] if i < len(hsn_list) else "",
            "gst_pct": gst_list[i] if i < len(gst_list) else "",
            "taxable_amt": tax_list[i] if i < len(tax_list) else "",
        })

    return {
        "invoice_no": data.get("Invoice", ""),
        "date": data.get("Date", ""),
        "customer_name": data.get("customerName", ""),
        "customer_details": data.get("CustomerDetails", ""),
        "customer_gstn": data.get("customerGSTN", ""),
        "customer_id": data.get("customerID", ""),
        "line_items": line_items,
        "hsn_summary": hsn_summary,
        "taxable_total": _num(data.get("ttaxamt")),
        "cgst": _num(data.get("cgst")),
        "sgst": _num(data.get("sgst")),
        "grand_total": _num(data.get("total")),
        "round_off": data.get("roff", ""),
        "amount_in_words": data.get("finalAmountWord", ""),
    }


def parse_invoice_json_for(inv):
    """Convenience: locate + parse in one call for an enriched invoice-line dict."""
    return parse_invoice_json_at(find_json_path(inv))
