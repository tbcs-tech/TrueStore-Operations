"""
purchase_pdf.py
================
Professional branded purchase receipt PDF matching the bill_pdf.py design
language — same navy header band, same table styling, same totals box.
"""
from fpdf import FPDF
import db as _db

NAVY       = (20, 33, 61)
DARK_GRAY  = (55, 55, 55)
MID_GRAY   = (120, 120, 120)
LIGHT_BG   = (245, 247, 250)
WHITE      = (255, 255, 255)
BORDER_CLR = (200, 200, 200)

ITEM_COLS = [72, 20, 16, 14, 24, 28]
ITEM_HDRS = ["Description", "HSN", "Qty", "GST %", "Cost Rate", "Amount"]
PAGE_W = 174


def _safe(text):
    return str(text if text is not None else "").encode("latin-1", "replace").decode("latin-1")


def _get_profile():
    try:
        p = _db.get_business_profile()
        if p:
            return p
    except Exception:
        pass
    return {"business_name": "TRUE STORE", "tagline": "Complete Office Solutions",
            "gstin": "", "phone": "", "state": "", "state_code": "",
            "address_line1": "", "address_line2": ""}


class PurchasePDF(FPDF):
    def __init__(self, profile):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.profile = profile
        self.set_auto_page_break(auto=True, margin=22)

    def header(self):
        p = self.profile
        self.set_fill_color(*NAVY)
        self.rect(0, 0, 210, 30, "F")
        self.set_text_color(*WHITE)
        self.set_font("Helvetica", "B", 18)
        self.set_xy(18, 6)
        self.cell(120, 8, _safe(p.get("business_name", "TRUE STORE")))
        tagline = p.get("tagline", "")
        if tagline:
            self.set_font("Helvetica", "", 8)
            self.set_xy(18, 16)
            self.cell(120, 5, _safe(tagline))
        self.set_text_color(*DARK_GRAY)
        self.set_y(34)

    def footer(self):
        self.set_y(-14)
        self.set_draw_color(*BORDER_CLR)
        self.line(18, self.get_y(), 192, self.get_y())
        self.set_font("Helvetica", "I", 7)
        self.set_text_color(*MID_GRAY)
        self.cell(0, 4, f"Page {self.page_no()}/{{nb}}", align="C")


