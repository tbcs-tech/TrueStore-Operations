"""
bill_pdf.py
===========
Professional branded "True Store" tax invoice PDF using fpdf2.

Layout: A4 portrait with a dark-navy header band carrying the business name
and invoice metadata, a customer details block, a clean line-item table with
alternating row shading, an HSN summary block, a prominent totals box on the
right, amount in words, and a footer with bank details + signature lines.

The business profile (name, GSTIN, address, bank details, phone, state code)
is read from `db.get_business_profile()` — admin configures it once via
Settings. Falls back to sensible placeholders if not yet configured.

All colors, fonts, and spacing are defined as constants at the top for easy
brand tuning.
"""
from fpdf import FPDF
import db as _db

# ── Brand palette ──────────────────────────────────────────────────────────
NAVY       = (20, 33, 61)       # header band, accent lines
DARK_GRAY  = (55, 55, 55)       # body text
MID_GRAY   = (120, 120, 120)    # secondary text
LIGHT_BG   = (245, 247, 250)    # alternating row fill
WHITE      = (255, 255, 255)
ACCENT     = (0, 102, 153)      # totals highlight
BORDER_CLR = (200, 200, 200)    # table borders

# ── Column widths (mm, must sum to ~174 for A4 with 18mm margins) ─────────
#   Description  HSN   Qty   MRP   GST%  Rate   Amount
ITEM_COLS = [62, 18, 14, 20, 14, 22, 24]
ITEM_HDRS = ["Description", "HSN", "Qty", "MRP", "GST %", "Rate", "Amount"]
PAGE_W = 174  # usable width


def _safe(text):
    return str(text if text is not None else "").encode("latin-1", "replace").decode("latin-1")


def _get_profile():
    """Business profile from DB settings, with safe fallbacks."""
    try:
        p = _db.get_business_profile()
        if p:
            return p
    except Exception:
        pass
    return {
        "business_name": "TRUE STORE",
        "tagline": "Complete Office Solutions",
        "gstin": "",
        "address_line1": "",
        "address_line2": "",
        "state": "Jharkhand",
        "state_code": "20",
        "phone": "",
        "email": "",
        "bank_name": "",
        "bank_account": "",
        "bank_ifsc": "",
        "bank_branch": "",
    }


class InvoicePDF(FPDF):
    """Custom PDF with branded header/footer."""

    def __init__(self, profile, copy_label=None):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.profile = profile
        self.copy_label = copy_label
        self.set_auto_page_break(auto=True, margin=22)

    # ── Header band ────────────────────────────────────────────────────────
    def header(self):
        p = self.profile
        # Navy band
        self.set_fill_color(*NAVY)
        self.rect(0, 0, 210, 34, "F")

        # Business name
        self.set_text_color(*WHITE)
        self.set_font("Helvetica", "B", 20)
        self.set_xy(18, 6)
        self.cell(120, 8, _safe(p.get("business_name", "TRUE STORE")))

        # Tagline
        tagline = p.get("tagline", "")
        if tagline:
            self.set_font("Helvetica", "", 9)
            self.set_xy(18, 15)
            self.cell(120, 5, _safe(tagline))

        # GSTIN in header
        gstin = p.get("gstin", "")
        if gstin:
            self.set_font("Helvetica", "", 8)
            self.set_xy(18, 22)
            self.cell(120, 5, f"GSTIN: {_safe(gstin)}")

        # Right side: contact
        self.set_font("Helvetica", "", 8)
        phone = p.get("phone", "")
        state = p.get("state", "")
        state_code = p.get("state_code", "")
        right_text = []
        if phone:
            right_text.append(f"Ph: {phone}")
        if state:
            sc = f" (Code: {state_code})" if state_code else ""
            right_text.append(f"{state}{sc}")
        for i, line in enumerate(right_text):
            self.set_xy(140, 8 + i * 5)
            self.cell(52, 5, _safe(line), align="R")

        # Copy label (Original / Duplicate)
        if self.copy_label:
            self.set_font("Helvetica", "I", 7)
            self.set_xy(140, 26)
            self.cell(52, 4, _safe(self.copy_label), align="R")

        # Reset
        self.set_text_color(*DARK_GRAY)
        self.set_y(38)

    # ── Footer ─────────────────────────────────────────────────────────────
    def footer(self):
        self.set_y(-18)
        self.set_draw_color(*BORDER_CLR)
        self.line(18, self.get_y(), 192, self.get_y())
        self.set_font("Helvetica", "I", 7)
        self.set_text_color(*MID_GRAY)
        addr = self.profile.get("address_line1", "")
        if addr:
            self.cell(0, 4, _safe(addr), ln=1, align="C")
        self.cell(0, 4, f"Page {self.page_no()}/{{nb}}", align="C")


