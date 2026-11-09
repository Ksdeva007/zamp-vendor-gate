"""The onboarding process: six stages, each writing its checks to the database as they happen."""
import re
import time

from . import db, messages
from .config import COUNTRIES, DOC_SLOTS, FREE_MAIL, GST_STATES, PAN_TYPE, PIN_PREFIX, required_docs
from .documents import days_until, parse_date, read_document
from .forms import required_fields
from .validators import (
    NAME_LOOKALIKE, NAME_MATCH, NAME_PARTIAL, NAME_SAME, TAX_CHECK, acct_hash, acct_last4, clean_id, is_aba, is_gstin, is_iban,
    is_ifsc, is_pan, is_udyam, looks_like_gstin, name_score, name_similarity, normalise_name,
)

FIELD_NAMES = {"legalName": "Name on document", "holder": "Account holder", "legalEntity": "Legal entity", "account": "Account", "accountType": "Account type", "code": "Bank code", "taxId": "Tax ID", "pan": "PAN", "trcNo": "TRC number", "expiry": "Valid until", "incDate": "Incorporated"}
TEXT_FIELDS = ["legalName", "taxCertName", "certExpiry", "tradeName", "state", "addr1", "addr2", "city", "pincode", "website", "contactName", "contactEmail", "mobile", "phone", "altPhone", "bankHolder", "bankName", "branch", "serviceDesc", "hsn", "trc", "entityType", "vendorType", "category", "accountType", "country"]
BANK_PROOF = "cheque or bank letter"
PACE_CHECK = 0.12
PACE_STAGE = 0.45


def normalise(form):
    s = {k: str(form.get(k) or "").strip() for k in TEXT_FIELDS}
    s["taxId"] = clean_id(form.get("taxId"))
    s["pan"] = clean_id(form.get("pan"))
    s["accountNumber"] = clean_id(form.get("accountNumber"))
    s["bankCode"] = clean_id(form.get("bankCode"))
    s["udyam"] = clean_id(form.get("udyam"))
    if s["country"] not in COUNTRIES:
        s["country"] = "OT"
    if s["country"] == "IN" and s["vendorType"] == "PAN without GST":
        s["taxId"] = ""
        s["taxCertName"] = ""
        s["certExpiry"] = ""
    return s


def mask_form(s):
    masked = dict(s)
    masked["accountNumber"] = acct_last4(s["accountNumber"])
    return masked


class Recorder:
    """Writes checks for the current stage and keeps a copy for the decision."""

    def __init__(self, run_id, pace):
        self.run_id, self.pace, self.stage_idx, self.checks = run_id, pace, 0, []

    def add(self, title, status, detail="", evidence=None, **vendor_facing):
        db.add_check(self.run_id, self.stage_idx, title, status, detail, evidence, **vendor_facing)
        self.checks.append({"title": title, "status": status, "detail": detail, "field": vendor_facing.get("field"), "entered": vendor_facing.get("entered"), "expected": vendor_facing.get("expected"), "action": vendor_facing.get("action"), "kind": vendor_facing.get("kind")})
        if self.pace:
            time.sleep(PACE_CHECK)

    def ok(self, title, detail="", evidence=None):
        self.add(title, "pass", detail, evidence)

    def note(self, title, detail=""):
        self.add(title, "warn", detail)

    def wrong(self, title, detail, field, entered, expected, action):
        """An entry the vendor must correct."""
        self.add(title, "fixable", detail, field=field, entered=entered, expected=expected, action=action, kind="entry")

    def missing(self, title, detail, field):
        self.add(title, "fixable", detail, field=field, kind="missing")

    def review(self, title, detail, field, entered, action, evidence=None):
        """Only an independent check settles it. `detail` stays internal."""
        self.add(title, "verify", detail, evidence, field=field, entered=entered, action=action, kind="review")

    def fail(self, title, detail, kind, evidence=None):
        """kind: risk (no reasons given to the vendor), duplicate, or correctable (the reason is given)."""
        self.add(title, "block", detail, evidence, kind=kind)

    def stage(self, idx, fn):
        self.stage_idx = idx
        db.set_active_stage(self.run_id, idx)
        start = time.perf_counter()
        if self.pace:
            time.sleep(PACE_STAGE)
        fn()
        db.finish_stage(self.run_id, idx, round((time.perf_counter() - start) * 1000))


