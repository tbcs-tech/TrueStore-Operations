"""
record_room_lookup.py
======================
Shared helpers for reading bill data out of the record_room folder tree.
Import this module wherever you need to look up an invoice's taxable
amount or item-wise margin instead of re-implementing the path/parsing
logic (fill_taxable_columns.py uses it; orderSummary.py / mtsBills.py can
import it too so new bills can carry this info at creation time instead of
needing a separate fix-up pass).

record_room layout on disk:
    record_room/
        <mm_dd_yyyy>/                                <- from the bill's Date
            <Invoice#>_<ddmmyyyy>.json                <- NOT split: full bill data
            <Invoice#>_<ddmmyyyy>_CostReport.json      <- item -> cost price map
                                                            (ALWAYS at this top level,
                                                             even for split bills -
                                                             cost data is invoice-wide)
            <Invoice#>_<ddmmyyyy>/                      <- split bill -> folder
                <Invoice#>a_<ddmmyyyy>.json             <- split bill, part 1
                <Invoice#>b_<ddmmyyyy>.json             <- split bill, part 2
                ...

Bill-item json fields (per item line, "a".."z","1".."9" - see ITEM_LETTERS):
    item<L>   item description (also the key used in the CostReport json)
    q<L>      quantity
    w<L>      taxable rate per unit (excl. GST)
    r<L>      selling rate per unit (used for margin)
    a<L>      line amount (= w<L> * q<L>, excl. GST)

Whole-bill fields used here:
    ttaxamt   total taxable amount (excl. GST) for the whole bill/part
"""

import os
import json
import string
import datetime

# --------------------------------------------------------------------------- #
# Letters used for item lines within a single bill json (skips m, n, w -
# those are used for MRP / rate fields). Taken directly from
# orderSummary.py's itemLineCoRelation.
# --------------------------------------------------------------------------- #
ITEM_LETTERS = (
    list("abcdefghijkl") + ["o", "p"] + list("qrstuv") + ["x", "y", "z"]
    + [str(d) for d in range(1, 10)]
)

# Letters used to name split-PART files (TS...a_..., TS...b_..., always
# plain sequential a, b, c, ... - unrelated to ITEM_LETTERS above).
SPLIT_PART_LETTERS = string.ascii_lowercase


# --------------------------------------------------------------------------- #
# Case-insensitive path resolution
#
# All of this module's path-building is exact-case string concatenation
# followed by os.path.exists()/os.listdir() checks. That was silently safe
# on Windows (case-insensitive filesystem) but breaks the moment an invoice
# number is typed/stored with different casing somewhere along the chain
# (Excel entry vs. the string used at file-creation time) and this runs on
# a case-sensitive filesystem. _ci() resolves a path case-insensitively
# against what's actually on disk, falling back to the exact path (so
# error messages / "file not found" behaviour is unchanged when there's
# genuinely no match).
# --------------------------------------------------------------------------- #
def _ci(path):
    if os.path.exists(path):
        return path
    parent, target = os.path.dirname(path), os.path.basename(path)
    if not os.path.isdir(parent):
        return path
    target_lower = target.lower()
    for entry in os.listdir(parent):
        if entry.lower() == target_lower:
            return os.path.join(parent, entry)
    return path


# --------------------------------------------------------------------------- #
# Date / path helpers
# --------------------------------------------------------------------------- #
def normalize_date(date_val):
    """
    Accepts either a 'dd/mm/yyyy' string or a datetime/date object (openpyxl
    sometimes hands back real date objects if the cell is formatted as a
    date). Returns dd, mm, yyyy as zero-padded strings.
    """
    if isinstance(date_val, (datetime.datetime, datetime.date)):
        dd = f"{date_val.day:02d}"
        mm = f"{date_val.month:02d}"
        yyyy = f"{date_val.year:04d}"
    else:
        dd, mm, yyyy = str(date_val).strip().split("/")
        dd = dd.zfill(2)
        mm = mm.zfill(2)
        yyyy = yyyy.zfill(4)
    return dd, mm, yyyy


