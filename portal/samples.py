"""Test scenarios: full submissions plus real PDF files. Everything here is fictional."""
from datetime import date, timedelta

from .db import KAVERI_PAN, NORTHWIND_ACCOUNT
from .validators import make_gstin


def iso(days):
    return (date.today() + timedelta(days=days)).isoformat()


def make_pdf(title, lines):
    """A small valid PDF, read back through the same parser as any upload."""
    def esc(text):
        safe = "".join(ch if 32 <= ord(ch) < 127 else "?" for ch in str(text))
        return safe.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    content = f"BT /F1 16 Tf 56 770 Td ({esc(title)}) Tj ET\n"
    y = 736
    for line in lines:
        content += f"BT /F1 11 Tf 56 {y} Td ({esc(line)}) Tj ET\n"
        y -= 20
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(content)} >>\nstream\n{content}endstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = "%PDF-1.4\n"
    offsets = []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{obj}\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n" + "".join(f"{n:010d} 00000 n \n" for n in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    return out.encode("latin-1")


def doc(filename, title, lines):
    return {"name": filename, "bytes": make_pdf(title, lines), "type": "application/pdf"}


ACME = "Acme Industrial Supplies Pvt Ltd"
ACME_GSTIN = "27AAPFU0939F1ZV"
ACME_ACCOUNT = "123456789012"
BANK_CODE = "HDFC0001234"
BANK_NAMES = {"HDFC": "HDFC Bank", "ICIC": "ICICI Bank"}


def _tax_doc(slug, legal, gstin, valid_until):
    return doc(f"{slug}-tax-certificate.pdf", "GST Registration Certificate", ["Goods and Services Tax, Government of India", f"Legal Name: {legal}", f"GSTIN: {gstin}", f"Valid Until: {valid_until}"])


def _inc_doc(slug, legal):
    return doc(f"{slug}-incorporation.pdf", "Certificate of Incorporation", ["Companies Act, 2013", f"Company Name: {legal}", f"Date of Incorporation: {iso(-1500)}"])


def _cheque(slug, holder, account, code):
    return doc(f"{slug}-crossed-cheque.pdf", "Crossed Cheque", [BANK_NAMES[code[:4]], f"Account Holder: {holder}", "Account Type: Current", f"Account Number: {account}", f"IFSC: {code}"])


def _documents(slug, details):
    """All three documents, built from one set of details so they can't disagree with it."""
    return {
        "inc": _inc_doc(slug, details["legalName"]),
        "tax": _tax_doc(slug, details["taxCertName"], details["taxId"], details["certExpiry"]),
        "bank": _cheque(slug, details["bankHolder"], details["accountNumber"], details["bankCode"]),
    }


def _acme_form(**changes):
    return {
        "legalName": ACME, "country": "IN", "taxId": ACME_GSTIN, "taxCertName": ACME, "bankHolder": ACME, "bankCode": BANK_CODE,
        "accountNumber": ACME_ACCOUNT, "contactEmail": "accounts@acmeindustrial.in", "website": "https://www.acmeindustrial.in", "certExpiry": iso(300), **changes,
    }


def _clean():
    form = _acme_form()
    return form, _documents("acme", form)


def _trade_name():
    form = _acme_form(bankHolder="Acme Industrial Traders", contactEmail="acmeindustrial@gmail.com")
    return form, _documents("acme", form)


def _trade_name_fixed():
    form, docs = _trade_name()
    form["contactEmail"] = "accounts@acmeindustrial.in"
    docs["bank"] = doc("acme-bank-letter.pdf", "Bank Confirmation Letter", [BANK_NAMES["HDFC"], f"Account Holder: {form['bankHolder']}", f"Legal Entity: {ACME}", f"Account Number: {ACME_ACCOUNT}", f"IFSC: {BANK_CODE}", f"Issued On: {iso(-1)}"])
    return form, docs


def _form_differs():
    """The only scenario where the form and documents disagree: the documents are right, three entries are typed wrongly."""
    legal = "Kaveri Precision Components Pvt Ltd"
    correct = {
        "legalName": legal, "country": "IN", "taxId": make_gstin("27", "AAKCK4512P"), "taxCertName": legal, "bankHolder": legal, "bankCode": BANK_CODE,
        "accountNumber": "987654321098", "contactEmail": "accounts@kavericomponents.in", "website": "kavericomponents.in", "certExpiry": iso(300),
    }
    form = {**correct, "taxId": make_gstin("27", "AAKCK4521P"), "bankCode": "HDFC0001243", "accountNumber": "987654321089"}
    return form, _documents("kaveri-precision", correct)


def _shared_bank():
    legal = "Meridian Freight Solutions"
    form = _acme_form(legalName=legal, taxCertName=legal, bankHolder=legal, taxId=make_gstin("27", "AAHCM8823Q"), accountNumber=NORTHWIND_ACCOUNT, contactEmail="accounts@meridianfreight.in", website="https://www.meridianfreight.in")
    return form, _documents("meridian", form)


def _bank_change():
    legal = "Kaveri Industrial Supplies Private Limited"
    form = _acme_form(legalName=legal, taxCertName=legal, bankHolder=legal, taxId=make_gstin("33", KAVERI_PAN), bankCode="ICIC0002291", accountNumber="77190034218850", contactName="Meena Iyer", contactEmail="meena.iyer@kaverisupplies.example", website="https://kaverisupplies.example")
    return form, _documents("kaveri", form)


SAMPLES = {
    "clean": {"label": "Happy path: all details are correct", "build": _clean},
    "tradename": {"label": "Edge 1: trade name on bank, personal email", "fix": "tradename_fixed", "build": _trade_name},
    "tradename_fixed": {"label": "Edge 1 corrected: bank letter added, company email used", "hidden": True, "build": _trade_name_fixed},
    "subtle": {"label": "Edge 2: form does not match the documents", "build": _form_differs},
    "sharedbank": {"label": "Edge 3: bank account belongs to another vendor", "build": _shared_bank},
    "bankchange": {"label": "Edge 4: registered vendor changes bank account", "build": _bank_change},
}
