"""
delivery_pdf.py
================
Branded delivery receipt PDF — same navy header design as bill/purchase PDFs.
Two variants: customer copy (clean) and office copy (shows US/DU status).
"""
from fpdf import FPDF
import db as _db

NAVY       = (20, 33, 61)
DARK_GRAY  = (55, 55, 55)
MID_GRAY   = (120, 120, 120)
LIGHT_BG   = (245, 247, 250)
WHITE      = (255, 255, 255)
BORDER_CLR = (200, 200, 200)
PAGE_W     = 174


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
            "phone": "", "address_line1": ""}


def _merge_lines_by_description(lines):
    merged = {}
    order = []
    for line in lines:
        key = line["description"]
        if key not in merged:
            merged[key] = {"description": key, "qty": 0.0, "us_qty": 0.0, "du_qty": 0.0}
            order.append(key)
        qty = line["qty"]
        merged[key]["qty"] += qty
        if line.get("update_stock", True):
            merged[key]["us_qty"] += qty
        else:
            merged[key]["du_qty"] += qty
    return [merged[k] for k in order]


class DeliveryPDF(FPDF):
    def __init__(self, profile, copy_type):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.profile = profile
        self.copy_type = copy_type
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
        # Copy label
        self.set_font("Helvetica", "B", 9)
        self.set_xy(140, 10)
        label = "OFFICE COPY" if self.copy_type == "office" else "CUSTOMER COPY"
        self.cell(52, 5, label, align="R")
        self.set_text_color(*DARK_GRAY)
        self.set_y(34)

    def footer(self):
        self.set_y(-14)
        self.set_draw_color(*BORDER_CLR)
        self.line(18, self.get_y(), 192, self.get_y())
        self.set_font("Helvetica", "I", 7)
        self.set_text_color(*MID_GRAY)
        self.cell(0, 4, f"Page {self.page_no()}/{{nb}}", align="C")


def build_delivery_pdf(receipt_no, date, customer_name, lines, copy_type="customer"):
    profile = _get_profile()
    is_office = copy_type == "office"
    merged = _merge_lines_by_description(lines)

    pdf = DeliveryPDF(profile, copy_type)
    pdf.alias_nb_pages()
    pdf.add_page()
    lm = 18
    pdf.set_left_margin(lm)
    pdf.set_right_margin(18)

    # Title
    pdf.set_font("Helvetica", "B", 14)
    pdf.set_text_color(*NAVY)
    pdf.cell(PAGE_W, 8, "DELIVERY RECEIPT", ln=1, align="C")
    pdf.set_draw_color(*NAVY)
    pdf.line(lm, pdf.get_y(), lm + PAGE_W, pdf.get_y())
    pdf.ln(3)

    # Meta
    y_meta = pdf.get_y()
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(*DARK_GRAY)
    pdf.cell(90, 5, "Delivered To:", ln=1)
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(90, 5, _safe(customer_name), ln=1)

    box_x = lm + 110
    pdf.set_xy(box_x, y_meta)
    pdf.set_fill_color(*LIGHT_BG)
    pdf.set_draw_color(*BORDER_CLR)
    pdf.rect(box_x, y_meta, 64, 22, "FD")
    pdf.set_xy(box_x + 2, y_meta + 2)
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(*MID_GRAY)
    pdf.cell(30, 5, "Receipt No.")
    pdf.set_font("Helvetica", "B", 10)
    pdf.set_text_color(*NAVY)
    pdf.cell(30, 5, _safe(receipt_no), ln=1)
    pdf.set_xy(box_x + 2, y_meta + 11)
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(*MID_GRAY)
    pdf.cell(30, 5, "Date")
    pdf.set_font("Helvetica", "", 10)
    pdf.set_text_color(*DARK_GRAY)
    pdf.cell(30, 5, _safe(date))

    pdf.set_y(max(pdf.get_y() + 5, y_meta + 28))
    pdf.ln(2)

    # Table
    pdf.set_text_color(*DARK_GRAY)
    if is_office:
        cols = [100, 34, 40]
        hdrs = ["Item", "Quantity", "Stock Info"]
    else:
        cols = [134, 40]
        hdrs = ["Item", "Quantity"]

    pdf.set_font("Helvetica", "B", 8)
    pdf.set_fill_color(*NAVY)
    pdf.set_text_color(*WHITE)
    for h, w in zip(hdrs, cols):
        align = "R" if h == "Quantity" else ("C" if h == "Stock Info" else "L")
        pdf.cell(w, 7, h, border=0, fill=True, align=align)
    pdf.ln()

    pdf.set_text_color(*DARK_GRAY)
    pdf.set_font("Helvetica", "", 9)
    for idx, line in enumerate(merged):
        pdf.set_fill_color(*(LIGHT_BG if idx % 2 == 0 else WHITE))
        if is_office:
            pdf.cell(100, 7, _safe(line["description"]), border=0, fill=True)
            pdf.cell(34, 7, f'{line["qty"]:g}', border=0, fill=True, align="R")
            if line["du_qty"] == 0:
                info = "US"
            elif line["us_qty"] == 0:
                info = "DU"
            else:
                info = f'US {line["us_qty"]:g} / DU {line["du_qty"]:g}'
            pdf.cell(40, 7, _safe(info), border=0, fill=True, align="C")
        else:
            pdf.cell(134, 7, _safe(line["description"]), border=0, fill=True)
            pdf.cell(40, 7, f'{line["qty"]:g}', border=0, fill=True, align="R")
        pdf.ln()

    pdf.set_draw_color(*NAVY)
    pdf.line(lm, pdf.get_y(), lm + PAGE_W, pdf.get_y())
    pdf.ln(3)

    # Item count
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_text_color(*NAVY)
    total_qty = sum(l["qty"] for l in merged)
    pdf.cell(PAGE_W, 6, f"Total Items: {len(merged)}    Total Quantity: {total_qty:g}", ln=1, align="R")
    pdf.ln(2)

    if is_office:
        pdf.set_font("Helvetica", "I", 7)
        pdf.set_text_color(*MID_GRAY)
        pdf.multi_cell(PAGE_W, 4,
            "US = counted as inventory movement once approved. "
            "DU = delivered but not stock-tracked (extra/informal supply). "
            "Split items show both portions.")
        pdf.ln(4)

    # Signatures
    sig_y = max(pdf.get_y() + 10, 240)
    pdf.set_y(sig_y)
    pdf.set_font("Helvetica", "", 8)
    pdf.set_text_color(*MID_GRAY)
    pdf.cell(87, 5, "Delivered by", align="C")
    pdf.cell(87, 5, "Received by", align="C")
    pdf.ln(8)
    pdf.set_draw_color(*BORDER_CLR)
    pdf.line(lm + 10, pdf.get_y(), lm + 77, pdf.get_y())
    pdf.line(lm + 97, pdf.get_y(), lm + 164, pdf.get_y())

    return bytes(pdf.output())