def local_folder_name(date_val):
    """mm_dd_yyyy folder name that sits directly under record_room/"""
    dd, mm, yyyy = normalize_date(date_val)
    return f"{mm}_{dd}_{yyyy}"


def ddmmyyyy(date_val):
    dd, mm, yyyy = normalize_date(date_val)
    return f"{dd}{mm}{yyyy}"


def is_split_row(total_breakup_value):
    """Column J non-empty (and not just whitespace) => split bill."""
    return total_breakup_value is not None and str(total_breakup_value).strip() != ""


def split_part_count(total_breakup_value):
    """Number of '|' separated pieces in column J."""
    return len(str(total_breakup_value).split("|"))


def locate_invoice_json_paths(base_address, invoice, date_val, split, n_parts=None):
    """
    Returns a list of json file paths holding the bill's item/tax data.
      - non-split bill -> list with a single path
      - split bill      -> list with one path per part (a, b, c, ...)
    Every path segment derived from `invoice` is resolved case-insensitively
    against what's actually on disk (see _ci above).
    """
    folder = local_folder_name(date_val)
    dmy = ddmmyyyy(date_val)
    file_stub = f"{invoice}_{dmy}"                     # matches column A

    record_room_dir = _ci(os.path.join(base_address, "record_room", folder))

    if not split:
        return [_ci(os.path.join(record_room_dir, file_stub + ".json"))]

    inner_folder = _ci(os.path.join(record_room_dir, file_stub))
    paths = []
    for i in range(n_parts):
        letter = SPLIT_PART_LETTERS[i]
        part_file = f"{invoice}{letter}_{dmy}.json"
        paths.append(_ci(os.path.join(inner_folder, part_file)))
    return paths


def locate_cost_report_path(base_address, invoice, date_val):
    """
    The CostReport json always sits next to the (non-split) invoice json,
    named after the FULL invoice (never a split-part letter) - cost data is
    invoice-wide, not per split part. Resolved case-insensitively.
    """
    folder = local_folder_name(date_val)
    dmy = ddmmyyyy(date_val)
    file_stub = f"{invoice}_{dmy}"
    record_room_dir = _ci(os.path.join(base_address, "record_room", folder))
    return _ci(os.path.join(record_room_dir, file_stub + "_CostReport.json"))


# Public alias - other modules in this project (invoice_files.py) reuse the
# same case-insensitive resolution for paths this module doesn't natively
# handle (e.g. the sibling .pdf next to a bill .json).
resolve_case_insensitive = _ci


# --------------------------------------------------------------------------- #
# JSON reading
# --------------------------------------------------------------------------- #
def read_json_file(path):
    """Returns (data, error). data is None if error is set."""
    if not os.path.exists(path):
        return None, "FILE NOT FOUND"
    try:
        with open(path, "r") as f:
            return json.load(f), None
    except Exception as e:
        return None, f"JSON READ ERROR: {e}"


def read_ttaxamt(json_path):
    """Read the 'ttaxamt' field out of a single bill json. Returns (value, error)."""
    data, err = read_json_file(json_path)
    if err:
        return None, err
    if "ttaxamt" not in data:
        return None, "KEY 'ttaxamt' NOT IN JSON"
    return data["ttaxamt"], None


def read_cost_report(json_path):
    """Read a CostReport json ({item_name: cost_price, ...}). Returns (dict, error)."""
    data, err = read_json_file(json_path)
    if err:
        return None, err
    return data, None


