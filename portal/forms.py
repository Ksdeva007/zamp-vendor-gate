"""The vendor submission form: which fields and documents apply, what's missing, and how edits are applied.
The browser only displays what this returns."""
from .config import COUNTRIES, DOC_SLOTS, GST_STATES, OWNERSHIP, optional_docs, required_docs

MAX_LENGTHS = {"serviceDesc": 2000}
DEFAULT_LENGTH = 300


def blank_values():
    return {
        "legalName": "", "country": "IN", "taxId": "", "taxCertName": "", "bankHolder": "", "bankCode": "", "accountNumber": "",
        "contactEmail": "", "website": "", "certExpiry": "", "vendorType": "PAN with GST", "pan": "", "trc": "", "tradeName": "",
        "entityType": "", "addr1": "", "addr2": "", "city": "", "state": "", "pincode": "", "contactName": "", "mobile": "",
        "phone": "", "altPhone": "", "bankName": "", "branch": "", "accountType": "Current", "category": "Goods", "serviceDesc": "",
        "hsn": "", "udyam": "",
    }


EDITABLE = set(blank_values())


def _field(key, label, required=False, **extra):
    return {"key": key, "label": label, "required": required, **extra}


def _select(key, label, options, required=False, always_set=False):
    choices = [{"value": o[0], "label": o[1]} if isinstance(o, tuple) else {"value": o, "label": o or "Select"} for o in options]
    return _field(key, label, required, type="select", options=choices, always_set=always_set)


def form_sections(v):
    """The first section is always open. The rest are additional details, checked whenever they are filled in."""
    domestic = v.get("country") == "IN"
    without_gst = domestic and v.get("vendorType") == "PAN without GST"

    main = [
        _field("legalName", "Legal name", True),
        _select("country", "Country", [(code, c["name"]) for code, c in COUNTRIES.items()], True, True),
    ]
    if not without_gst:
        main += [
            _field("taxId", "Tax ID (GSTIN / EIN / VAT)", True, mono=True),
            _field("taxCertName", "Name on tax certificate", True),
        ]
    main += [
        _field("bankHolder", "Bank account holder name", True),
        _field("bankCode", "IFSC / routing no. (blank for IBAN)", mono=True),
        _field("accountNumber", "Account no. / IBAN", True, mono=True),
        _field("contactEmail", "Contact email", True, type="email"),
        _field("website", "Website"),
    ]
    if not without_gst:
        main.append(_field("certExpiry", "Tax certificate valid until", type="date"))

    if domestic:
        tax = [
            _select("vendorType", "Vendor type", ["PAN with GST", "PAN without GST"], always_set=True),
            _field("pan", "PAN number", without_gst, mono=True, hint="For example ABCDE1234F"),
            _field("udyam", "Udyam (MSME) registration number", mono=True),
        ]
    else:
        tax = [_field("trc", "TRC number", mono=True, hint="Tax Residency Certificate number")]

    organisation = [
        _field("tradeName", "Trade name, if different"),
        _select("entityType", "Ownership", ["", *OWNERSHIP]),
        _field("addr1", "Registered address line 1"),
        _field("addr2", "Registered address line 2"),
        _field("city", "City"),
        _field("state", "State" if domestic else "State / region", suggestions=[s.title() for s in sorted(GST_STATES)] if domestic else None),
        _field("pincode", "Pincode" if domestic else "Postcode / ZIP", mono=True),
    ]
    contact = [
        _field("contactName", "Contact person name"),
        _field("mobile", "Contact person mobile number"),
        _field("phone", "Company phone number"),
        _field("altPhone", "Alternative phone number"),
    ]
    bank = [
        _field("bankName", "Bank name"),
        _field("branch", "Branch"),
        _select("accountType", "Account type", ["Current", "Savings"], always_set=True),
    ]
    services = [_select("category", "Supply type", ["Goods", "Services", "On-site services"], always_set=True)]
    if domestic:
        services.append(_field("hsn", "HSN / SAC code", mono=True, hint="4, 6 or 8 digits"))
    services.append(_field("serviceDesc", "Products or services offered", type="textarea"))

    return [
        {"title": None, "fields": main},
        {"title": "Tax details", "fields": tax},
        {"title": "Organisation", "fields": organisation},
        {"title": "Contact person", "fields": contact},
        {"title": "Bank branch", "fields": bank},
        {"title": "Services", "fields": services},
    ]


def required_fields(values):
    """(key, label) for every required field, in form order. The process uses this same list."""
    return [(f["key"], f["label"]) for section in form_sections(values) for f in section["fields"] if f["required"] and not f.get("always_set")]


def apply_edits(values, edits, sample_id):
    """Applies form edits. Returns (values, sample_id, error). Editing a test case makes it a real submission."""
    out = dict(values)
    for key, value in edits.items():
        if key not in EDITABLE:
            return values, sample_id, f"Unknown field: {key}"
        if not isinstance(value, str):
            return values, sample_id, f"Invalid value for {key}"
        if len(value) > MAX_LENGTHS.get(key, DEFAULT_LENGTH):
            return values, sample_id, "That entry is too long."
        out[key] = value
    if any(out.get(k) != values.get(k) for k in out):
        sample_id = None
    return out, sample_id, None


def _doc_rows(draft):
    values, attached = draft["values"], draft["documents"]
    required = required_docs(values)

    def row(slot):
        file = {"name": attached[slot]["name"], "size": attached[slot]["size"]} if slot in attached else None
        return {"slot": slot, "label": DOC_SLOTS[slot], "required": slot in required, "file": file}

    return [row(slot) for slot in [*required, *optional_docs(values)]]


def draft_view(draft):
    """Everything the browser needs to draw the form."""
    v = draft["values"]
    sections = form_sections(v)
    documents = _doc_rows(draft)
    missing = [f["label"] for s in sections for f in s["fields"] if f["required"] and not str(v.get(f["key"]) or "").strip()]
    missing += [d["label"] for d in documents if d["required"] and not d["file"]]
    return {
        "id": draft["id"],
        "sample_id": draft["sample_id"],
        "parent_id": draft["parent_id"],
        "submitted_run_id": draft["submitted_run_id"],
        "sections": [{**s, "fields": [{**f, "value": v.get(f["key"]) or ""} for f in s["fields"]]} for s in sections],
        "documents": documents,
        "missing": missing,
    }


def submission_summary(form):
    """The entries of a stored submission, for display. The account number is already masked."""
    labels = {f["key"]: f for s in form_sections(form) for f in s["fields"]}
    rows = []
    for key, f in labels.items():
        value = form.get(key) or ""
        if not value or (f.get("always_set") and key != "country"):
            continue
        if f.get("type") == "select":
            value = next((o["label"] for o in f["options"] if o["value"] == value), value)
        rows.append({"label": f["label"], "value": value})
    return rows