def _host(url):
    return re.sub(r"^www\.", "", re.sub(r"^https?://", "", url, flags=re.I), flags=re.I).split("/")[0].lower()


def run_pipeline(run_id, s, docs, sample_id=None, pace=False, ai_reader=None, draft_id=None):
    r = Recorder(run_id, pace)
    c = COUNTRIES[s["country"]]
    domestic = s["country"] == "IN"
    read = {}
    ai_used = []

    def intake():
        # Every gap is collected so the vendor receives one complete request.
        gaps = [label for key, label in required_fields(s) if not s[key]]
        for label in gaps:
            r.missing("Required fields", f"Missing: {label}", label)
        if not gaps:
            r.ok("Required fields", "All mandatory fields present")
        r.ok("Normalisation", "IDs upper-cased, whitespace stripped")

    def documents():
        needed = required_docs(s)
        for slot in needed:
            if slot not in docs:
                r.missing("Documents", f"Missing: {DOC_SLOTS[slot]}", f"{DOC_SLOTS[slot]} (document)")
        if all(slot in docs for slot in needed):
            r.ok("Documents", f"All {len(needed)} required documents attached")

        for slot, doc in docs.items():
            d = read_document(doc, slot, ai_reader)
            label = DOC_SLOTS[slot]
            if d["method"] == "ai":
                ai_used.append(f"Read {d['name']}")
            if d["error"]:
                why = {"scanned": "No selectable text, so this looks like a scan, and it could not be read automatically.", "corrupt": "The file could not be opened as a PDF or image.", "unread": "The image could not be read automatically."}[d["error"]]
                r.wrong(f"Could not read: {label}", why, label, d["name"], "A clear, readable copy", "Please send a clear PDF of the original document.")
                continue
            if d["docType"] != slot:
                seen = DOC_SLOTS[d["docType"]] if d["docType"] else "Not a document we recognise"
                r.wrong(f"Wrong file uploaded: {label}", f"{d['name']} reads as: {seen}.", label, f"{d['name']} ({seen})", label, "Please upload the correct document.")
                continue
            read[slot] = d
            how = "Text extracted" if d["method"] == "parser" else "Could not be parsed, read with AI"
            evidence = [[FIELD_NAMES.get(k, k), acct_last4(v) if k == "account" else v] for k, v in d["fields"].items()]
            r.ok(f"Read: {label}", f"{how}: {d['name']}", evidence)

        certificate_validity()
        for slot, label in [("ins", "Insurance"), ("trc", "Tax Residency Certificate")]:
            d = read.get(slot)
            if not d:
                continue
            expiry = parse_date(d["fields"].get("expiry"))
            if not expiry:
                r.wrong(f"{label}: expiry date not found", d["name"], DOC_SLOTS[slot], d["name"], "A certificate showing its expiry date", "Please send the full certificate.")
                continue
            days = days_until(expiry)
            if days < 0:
                r.wrong(f"{label} has expired", f"Expired {-days} days ago, on {expiry.isoformat()}.", DOC_SLOTS[slot], f"Certificate valid until {expiry.isoformat()}", "A certificate that is still valid", "Please send a current certificate.")
            elif days < 30:
                r.note(f"{label} expires soon", f"{days} days left. Procurement should request a renewal.")
            else:
                r.ok(f"{label} is current", f"{days} days left.")

    def certificate_validity():
        if not s["taxId"]:
            return
        field = "Tax certificate valid until"
        if not s["certExpiry"]:
            r.missing("Certificate validity", "No validity date provided", field)
            return
        expiry = parse_date(s["certExpiry"])
        if not expiry:
            r.wrong("Certificate validity", f"{s['certExpiry']} is not a date.", field, s["certExpiry"], "A date, for example 2027-08-31", "Please confirm the validity date of your tax certificate.")
        elif days_until(expiry) < 0:
            r.wrong("Certificate validity", f"Tax certificate expired on {expiry.isoformat()}", field, expiry.isoformat(), "A certificate that is still valid", f"Please send a current tax registration certificate (the one provided expired on {expiry.isoformat()}).")
        else:
            r.ok("Certificate validity", f"Valid until {expiry.isoformat()}")
        tax = read.get("tax")
        on_doc = parse_date(tax["fields"].get("expiry")) if tax else None
        if expiry and on_doc and on_doc != expiry:
            r.wrong("Validity date differs from the certificate", f"Form: {expiry.isoformat()}. Certificate: {on_doc.isoformat()}.", field, expiry.isoformat(), f"{on_doc.isoformat()} (as shown on your certificate)", "Please correct whichever is wrong.")

    def tax_registration():
        tax_id = s["taxId"]
        if s["country"] == "OT":
            r.review("No validation rules for this country", "Rules exist for India, the US, the UK and Germany. A person must review the registration number and bank details.", "Registration and bank details", None, "We are reviewing these manually and will contact you if we need anything further.")
        elif tax_id:
            if TAX_CHECK[s["country"]](tax_id):
                how = "(format + check digit)" if s["country"] in ("IN", "GB") else "(format)"
                r.ok("Tax ID", f"{tax_id} valid {c['tax']} {how}", [[c["tax"], tax_id]])
            else:
                other = next((k for k, check in TAX_CHECK.items() if k != s["country"] and check(tax_id)), None)
                if other:
                    r.wrong("Tax ID belongs to a different country", f"{tax_id} is a valid {COUNTRIES[other]['tax']} from {COUNTRIES[other]['name']}, but the vendor is registered in {c['name']}.", c["tax"], tax_id, f"A valid {c['tax']} for {c['name']}", "Please correct the number, or your registered country.")
                elif domestic and looks_like_gstin(tax_id):
                    r.wrong("Tax ID checksum", f"{tax_id} has valid format but fails the check-digit test (likely a typo/transposition)", c["tax"], tax_id, f"A {c['tax']} with a valid check digit", f"Your {c['tax']} ({tax_id}) fails its check-digit validation. Please re-check it.")
                else:
                    r.wrong("Tax ID format", f"{tax_id} is not a valid {c['tax']} format for {c['name']}", c["tax"], tax_id, f"A valid {c['tax']} for {c['name']}", f"Your {c['tax']} ({tax_id}) doesn't match the {c['name']} format. Please re-check it.")

        if s["taxCertName"] and s["legalName"]:
            score = name_similarity(s["legalName"], s["taxCertName"])
            pct = round(score * 100)
            if score >= NAME_MATCH:
                r.ok("Name vs tax certificate", f"Match ({pct}%)")
            elif score >= NAME_PARTIAL:
                r.wrong("Name vs tax certificate", f"Partial match ({pct}%): \"{s['legalName']}\" vs \"{s['taxCertName']}\"", "Name on tax certificate", s["taxCertName"], s["legalName"], "Please explain the difference between your legal name and the name on your tax certificate.")
            else:
                r.fail("Name vs tax certificate", f"Names unrelated ({pct}%): \"{s['legalName']}\" vs \"{s['taxCertName']}\"", "correctable")

        if domestic and s["pan"]:
            if not is_pan(s["pan"]):
                r.wrong("PAN format invalid", f"{s['pan']} is not in the format ABCDE1234F.", "PAN number", s["pan"], "10 characters in the format ABCDE1234F", "Please check the number and send the correct one.")
            else:
                want = PAN_TYPE.get(s["entityType"])
                if want and s["pan"][3] != want:
                    r.wrong("PAN holder type does not match ownership", f"The 4th character of a PAN shows the holder type. {s['pan']} has \"{s['pan'][3]}\", while {s['entityType']} should have \"{want}\".", "PAN number / Ownership information", f"{s['pan']}, {s['entityType']}", f"A PAN with \"{want}\" as its 4th character for {s['entityType']}", "Please confirm which of the two is correct.")
                else:
                    r.ok("PAN format valid", f"4th character \"{s['pan'][3]}\" matches {s['entityType']}." if want else "")
        if domestic and is_gstin(tax_id):
            embedded = tax_id[2:12]
            if s["pan"] and embedded != s["pan"]:
                r.wrong("GST number and PAN do not match", f"Characters 3 to 12 of a GST number are the holder's PAN. The GST number contains {embedded}, the form says {s['pan']}.", "PAN number", s["pan"], f"{embedded} (the PAN inside your GST number {tax_id})", "Please confirm which is correct.")
            elif s["pan"]:
                r.ok("PAN matches the GST number", evidence=[["PAN", embedded]])
            code, state_code = tax_id[:2], GST_STATES.get(s["state"].lower())
            if s["state"] and state_code and state_code != code:
                gst_state = next((k for k, v in GST_STATES.items() if v == code), "another state").title()
                r.wrong("GST number is registered in a different state", f"State code {code} is {gst_state}, the address is in {s['state']}. Multi-state companies hold one GST number per state.", "GST number / State", f"{tax_id} (registered in {gst_state}), address in {s['state']}", f"A GST number for {s['state']}, which starts with {state_code}", f"Please send the GST number for {s['state']}, or correct your address.")
            elif state_code:
                r.ok("GST state matches the address", f"Code {code} is {s['state']}.")

        tax = read.get("tax")
        if tax:
            value = tax["fields"].get("taxId")
            if value and clean_id(value) != tax_id:
                r.wrong(f"{c['tax']} does not match the certificate", f"Form: {tax_id}. Certificate: {value}.", c["tax"], tax_id, f"{value} (as shown on your certificate)", "Please correct the number on the form, or send the right certificate.")
            elif value:
                r.ok(f"{c['tax']} matches the certificate")
            else:
                r.wrong("Certificate does not show a tax ID", tax["name"], DOC_SLOTS["tax"], tax["name"], "A certificate showing your tax ID", "Please send the full certificate.")
        pan_doc = read.get("pan")
        if pan_doc and pan_doc["fields"].get("pan"):
            value = pan_doc["fields"]["pan"]
            if clean_id(value) != s["pan"]:
                r.wrong("PAN differs from the PAN card", f"Form: {s['pan']}. Card: {value}.", "PAN number", s["pan"], f"{value} (as shown on your PAN card)", "Please correct whichever is wrong.")
            else:
                r.ok("PAN matches the PAN card")
        trc = read.get("trc")
        if trc and trc["fields"].get("trcNo"):
            value = trc["fields"]["trcNo"]
            if clean_id(value) != clean_id(s["trc"]):
                r.wrong("TRC number differs from the certificate", f"Form: {s['trc']}. Certificate: {value}.", "TRC number", s["trc"], f"{value} (as shown on your certificate)", "Please correct whichever is wrong.")
            else:
                r.ok("TRC number matches the certificate")
        for slot, label in [("tax", "tax certificate"), ("pan", "PAN card"), ("inc", "certificate of incorporation"), ("moa", "memorandum"), ("w9", "Form W-9"), ("trc", "Tax Residency Certificate"), ("ins", "insurance certificate")]:
            d = read.get(slot)
            if not d:
                continue
            name = d["fields"].get("legalName")
            if not name:
                r.wrong(f"No company name on the {label}", d["name"], DOC_SLOTS[slot], d["name"], f"A document showing {s['legalName'] or 'your company name'}", "Please send the full document.")
                continue
            score = name_score(name, s["legalName"])
            if score >= NAME_SAME:
                r.ok(f"Company name matches the {label}", evidence=[["Document", name]])
            else:
                r.wrong(f"Company name differs from the {label}", f"Form: {s['legalName']}. Document: {name}. Similarity {round(score * 100)}%.", "Legal name", s["legalName"], f"{name} (as shown on your {label})", "Please send a document in your legal name, or explain the difference.")

    def match_bank_proof(shown, filename):
        """The crossed cheque or bank letter must show the same details as the form."""
        field, fix = DOC_SLOTS["bank"], "Please correct the entry on the form, or send the right cheque or bank letter."

        def not_shown(item):
            r.wrong(f"{item} not shown on the {BANK_PROOF}", filename, field, filename, f"{item} shown on your {BANK_PROOF}", "Please send a clear copy of your crossed cheque or bank letter.")

        acct, on_proof = s["accountNumber"], clean_id(shown.get("account"))
        if not on_proof:
            not_shown(c["acct"])
        elif acct and on_proof != acct:
            r.wrong(f"{c['acct']} does not match the {BANK_PROOF}", f"Form ends {acct[-4:]}. Document ends {on_proof[-4:]}.", c["acct"], f"Ending {acct[-4:]}", f"Ending {on_proof[-4:]} (as shown on your {BANK_PROOF})", fix)
        elif acct:
            r.ok(f"{c['acct']} matches the {BANK_PROOF}", evidence=[["Account", acct_last4(on_proof)]])

        if c["code"]:
            code, on_proof = s["bankCode"], clean_id(shown.get("code"))
            if not on_proof:
                not_shown(c["code"])
            elif code and on_proof != code:
                r.wrong(f"{c['code']} does not match the {BANK_PROOF}", f"Form: {code}. Document: {on_proof}.", c["code"], code, f"{on_proof} (as shown on your {BANK_PROOF})", fix)
            elif code:
                r.ok(f"{c['code']} matches the {BANK_PROOF}", evidence=[[c["code"], on_proof]])

        holder = shown.get("holder")
        if not holder:
            not_shown("Account holder")
        elif s["bankHolder"] and name_score(holder, s["bankHolder"]) < NAME_SAME:
            r.wrong(f"Account holder does not match the {BANK_PROOF}", f"Form: {s['bankHolder']}. Document: {holder}.", "Bank account holder name", s["bankHolder"], f"{holder} (as shown on your {BANK_PROOF})", fix)
        elif s["bankHolder"]:
            r.ok(f"Account holder matches the {BANK_PROOF}", evidence=[["Document", holder]])

        kind = shown.get("accountType")
        if kind and s["accountType"] and s["accountType"].lower() not in kind.lower():
            r.wrong(f"Account type does not match the {BANK_PROOF}", f"Form: {s['accountType']}. Document: {kind}.", "Account type", s["accountType"], f"{kind} (as shown on your {BANK_PROOF})", fix)
        elif kind and s["accountType"]:
            r.ok(f"Account type matches the {BANK_PROOF}", evidence=[["Document", kind]])

    def banking():
        acct, code = s["accountNumber"], s["bankCode"]
        if domestic:
            if is_ifsc(code):
                r.ok("IFSC", f"{code} format valid", [["IFSC", code]])
            else:
                r.wrong("IFSC", f"\"{code or 'blank'}\" is not a valid IFSC", "IFSC", code or "(blank)", "11 characters, with 0 as the 5th character", "Please provide a valid 11-character IFSC code.")
            if acct:
                if re.fullmatch(r"\d{9,18}", acct):
                    r.ok("Account number", "9 to 18 digits, valid", [["Account", acct_last4(acct)]])
                else:
                    r.wrong("Account number", "Must be 9 to 18 digits", "Account number", f"Ending {acct[-4:]}", "9 to 18 digits", "Please re-check your bank account number.")
        elif s["country"] == "US":
            if is_aba(code):
                r.ok("ABA routing", "Checksum valid", [["Routing number", code]])
            else:
                r.wrong("ABA routing", f"\"{code or 'blank'}\" fails the routing number checksum", "ABA routing number", code or "(blank)", "A 9-digit routing number with a valid checksum", "Please re-check your 9-digit ABA routing number.")
            if acct:
                if re.fullmatch(r"\d{4,17}", acct):
                    r.ok("Account number", "Valid length", [["Account", acct_last4(acct)]])
                else:
                    r.wrong("Account number", "Invalid length", "Account number", f"Ending {acct[-4:]}", "4 to 17 digits", "Please re-check your account number.")
        elif acct and s["country"] in ("GB", "DE"):
            if acct.startswith(s["country"]) and len(acct) == 22 and is_iban(acct):
                r.ok("IBAN", "Valid (mod-97 checksum, length, country)", [["Account", acct_last4(acct)]])
            else:
                r.wrong("IBAN", "Fails IBAN validation (mod-97, length or country)", "IBAN", f"Ending {acct[-4:]}", f"A valid {c['name']} IBAN, starting with {s['country']}", "Your IBAN failed validation. Please re-check it.")
        elif acct:
            if re.match(r"^[A-Z]{2}\d{2}", acct) and not is_iban(acct):
                r.wrong("IBAN", "Fails the mod-97 checksum", "IBAN", f"Ending {acct[-4:]}", "An IBAN with valid check digits", "Your IBAN failed validation. Please re-check it.")
            else:
                r.ok("Account number", "Provided. A person reviews it for this country.", [["Account", acct_last4(acct)]])

        bank = read.get("bank")
        if bank:
            match_bank_proof(bank["fields"], bank["name"])

        if s["bankHolder"] and s["legalName"]:
            score = name_similarity(s["bankHolder"], s["legalName"])
            pct = round(score * 100)
            entity = bank["fields"].get("legalEntity") if bank else None
            declared_trade_name = bool(s["tradeName"]) and name_similarity(s["bankHolder"], s["tradeName"]) >= NAME_MATCH
            if score >= NAME_MATCH:
                r.ok("Account holder", f"Matches legal name ({pct}%)")
            elif entity and name_similarity(entity, s["legalName"]) >= NAME_MATCH:
                r.ok("Account holder", f"Held as \"{s['bankHolder']}\". The bank letter names {entity} as the legal entity.", [["Bank letter", entity]])
            elif score >= NAME_PARTIAL or declared_trade_name:
                r.wrong("Account holder is not the legal name", f"The account is held by {s['bankHolder']}, but the legal name is {s['legalName']} ({pct}% similar). It may be a trade name.", "Bank account holder name", s["bankHolder"], s["legalName"], "The account holder is different from your legal name. Please send a bank letter confirming that this account belongs to your company.")
            else:
                r.fail("Account holder", f"\"{s['bankHolder']}\" is unrelated to \"{s['legalName']}\" ({pct}%), a third-party account", "correctable")
        if s["accountType"] == "Savings" and s["entityType"] != "Sole Proprietorship":
            r.note("Company uses a savings account", "Companies normally hold current accounts. Recorded for review.")

    def cross_reference():
        master = db.list_vendors()
        key = acct_hash(s["accountNumber"])
        by_tax = next((m for m in master if (s["taxId"] and m["tax_id"] == s["taxId"]) or (domestic and not s["taxId"] and s["pan"] and m["pan"] == s["pan"])), None)
        by_bank = next((m for m in master if s["accountNumber"] and m["acct_hash"] == key), None)
        same_vendor = by_tax and by_bank and by_tax["vendor_id"] == by_bank["vendor_id"]

        if by_bank and not same_vendor:
            r.fail("Bank account belongs to another vendor", f"This account is already registered to {by_bank['vendor_id']}, {by_bank['legal_name']}. This may be payment fraud. Do not tell the vendor which record matched.", "risk", [["Matched record", by_bank["vendor_id"]], ["Account", by_bank["acct_last4"]]])
        elif not same_vendor:
            r.ok("Bank account is not used by another vendor")

        if same_vendor:
            r.fail("Duplicate vendor", f"Same tax ID and bank account as {by_tax['vendor_id']}, {by_tax['legal_name']}. A second record risks duplicate payments.", "duplicate", [["Existing record", by_tax["vendor_id"]]])
        elif by_tax:
            r.review("Registered vendor is changing bank details", f"This vendor is already registered as {by_tax['vendor_id']}, {by_tax['legal_name']}. The account on file is {by_tax['acct_last4']} and the new account is {acct_last4(s['accountNumber'])}. Call {by_tax['contact_name']} using the details on file, not the details in this submission.",
                     "Bank account", f"Account ending {s['accountNumber'][-4:]}, which differs from the account on our records", "For your protection, we will confirm this change by phone with the contact on our records.",
                     [["Contact on file", f"{by_tax['contact_name']}, {by_tax['contact_email']}"], ["Contact submitted", f"{s['contactName'] or 'none'}, {s['contactEmail'] or 'none'}"]])
        else:
            same_name = next((m for m in master if s["legalName"] and normalise_name(m["legal_name"]) == normalise_name(s["legalName"])), None)
            look = next((m for m in master if s["legalName"] and name_score(m["legal_name"], s["legalName"]) >= NAME_LOOKALIKE), None)
            if same_name:
                r.fail("Duplicate vendor", f"Vendor already onboarded as {same_name['vendor_id']}.", "duplicate", [["Existing record", same_name["vendor_id"]]])
            elif look:
                r.review("Name closely resembles a registered vendor", f"Resembles {look['vendor_id']}, {look['legal_name']}, with a different tax ID. Procurement should confirm the two are unrelated.", "Legal name", s["legalName"], "We are checking this against our records and will contact you if we need anything further.", [["Similar record", look["vendor_id"]]])
            else:
                r.ok("Duplicate check", "No existing vendor with this name or tax ID")

        email = s["contactEmail"]
        if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            r.wrong("Contact email invalid", email, "Contact email", email, "A valid email address", "Please send the correct address.")
        elif email:
            domain, web = email.split("@")[1].lower(), _host(s["website"])
            if domain in FREE_MAIL:
                r.wrong("Contact email is personal", "A company vendor should use a company email address.", "Contact email", email, "An address on your company's domain", "Please contact us from a company email address.")
            elif web and domain != web and not domain.endswith("." + web):
                r.wrong("Email domain", f"Email domain differs from website ({web})", "Contact email", email, f"An address on {web}", "Please confirm your contact email belongs to your company domain.")
            else:
                r.ok("Email domain", "Corporate domain" + (" matches website" if web else ""))

        if domestic and s["pincode"]:
            if not re.fullmatch(r"[1-9]\d{5}", s["pincode"]):
                r.wrong("Pincode invalid", f"{s['pincode']} is not a 6-digit Indian pincode.", "Pincode", s["pincode"], "A 6-digit Indian pincode", "Please check your address.")
            else:
                prefixes = PIN_PREFIX.get(s["state"].lower())
                if prefixes and s["pincode"][:2] not in prefixes:
                    r.wrong("Pincode does not match the state", f"{s['pincode']} is not a {s['state']} pincode.", "Pincode / State", f"{s['pincode']}, {s['state']}", f"A {s['state']} pincode, starting with {' or '.join(prefixes)}", "Please check your address.")
                elif prefixes:
                    r.ok("Pincode matches the state", s["pincode"])
        if domestic and s["mobile"] and not re.fullmatch(r"(\+?91)?[6-9]\d{9}", re.sub(r"[\s-]", "", s["mobile"])):
            r.wrong("Mobile number invalid", s["mobile"], "Contact person mobile number", s["mobile"], "A 10-digit Indian mobile number", "Please send the correct number.")
        if domestic and s["hsn"] and not re.fullmatch(r"\d{4}(\d{2})?(\d{2})?", s["hsn"]):
            r.wrong("HSN / SAC code invalid", f"{s['hsn']} should have 4, 6 or 8 digits.", "HSN / SAC code", s["hsn"], "A code with 4, 6 or 8 digits", "Please send the correct code.")
        if s["udyam"]:
            if is_udyam(s["udyam"]):
                r.note("MSME registered", f"{s['udyam']}. Accounts Payable should apply MSME payment terms (45 days).")
            else:
                r.wrong("Udyam number invalid", s["udyam"], "Udyam registration number", s["udyam"], "A number like UDYAM-TN-03-0048213", "Please send the correct number.")
        inc = read.get("inc")
        inc_date = parse_date(inc["fields"].get("incDate")) if inc else None
        if inc_date and -days_until(inc_date) < 90:
            r.note("Newly incorporated company", f"Only {-days_until(inc_date)} days old. Recorded for large orders.")

    def risk_screening():
        legal, holder = normalise_name(s["legalName"]), normalise_name(s["bankHolder"])
        for w in db.list_watchlist():
            entry = normalise_name(w["name"])
            if entry and (entry in legal or entry in holder or name_score(w["name"], s["legalName"]) >= NAME_SAME):
                r.fail("Sanctions screen", f"Match on watchlist entry \"{w['name']}\". {w['why']}.", "risk")
                return
        r.ok("Sanctions screen", "No watchlist match (mock list)")

    for idx, fn in enumerate([intake, documents, tax_registration, banking, cross_reference, risk_screening]):
        r.stage(idx, fn)

    # The decision: any failure rejects, any open item holds, otherwise approved.
    blocks = [x for x in r.checks if x["status"] == "block"]
    verifies = [x for x in r.checks if x["status"] == "verify"]
    fixes = [x for x in r.checks if x["status"] == "fixable"]
    notes = [x for x in r.checks if x["status"] == "warn"]
    name = s["legalName"] or "your company"
    to = s["contactEmail"] or "(no contact email given)"
    vendor_id, added = None, False
    if blocks:
        status = "rejected"
        headline = blocks[0]["title"] + "."
        if any(b["kind"] == "risk" for b in blocks):
            owner = "Risk team (review as suspected fraud)"
            message = messages.rejected_risk(name, to)
        elif all(b["kind"] == "duplicate" for b in blocks):
            owner = "Procurement (link to existing record)"
            message = messages.rejected_duplicate(name, to)
        else:
            owner = "Vendor (may resubmit)"
            message = messages.rejected_correctable(name, to, [b["detail"] for b in blocks if b["kind"] == "correctable"])
    elif verifies or fixes:
        status = "pending"
        count = len(verifies) + len(fixes)
        headline = f"{count} item{'s' if count > 1 else ''} must be cleared before this vendor can be paid."
        owner = ("Procurement and vendor" if fixes else "Procurement (verify independently)") if verifies else "Vendor (awaiting reply)"
        message = messages.pending(name, to, r.checks)
    else:
        status = "approved"
        headline = f"All checks passed, with {len(notes)} note{'s' if len(notes) > 1 else ''} on record." if notes else "All checks passed."
        owner = "Accounts Payable (ready to pay)"
        if sample_id:  # Test cases never change the vendor master, so they can be repeated.
            vendor_id = db.next_vendor_id()
        else:
            vendor_id = db.add_vendor(s["legalName"], s["country"], s["taxId"], s["pan"], s["accountNumber"], s["contactName"], s["contactEmail"])
            added = True
        message = messages.approved(name, to, vendor_id)
    can_correct = status == "pending" or (status == "rejected" and all(b["kind"] == "correctable" for b in blocks))
    if draft_id and not can_correct:  # A decided vendor has no further use for the full details and files.
        db.delete_draft(draft_id)
    db.finish_run(run_id, status, headline, owner, vendor_id, added, ai_used, message)
    return status