# --------------------------------------------------------------------------- #
# Margin calculation
#   margin per item = qty * (sell_rate - cost_price)     <- matches
#   get_margin_base() in orderSummary.py, verified against real data
#   (TS2026AA0016 -> computed 6092, matches column H exactly)
# --------------------------------------------------------------------------- #
def compute_margin_for_bill_json(bill_json, cost_report):
    """
    Sums qty * (r<L> - cost[item<L>]) across every item line present in
    bill_json. Returns (margin_total, errors) where errors is a list of
    strings (empty if everything resolved cleanly).
    """
    margin_total = 0.0
    errors = []

    for letter in ITEM_LETTERS:
        item_key = f"item{letter}"
        if item_key not in bill_json or bill_json[item_key] in (None, ""):
            continue

        item_name = bill_json[item_key]
        qty_key = f"q{letter}"
        rate_key = f"r{letter}"

        if qty_key not in bill_json or rate_key not in bill_json:
            errors.append(f"line '{letter}' ({item_name}): missing qty/rate field in bill json")
            continue

        if item_name not in cost_report:
            errors.append(f"line '{letter}' ({item_name}): no cost price in CostReport json")
            continue

        try:
            qty = float(bill_json[qty_key])
            rate = float(bill_json[rate_key])
            cost = float(cost_report[item_name])
        except (TypeError, ValueError) as e:
            errors.append(f"line '{letter}' ({item_name}): bad numeric value ({e})")
            continue

        margin_total += qty * (rate - cost)

    return round(margin_total, 2), errors


def get_margin_for_single_bill(base_address, invoice, date_val):
    """
    For a NON-split bill: reads the one bill json plus the shared CostReport
    json and computes the item-wise margin the same way split parts do.

    Returns dict with:
        bill_json_path : path to the bill json that was read
        cost_report_path : path to the CostReport json that was read
        margin          : float, or None if unresolved
        errors          : list of error strings (empty if fully resolved)
    """
    bill_path = locate_invoice_json_paths(base_address, invoice, date_val, split=False)[0]
    cost_path = locate_cost_report_path(base_address, invoice, date_val)

    bill_json, bill_err = read_json_file(bill_path)
    cost_report, cost_err = read_cost_report(cost_path)

    errors = []
    if bill_err:
        errors.append(f"{bill_path}: {bill_err}")
    if cost_err:
        errors.append(f"{cost_path}: {cost_err}")

    if errors:
        return {
            "bill_json_path": bill_path,
            "cost_report_path": cost_path,
            "margin": None,
            "errors": errors,
        }

    margin, item_errors = compute_margin_for_bill_json(bill_json, cost_report)
    if item_errors:
        errors.extend(f"{bill_path}: {e}" for e in item_errors)
        margin = None

    return {
        "bill_json_path": bill_path,
        "cost_report_path": cost_path,
        "margin": margin,
        "errors": errors,
    }


def get_margin_for_split_parts(base_address, invoice, date_val, part_paths):
    """
    For a SPLIT bill: reads the one shared CostReport json plus every split
    part's bill json, and returns per-part margins.

    Returns dict with:
        cost_report      : {item_name: cost} or None if unreadable
        cost_report_error: error string or None
        part_margins      : list (same order as part_paths), one float per part
                             (None for a part that could not be resolved)
        part_errors       : list of error-string-lists, one per part
    """
    cost_path = locate_cost_report_path(base_address, invoice, date_val)
    cost_report, cost_err = read_cost_report(cost_path)

    part_margins = []
    part_errors = []

    for p in part_paths:
        bill_json, err = read_json_file(p)
        if err:
            part_margins.append(None)
            part_errors.append([err])
            continue
        if cost_err:
            part_margins.append(None)
            part_errors.append([f"CostReport unreadable ({cost_path}): {cost_err}"])
            continue

        margin, errs = compute_margin_for_bill_json(bill_json, cost_report)
        part_margins.append(margin if not errs else None)
        part_errors.append(errs)

    return {
        "cost_report_path": cost_path,
        "cost_report": cost_report,
        "cost_report_error": cost_err,
        "part_margins": part_margins,
        "part_errors": part_errors,
    }
