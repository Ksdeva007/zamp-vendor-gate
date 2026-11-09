"""Business settings and reference data. Everything here is fictional demo data."""

APP_NAME = "VendorGate"
APP_TAGLINE = "Vendor onboarding"
BUYER = "ABC Company"
BUYER_DEPT = "Procurement"

MAX_UPLOAD_BYTES = 5 * 1024 * 1024

COUNTRIES = {
    "IN": {"name": "India", "tax": "GSTIN", "code": "IFSC", "acct": "Account number"},
    "US": {"name": "United States", "tax": "EIN", "code": "ABA routing number", "acct": "Account number"},
    "GB": {"name": "United Kingdom", "tax": "VAT no.", "code": None, "acct": "IBAN"},
    "DE": {"name": "Germany", "tax": "USt-IdNr.", "code": None, "acct": "IBAN"},
    "OT": {"name": "Other country", "tax": "Tax ID", "code": None, "acct": "Account number or IBAN"},
}

OWNERSHIP = ["Private Limited", "Public Limited", "Limited Liability Partnership", "Partnership Firm", "Sole Proprietorship"]
PAN_TYPE = {"Private Limited": "C", "Public Limited": "C", "Limited Liability Partnership": "F", "Partnership Firm": "F", "Sole Proprietorship": "P"}

GST_STATES = {
    "jammu and kashmir": "01", "himachal pradesh": "02", "punjab": "03", "chandigarh": "04", "uttarakhand": "05",
    "haryana": "06", "delhi": "07", "rajasthan": "08", "uttar pradesh": "09", "bihar": "10", "assam": "18",
    "west bengal": "19", "jharkhand": "20", "odisha": "21", "chhattisgarh": "22", "madhya pradesh": "23",
    "gujarat": "24", "maharashtra": "27", "karnataka": "29", "goa": "30", "kerala": "32", "tamil nadu": "33",
    "puducherry": "34", "telangana": "36", "andhra pradesh": "37",
}

# First two PIN digits by state, for the address consistency check.
PIN_PREFIX = {
    "delhi": ["11"], "haryana": ["12", "13"], "punjab": ["14", "15", "16"], "chandigarh": ["16"], "himachal pradesh": ["17"],
    "jammu and kashmir": ["18", "19"], "uttar pradesh": ["20", "21", "22", "23", "24", "25", "26", "27", "28"],
    "uttarakhand": ["24", "26"], "rajasthan": ["30", "31", "32", "33", "34"], "gujarat": ["36", "37", "38", "39"],
    "maharashtra": ["40", "41", "42", "43", "44"], "goa": ["40"], "madhya pradesh": ["45", "46", "47", "48"],
    "chhattisgarh": ["49"], "telangana": ["50"], "andhra pradesh": ["50", "51", "52", "53"],
    "karnataka": ["56", "57", "58", "59"], "tamil nadu": ["60", "61", "62", "63", "64", "65", "66"], "puducherry": ["60"],
    "kerala": ["67", "68", "69"], "west bengal": ["70", "71", "72", "73", "74"], "odisha": ["75", "76", "77"],
    "assam": ["78"], "bihar": ["80", "81", "82", "84", "85"], "jharkhand": ["81", "82", "83"],
}

FREE_MAIL = {"gmail.com", "yahoo.com", "yahoo.co.in", "hotmail.com", "outlook.com", "rediffmail.com", "proton.me", "protonmail.com", "icloud.com"}

DOC_SLOTS = {
    "inc": "Certificate of incorporation",
    "tax": "Tax registration certificate",
    "bank": "Crossed cheque or bank letter",
    "w9": "Form W-9 (US vendors only)",
    "pan": "PAN card",
    "moa": "Memorandum / Articles of Association",
    "trc": "Tax Residency Certificate",
    "ins": "Certificate of insurance",
}

STAGES = [
    "Intake & normalise",
    "Document completeness",
    "Tax registration",
    "Banking details",
    "Cross-reference & fraud signals",
    "Risk screening",
]

WATCHLIST = [
    {"name": "crimson harbor trading", "why": "Demo entry: mock sanctions list"},
    {"name": "blackwater shell holdings", "why": "Demo entry: mock sanctions list"},
    {"name": "Orion Trade Links FZE", "why": "Demo entry: payment fraud case 2025-117"},
    {"name": "Meridian Global Commodities", "why": "Demo entry: failed sanctions screening"},
]


def required_docs(sub):
    """Which documents this vendor must provide. The form and the process both use this list."""
    domestic = sub.get("country") == "IN"
    docs = []
    if sub.get("entityType") != "Sole Proprietorship":
        docs.append("inc")
    if not (domestic and sub.get("vendorType") == "PAN without GST"):
        docs.append("tax")
    docs.append("bank")
    if sub.get("country") == "US":
        docs.append("w9")
    if sub.get("category") == "On-site services":
        docs.append("ins")
    return docs


def optional_docs(sub):
    """Documents a vendor may add. If attached, they are read and checked like the required ones."""
    domestic = sub.get("country") == "IN"
    candidates = ["pan", "moa"] if domestic else ["trc"]
    if "inc" not in required_docs(sub):
        candidates.append("inc")
    if "ins" not in required_docs(sub):
        candidates.append("ins")
    return candidates
