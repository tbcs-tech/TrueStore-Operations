from fpdf import FPDF

COL_WIDTHS = [38, 78, 26, 30, 32, 26]
HEADERS = ["Invoice #", "Customer", "Date", "Amount", "Margin", "Status"]


def _safe(text):
    """FPDF's built-in fonts are latin-1 only; replace anything outside that range."""
    return str(text).encode("latin-1", "replace").decode("latin-1")


def build_summary_pdf(lines, metric):
    """
    lines: list of enriched invoice-line dicts (invoice_id, customer, date_str,
           amount, margin, taxable_amount, status)
    metric: 'margin' or 'taxable' — which second column to show.
    Returns raw PDF bytes.
    """
    metric_label = "Margin" if metric == "margin" else "Taxable Amount"
    headers = list(HEADERS)
    headers[4] = metric_label

    pdf = FPDF(orientation="L", unit="mm", format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()

    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, "Receivables Summary", ln=1)
    pdf.set_font("Helvetica", "", 9)
    pdf.cell(0, 6, f"Generated {__import__('datetime').date.today().strftime('%d/%m/%Y')} "
                    f"- {len(lines)} invoice line(s) - metric: {metric_label}", ln=1)
    pdf.ln(2)

    pdf.set_font("Helvetica", "B", 9)
    pdf.set_fill_color(230, 230, 230)
    for header, width in zip(headers, COL_WIDTHS):
        pdf.cell(width, 8, _safe(header), border=1, fill=True)
    pdf.ln()

    pdf.set_font("Helvetica", "", 8)
    total_amount = 0.0
    total_metric = 0.0
    for line in lines:
        metric_val = line.get("margin", 0.0) if metric == "margin" else line.get("taxable_amount", 0.0)
        total_amount += line.get("amount", 0.0)
        total_metric += metric_val
        row = [
            line.get("invoice_id", ""),
            line.get("customer", ""),
            line.get("date_str", ""),
            f'{line.get("amount", 0.0):,.2f}',
            f'{metric_val:,.2f}',
            line.get("status", ""),
        ]
        for value, width in zip(row, COL_WIDTHS):
            pdf.cell(width, 7, _safe(value), border=1)
        pdf.ln()

    pdf.set_font("Helvetica", "B", 8)
    pdf.cell(COL_WIDTHS[0] + COL_WIDTHS[1] + COL_WIDTHS[2], 7, "Total", border=1)
    pdf.cell(COL_WIDTHS[3], 7, f"{total_amount:,.2f}", border=1)
    pdf.cell(COL_WIDTHS[4], 7, f"{total_metric:,.2f}", border=1)
    pdf.cell(COL_WIDTHS[5], 7, "", border=1)

    output = pdf.output()
    return bytes(output)