def build_invoice_pdf(final_data, lines, hsn_summary):
    """Build a professional branded tax invoice PDF. Returns bytes."""
    profile = _get_profile()
    pdf = InvoicePDF(profile)
    pdf.alias_nb_pages()
    pdf.add_page()
    lm = 18  # left margin
    pdf.set_left_margin(lm)
    pdf.set_right_margin(18)

    # ── Document title ─────────────────────────────────────────────────────
    pdf.set_font("Helvetica", "B", 14)
    pdf.set_text_color(*NAVY)
    pdf.cell(PAGE_W, 8, "TAX INVOICE", ln=1, align="C")
    pdf.set_draw_color(*NAVY)
    pdf.line(lm, pdf.get_y(), lm + PAGE_W, pdf.get_y())
    pdf.ln(3)

    # ── Invoice meta (left) + Date/Invoice (right) ─────────────────────────
    y_meta = pdf.get_y()
    pdf.set_font("Helvetica", "", 9)
    pdf.set_text_color(*DARK_GRAY)

    # Left: Bill To
    pdf.set_font("Helvetica", "B", 9)
    pdf.cell(90, 5, "Bill To:", ln=1)
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(90, 5, _safe(final_data.get("customerName")), ln=1)
    addr = final_data.get("CustomerDetails", "")
    if addr:
        pdf.cell(90, 5, _safe(addr), ln=1)
    gstn = final_data.get("customerGSTN", "")
    if gstn and gstn != "Unregistered":
        pdf.cell(90, 5, f"GSTIN: {_safe(gstn)}", ln=1)
    contact = final_data.get("customerContNum", "")
    if contact:
        pdf.cell(90, 5, f"Contact: {_safe(contact)}", ln=1)

    # Right: Invoice # and Date box
    box_x = lm + 110
    box_w = 64
    pdf.set_xy(box_x, y_meta)
    pdf.set_fill_color(*LIGHT_BG)
    pdf.set_draw_color(*BORDER_CLR)
    pdf.rect(box_x, y_meta, box_w, 22, "FD")

    pdf.set_xy(box_x + 2, y_meta + 2)
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(*MID_GRAY)
    pdf.cell(30, 5, "Invoice No.")
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_text_color(*NAVY)
    pdf.cell(30, 5, _safe(final_data.get("Invoice")), ln=1)

    pdf.set_xy(box_x + 2, y_meta + 11)
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(*MID_GRAY)
    pdf.cell(30, 5, "Date")
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(*DARK_GRAY)
    pdf.cell(30, 5, _safe(final_data.get("Date")))

    pdf.set_y(max(pdf.get_y() + 5, y_meta + 28))
    pdf.set_text_color(*DARK_GRAY)
    pdf.ln(2)

    # ── Line items table ───────────────────────────────────────────────────
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_fill_color(*NAVY)
    pdf.set_text_color(*WHITE)
    for h, w in zip(ITEM_HDRS, ITEM_COLS):
        align = "R" if h in ("Qty", "MRP", "GST %", "Rate", "Amount") else "L"
        pdf.cell(w, 7, h, border=0, fill=True, align=align)
    pdf.ln()

    pdf.set_text_color(*DARK_GRAY)
    pdf.set_font("Helvetica", "", 8)
    for idx, line in enumerate(lines):
        if idx % 2 == 0:
            pdf.set_fill_color(*LIGHT_BG)
            fill = True
        else:
            pdf.set_fill_color(*WHITE)
            fill = True

        row = [
            line["description"],
            str(line.get("hsn", "")),
            f'{line["qty"]:g}',
            str(line.get("mrp") or ""),
            f'{line["gst_pct"]:g}%',
            f'{line["rate"]:,.2f}',
            f'{line["amount"]:,.2f}',
        ]
        for val, w, h in zip(row, ITEM_COLS, ITEM_HDRS):
            align = "R" if h in ("Qty", "MRP", "GST %", "Rate", "Amount") else "L"
            pdf.cell(w, 6, _safe(val), border=0, fill=fill, align=align)
        pdf.ln()

    # Bottom border of table
    pdf.set_draw_color(*NAVY)
    pdf.line(lm, pdf.get_y(), lm + PAGE_W, pdf.get_y())
    pdf.ln(3)

    # ── HSN Summary ────────────────────────────────────────────────────────
    if hsn_summary:
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_text_color(*NAVY)
        pdf.cell(PAGE_W, 5, "HSN/SAC Summary", ln=1)
        pdf.set_draw_color(*BORDER_CLR)

        pdf.set_fill_color(*LIGHT_BG)
        pdf.set_text_color(*DARK_GRAY)
        pdf.set_font("Helvetica", "B", 7)
        pdf.cell(40, 6, "HSN/SAC", border=1, fill=True)
        pdf.cell(30, 6, "GST %", border=1, fill=True, align="R")
        pdf.cell(40, 6, "Taxable Amt", border=1, fill=True, align="R")
        pdf.ln()
        pdf.set_font("Helvetica", "", 7)
        for h in hsn_summary:
            pdf.cell(40, 5, _safe(h["hsn"]), border="LR")
            pdf.cell(30, 5, _safe(f'{h["gst_pct"]}%'), border="LR", align="R")
            pdf.cell(40, 5, _safe(f'{h["taxable_amt"]:,.2f}'), border="LR", align="R")
            pdf.ln()
        pdf.set_draw_color(*BORDER_CLR)
        pdf.line(lm, pdf.get_y(), lm + 110, pdf.get_y())
        pdf.ln(3)

    # ── Totals box (right-aligned) ─────────────────────────────────────────
    totals_x = lm + PAGE_W - 70
    totals_w = 70
    ty = pdf.get_y()

    pdf.set_fill_color(*LIGHT_BG)
    pdf.set_draw_color(*BORDER_CLR)

    def _totals_row(label, value, bold=False):
        nonlocal ty
        pdf.set_xy(totals_x, ty)
        pdf.set_font("Helvetica", "B" if bold else "", 9 if bold else 8)
        pdf.cell(35, 6, label, border="LTB" if bold else "L", fill=not bold)
        pdf.set_font("Helvetica", "B" if bold else "", 9 if bold else 8)
        pdf.cell(35, 6, value, border="RTB" if bold else "R", fill=not bold, align="R")
        ty += 6

    _totals_row("Taxable Amount", f'Rs {final_data.get("ttaxamt", 0):,.2f}')
    _totals_row("CGST", f'Rs {final_data.get("cgst", 0):,.2f}')
    _totals_row("SGST", f'Rs {final_data.get("sgst", 0):,.2f}')

    # Grand total with accent
    pdf.set_xy(totals_x, ty)
    pdf.set_fill_color(*NAVY)
    pdf.set_text_color(*WHITE)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(35, 8, "Grand Total", border=1, fill=True)
    pdf.cell(35, 8, f'Rs {final_data.get("total", 0):,.2f}', border=1, fill=True, align="R")
    pdf.set_text_color(*DARK_GRAY)
    pdf.ln(12)

    # ── Amount in words ────────────────────────────────────────────────────
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(*MID_GRAY)
    pdf.cell(PAGE_W, 5, "Amount in words: " + _safe(final_data.get("finalAmountWord", "")), ln=1)
    pdf.ln(4)

    # ── Bank details ───────────────────────────────────────────────────────
    bank_name = profile.get("bank_name", "")
    if bank_name:
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_text_color(*NAVY)
        pdf.cell(PAGE_W, 5, "Bank Details for Payment", ln=1)
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(*DARK_GRAY)
        pdf.cell(PAGE_W, 4, f'Bank: {_safe(bank_name)}', ln=1)
        acct = profile.get("bank_account", "")
        if acct:
            pdf.cell(PAGE_W, 4, f'A/C No: {_safe(acct)}', ln=1)
        ifsc = profile.get("bank_ifsc", "")
        if ifsc:
            pdf.cell(PAGE_W, 4, f'IFSC: {_safe(ifsc)}', ln=1)
        branch = profile.get("bank_branch", "")
        if branch:
            pdf.cell(PAGE_W, 4, f'Branch: {_safe(branch)}', ln=1)
        pdf.ln(4)

    # ── Signature lines ────────────────────────────────────────────────────
    sig_y = max(pdf.get_y(), 250)
    pdf.set_y(sig_y)
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(*MID_GRAY)
    pdf.cell(87, 5, "Receiver's Signature", align="C")
    pdf.cell(87, 5, f'For {_safe(profile.get("business_name", "TRUE STORE"))}', align="C")
    pdf.ln(2)
    pdf.set_draw_color(*BORDER_CLR)
    pdf.line(lm + 10, sig_y + 7, lm + 77, sig_y + 7)
    pdf.line(lm + 97, sig_y + 7, lm + 164, sig_y + 7)

    return bytes(pdf.output())