def build_purchase_pdf(purchase_data, lines):
    profile = _get_profile()
    pdf = PurchasePDF(profile)
    pdf.alias_nb_pages()
    pdf.add_page()
    lm = 18
    pdf.set_left_margin(lm)
    pdf.set_right_margin(18)

    # Title
    pdf.set_font("Helvetica", "B", 14)
    pdf.set_text_color(*NAVY)
    pdf.cell(PAGE_W, 8, "PURCHASE RECEIPT", ln=1, align="C")
    pdf.set_draw_color(*NAVY)
    pdf.line(lm, pdf.get_y(), lm + PAGE_W, pdf.get_y())
    pdf.ln(3)

    # Meta: Vendor (left), Purchase #/Date (right)
    y_meta = pdf.get_y()
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(*DARK_GRAY)
    pdf.cell(90, 5, "Vendor:", ln=1)
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(90, 5, _safe(purchase_data.get("vendor_name")), ln=1)
    vid = purchase_data.get("vendor_id", "")
    if vid:
        pdf.cell(90, 5, f"Vendor ID: {_safe(vid)}", ln=1)

    box_x = lm + 110
    box_w = 64
    pdf.set_xy(box_x, y_meta)
    pdf.set_fill_color(*LIGHT_BG)
    pdf.set_draw_color(*BORDER_CLR)
    pdf.rect(box_x, y_meta, box_w, 22, "FD")
    pdf.set_xy(box_x + 2, y_meta + 2)
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(*MID_GRAY)
    pdf.cell(30, 5, "Purchase No.")
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_text_color(*NAVY)
    pdf.cell(30, 5, _safe(purchase_data.get("purchase_no")), ln=1)
    pdf.set_xy(box_x + 2, y_meta + 11)
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(*MID_GRAY)
    pdf.cell(30, 5, "Date")
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(*DARK_GRAY)
    pdf.cell(30, 5, _safe(purchase_data.get("date")))

    pdf.set_y(max(pdf.get_y() + 5, y_meta + 28))
    pdf.set_text_color(*DARK_GRAY)
    pdf.ln(2)

    # Table header
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_fill_color(*NAVY)
    pdf.set_text_color(*WHITE)
    for h, w in zip(ITEM_HDRS, ITEM_COLS):
        align = "R" if h != "Description" else "L"
        pdf.cell(w, 7, h, border=0, fill=True, align=align)
    pdf.ln()

    # Table rows
    pdf.set_text_color(*DARK_GRAY)
    pdf.set_font("Helvetica", "", 8)
    for idx, line in enumerate(lines):
        if idx % 2 == 0:
            pdf.set_fill_color(*LIGHT_BG)
        else:
            pdf.set_fill_color(*WHITE)
        row = [
            line["description"], str(line.get("hsn", "")), f'{line["qty"]:g}',
            f'{line["gst_pct"]:g}%', f'{line["cost_rate"]:,.2f}', f'{line["amount"]:,.2f}',
        ]
        for val, w, h in zip(row, ITEM_COLS, ITEM_HDRS):
            align = "R" if h != "Description" else "L"
            pdf.cell(w, 6, _safe(val), border=0, fill=True, align=align)
        pdf.ln()

    pdf.set_draw_color(*NAVY)
    pdf.line(lm, pdf.get_y(), lm + PAGE_W, pdf.get_y())
    pdf.ln(3)

    # Totals box
    totals_x = lm + PAGE_W - 70
    ty = pdf.get_y()

    def _row(label, value, bold=False):
        nonlocal ty
        pdf.set_xy(totals_x, ty)
        pdf.set_font("Helvetica", "B" if bold else "", 9 if bold else 8)
        pdf.set_fill_color(*LIGHT_BG)
        pdf.cell(35, 6, label, border="L", fill=not bold)
        pdf.cell(35, 6, value, border="R", fill=not bold, align="R")
        ty += 6

    _row("Taxable Amount", f'Rs {purchase_data.get("taxable_total", 0):,.2f}')
    _row("CGST", f'Rs {purchase_data.get("cgst", 0):,.2f}')
    _row("SGST", f'Rs {purchase_data.get("sgst", 0):,.2f}')

    pdf.set_xy(totals_x, ty)
    pdf.set_fill_color(*NAVY)
    pdf.set_text_color(*WHITE)
    pdf.set_font("Helvetica", "B", 10)
    pdf.cell(35, 8, "Grand Total", border=1, fill=True)
    pdf.cell(35, 8, f'Rs {purchase_data.get("total", 0):,.2f}', border=1, fill=True, align="R")
    pdf.set_text_color(*DARK_GRAY)
    pdf.ln(12)

    # Amount in words
    pdf.set_font("Helvetica", "I", 8)
    pdf.set_text_color(*MID_GRAY)
    pdf.cell(PAGE_W, 5, "Amount in words: " + _safe(purchase_data.get("amount_in_words", "")), ln=1)
    pdf.ln(8)

    # Signatures
    sig_y = max(pdf.get_y(), 250)
    pdf.set_y(sig_y)
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(*MID_GRAY)
    pdf.cell(87, 5, "Vendor Signature", align="C")
    pdf.cell(87, 5, f'For {_safe(profile.get("business_name", "TRUE STORE"))}', align="C")
    pdf.ln(2)
    pdf.set_draw_color(*BORDER_CLR)
    pdf.line(lm + 10, sig_y + 7, lm + 77, sig_y + 7)
    pdf.line(lm + 97, sig_y + 7, lm + 164, sig_y + 7)

    return bytes(pdf.output())
