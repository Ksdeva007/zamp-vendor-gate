"""Run with:  python -m unittest discover -s tests -v   (uses a throwaway database)"""
import io
import os
import smtplib
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
os.environ["DB_PATH"] = os.path.join(_tmp.name, "test.db")
for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "APP_PASSWORD", "SMTP_HOST", "SMTP_FROM"):
    os.environ.pop(name, None)

from app import app  # noqa: E402
from portal import db, forms, validators as v  # noqa: E402
from portal.config import STAGES  # noqa: E402
from portal.documents import read_document  # noqa: E402
from portal.samples import ACME, BANK_CODE, SAMPLES, _cheque, doc, iso, make_pdf  # noqa: E402

ACCOUNT = "123456789012"
PROOF = "cheque or bank letter"


class Base(unittest.TestCase):
    def setUp(self):
        db.clear_history()
        self.client = app.test_client()

    def wait(self, run_id):
        for _ in range(300):
            run = self.client.get(f"/api/runs/{run_id}").get_json()
            if run["state"] != "running":
                self.assertEqual(run["state"], "done")
                return run
            time.sleep(0.02)
        self.fail("run did not finish")

    def submit_draft(self, draft_id):
        res = self.client.post(f"/api/drafts/{draft_id}/submit", json={"pace": False})
        self.assertEqual(res.status_code, 202, res.get_data(as_text=True))
        return self.wait(res.get_json()["id"])

    def submit(self, form, docs, sample_id=None):
        draft_id = db.create_draft({**forms.blank_values(), **form}, sample_id=sample_id, documents=docs)
        return self.submit_draft(draft_id)

    def sample(self, sid, mutate=None, as_test_case=True):
        form, docs = SAMPLES[sid]["build"]()
        if mutate:
            mutate(form, docs)
        return self.submit(form, docs, sid if as_test_case else None)

    @staticmethod
    def titles(run, status):
        return [c["title"] for s in run["stages"] for c in s["checks"] if c["status"] == status]


class Scenarios(Base):
    def test_six_stages_in_order(self):
        run = self.sample("clean")
        self.assertEqual([s["title"] for s in run["stages"]], STAGES)
        self.assertEqual(STAGES[0], "Intake & normalise")
        self.assertEqual(STAGES[4], "Cross-reference & fraud signals")

    def test_happy_path(self):
        run = self.sample("clean")
        self.assertEqual(run["status"], "approved", self.titles(run, "fixable"))
        self.assertEqual(run["decision"], {"title": "Approved", "tone": "ok"})
        self.assertEqual(run["counts"]["issues"], 0)
        self.assertEqual(run["message_subject"], f"Welcome aboard: {ACME} is approved")
        self.assertTrue(run["message_text"].startswith("Subject: Welcome aboard"))
        self.assertIn(f"Hi {ACME} team,", run["message_body"])
        self.assertIn("valid purchase order", run["message_body"])
        self.assertIn(run["vendor_id"], run["message_body"])
        self.assertIn("ABC Company", run["message_body"])
        self.assertIsNone(run["next_action"])
        for title in ["Required fields", "Normalisation", "Documents", "Certificate validity", "Tax ID", "Name vs tax certificate", "IFSC", "Account number", "Account holder", "Bank account is not used by another vendor", "Duplicate check", "Email domain", "Sanctions screen", "GSTIN matches the certificate", f"Account number matches the {PROOF}", f"IFSC matches the {PROOF}", f"Account holder matches the {PROOF}", f"Account type matches the {PROOF}"]:
            self.assertIn(title, self.titles(run, "pass"))

    def test_edge1_trade_name_and_free_mail(self):
        run = self.sample("tradename")
        self.assertEqual(run["status"], "pending")
        self.assertEqual(run["decision"]["title"], "Pending: information needed")
        self.assertEqual(self.titles(run, "fixable"), ["Account holder is not the legal name", "Contact email is personal"])
        body = run["message_body"]
        self.assertTrue(run["message_subject"].startswith("Action needed: onboarding for"))
        self.assertIn("You entered: Acme Industrial Traders", body)
        self.assertIn(f"Expected: {ACME}", body)
        self.assertIn("bank letter confirming", body)
        self.assertIn("company email address", body)
        self.assertIn("you only need to send what's listed", body)
        self.assertEqual(run["next_action"]["label"], "Load the vendor's corrected file")

    def test_edge2_message_lists_every_wrong_entry(self):
        run = self.sample("subtle")
        self.assertEqual(run["status"], "pending")
        self.assertEqual(self.titles(run, "fixable"), ["GSTIN does not match the certificate", f"Account number does not match the {PROOF}", f"IFSC does not match the {PROOF}"])
        form, _ = SAMPLES["subtle"]["build"]()
        body = run["message_body"]
        for expected in [f"You entered: {form['taxId']}", v.make_gstin("27", "AAKCK4512P") + " (as shown on your certificate)", "You entered: Ending 1089", "Ending 1098", "You entered: HDFC0001243", f"{BANK_CODE} (as shown on your {PROOF})"]:
            self.assertIn(expected, body)
        self.assertEqual(run["outcome"]["missing"], [])

    def test_edge3_rejected_without_leaking(self):
        run = self.sample("sharedbank")
        self.assertEqual(run["status"], "rejected")
        self.assertEqual(self.titles(run, "block"), ["Bank account belongs to another vendor"])
        self.assertEqual(self.titles(run, "fixable"), [])
        self.assertTrue(run["reasons_withheld"])
        self.assertIsNone(run["next_action"])
        self.assertIn("contact your business sponsor", run["message_body"])
        for secret in ("Northwind", "V-1077", "fraud", "bank account", "free-mail"):
            self.assertNotIn(secret.lower(), run["message_text"].lower())
        self.assertTrue(any("Northwind" in c["detail"] for c in run["outcome"]["internal"]))

    def test_edge4_bank_change_keeps_contact_internal(self):
        run = self.sample("bankchange")
        self.assertEqual(run["status"], "pending")
        self.assertEqual(self.titles(run, "verify"), ["Registered vendor is changing bank details"])
        self.assertEqual(self.titles(run, "fixable"), [])
        self.assertIn("Being checked on our side:", run["message_body"])
        self.assertIn("ending 8850", run["message_body"])
        self.assertNotIn("Meena", run["message_body"])
        self.assertNotIn("7812", run["message_body"])
        self.assertTrue(any("Call Meena Iyer" in c["detail"] for c in run["outcome"]["internal"]))

    def test_scenario_documents_match_their_own_details(self):
        for sid, sample in SAMPLES.items():
            form, docs = sample["build"]()
            self.assertEqual(set(docs), {"inc", "tax", "bank"}, sid)
            tax = read_document(docs["tax"], "tax")["fields"]
            bank = read_document(docs["bank"], "bank")["fields"]
            self.assertEqual(tax["legalName"], form["taxCertName"], sid)
            self.assertEqual(tax["expiry"], form["certExpiry"], sid)
            self.assertEqual(bank["holder"], form["bankHolder"], sid)
            if sid != "subtle":  # Edge 2 exists to show a form that disagrees with its documents.
                self.assertEqual(tax["taxId"], form["taxId"], sid)
                self.assertEqual((bank["account"], bank["code"]), (form["accountNumber"], form["bankCode"]), sid)

    def test_scenario_labels_fit_the_dropdown(self):
        for sample in SAMPLES.values():
            if not sample.get("hidden"):
                self.assertLessEqual(len(sample["label"]), 46, sample["label"])

    def test_each_company_has_its_own_gst_number(self):
        owners = {}
        for sample in SAMPLES.values():
            form, _ = sample["build"]()
            self.assertEqual(owners.setdefault(form["taxId"], form["legalName"]), form["legalName"])
            self.assertTrue(v.is_gstin(form["taxId"]), form["taxId"])


class DocumentsMatchTheForm(Base):
    def holds(self, mutate, title):
        run = self.sample("clean", mutate)
        self.assertEqual(run["status"], "pending")
        self.assertIn(title, self.titles(run, "fixable"))
        return run

    def test_gst_number_must_match_the_certificate(self):
        run = self.holds(lambda f, d: f.update(taxId=v.make_gstin("27", "AAKCK4512P")), "GSTIN does not match the certificate")
        self.assertIn("27AAPFU0939F1ZV (as shown on your certificate)", run["message_body"])

    def test_cheque_must_match_the_bank_details_on_the_form(self):
        self.holds(lambda f, d: f.update(accountNumber="987654321098"), f"Account number does not match the {PROOF}")
        self.holds(lambda f, d: f.update(bankCode="HDFC0005678"), f"IFSC does not match the {PROOF}")
        self.holds(lambda f, d: d.update(bank=_cheque("x", "Acme Traders Of Pune", ACCOUNT, BANK_CODE)), f"Account holder does not match the {PROOF}")
        self.holds(lambda f, d: f.update(accountType="Savings"), f"Account type does not match the {PROOF}")

    def test_cheque_must_show_the_bank_details(self):
        lines = {"Account Number": ACCOUNT, "IFSC": BANK_CODE, "Account Holder": ACME}
        for label, item in [("Account Number", "Account number"), ("IFSC", "IFSC"), ("Account Holder", "Account holder")]:
            shown = [f"{k}: {x}" for k, x in lines.items() if k != label]
            self.holds(lambda f, d: d.update(bank=doc("c.pdf", "Crossed Cheque", shown)), f"{item} not shown on the {PROOF}")

    def test_every_required_document_is_needed_for_approval(self):
        for slot in ("inc", "tax", "bank"):
            run = self.sample("clean", lambda f, d: d.pop(slot))
            self.assertEqual(run["status"], "pending", slot)
            self.assertEqual(len(run["outcome"]["missing"]), 1, slot)

    def test_details_are_found_on_a_real_style_certificate(self):
        gstin = "27AAPFU0939F1ZV"
        got = read_document(doc("g.pdf", "Goods and Services Tax", ["Registration Number : " + gstin, f"Legal Name: {ACME}"]), "tax")["fields"]
        self.assertEqual(got["taxId"], gstin)
        got = read_document(doc("g.pdf", "GST Registration Certificate", [f"{ACME} is registered under {gstin}", f"Legal Name: {ACME}"]), "tax")["fields"]
        self.assertEqual(got["taxId"], gstin)
        got = read_document(doc("c.pdf", "Crossed Cheque", [f"Account Name: {ACME}", f"A/c No.: {ACCOUNT}", f"Branch code {BANK_CODE}"]), "bank")["fields"]
        self.assertEqual((got["holder"], got["account"], got["code"]), (ACME, ACCOUNT, BANK_CODE))


class Rules(Base):
    def check(self, mutate, title, status="fixable"):
        run = self.sample("clean", mutate)
        self.assertIn(title, self.titles(run, status), self.titles(run, "fixable"))
        return run

    def test_optional_fields_are_checked_when_filled_in(self):
        self.check(lambda f, d: f.update(state="Maharashtra", pincode="110001"), "Pincode does not match the state")
        self.check(lambda f, d: f.update(mobile="12345"), "Mobile number invalid")
        self.check(lambda f, d: f.update(hsn="12"), "HSN / SAC code invalid")
        self.check(lambda f, d: f.update(udyam="123"), "Udyam number invalid")
        self.check(lambda f, d: f.update(pan="AAPCU0939F", entityType="Partnership Firm"), "PAN holder type does not match ownership")
        self.check(lambda f, d: f.update(state="Karnataka"), "GST number is registered in a different state")
        self.check(lambda f, d: f.update(pan="AAPFU0939G"), "GST number and PAN do not match")

    def test_tax_id_problems(self):
        self.check(lambda f, d: f.update(taxId="84-3927199"), "Tax ID belongs to a different country")
        self.check(lambda f, d: f.update(taxId="NOTATAXID"), "Tax ID format")

    def test_name_on_certificate_thresholds(self):
        self.assertEqual(v.name_similarity("Acme Pvt Ltd", "ACME Limited"), 1.0)
        run = self.sample("clean", lambda f, d: f.update(taxCertName="Acme Industrial Traders"))
        self.assertEqual(run["status"], "pending")
        self.assertIn("Name vs tax certificate", self.titles(run, "fixable"))
        run = self.sample("clean", lambda f, d: f.update(taxCertName="Zephyr Quantum Foods"))
        self.assertEqual(run["status"], "rejected")
        self.assertIn("Names unrelated", run["message_body"])
        self.assertEqual(run["next_action"]["label"], "Correct and resubmit")

    def test_third_party_account_is_rejected_with_the_reason(self):
        run = self.sample("clean", lambda f, d: f.update(bankHolder="Zephyr Quantum Foods"))
        self.assertEqual(run["status"], "rejected")
        self.assertEqual(self.titles(run, "block"), ["Account holder"])
        self.assertFalse(run["reasons_withheld"])
        self.assertIn("third-party account", run["message_body"])
        self.assertIn("Please resubmit", run["message_body"])

    def test_certificate_validity(self):
        self.check(lambda f, d: f.update(certExpiry=""), "Certificate validity")
        self.check(lambda f, d: f.update(certExpiry="soon"), "Certificate validity")
        self.check(lambda f, d: f.update(certExpiry=iso(10)), "Validity date differs from the certificate")
        run = self.check(lambda f, d: f.update(certExpiry=iso(-30)), "Certificate validity")
        self.assertIn(f"expired on {iso(-30)}", run["message_body"])

    def test_email_domain(self):
        self.check(lambda f, d: f.update(contactEmail="a@other-domain.example"), "Email domain")
        self.check(lambda f, d: f.update(contactEmail="not-an-address"), "Contact email invalid")

    def test_notes_do_not_hold(self):
        def savings(f, d):
            f.update(accountType="Savings")
            d.update(bank=doc("c.pdf", "Crossed Cheque", [f"Account Holder: {ACME}", "Account Type: Savings", f"Account Number: {ACCOUNT}", f"IFSC: {BANK_CODE}"]))
        run = self.check(savings, "Company uses a savings account", "warn")
        self.assertEqual(run["status"], "approved", self.titles(run, "fixable"))

    def test_pan_without_gst(self):
        def change(f, d):
            f.update(vendorType="PAN without GST", pan="AAPFU0939F")
            del d["tax"]
        self.assertEqual(self.sample("clean", change)["status"], "approved")

    def test_all_gaps_listed_at_once(self):
        def change(f, d):
            f.update(bankHolder="", accountNumber="")
            d.clear()
        run = self.sample("clean", change)
        self.assertEqual(run["status"], "pending")
        for item in ["Bank account holder name", "Account no. / IBAN", "Tax registration certificate (document)", "Certificate of incorporation (document)"]:
            self.assertIn(f"- {item}", run["message_body"])

    def test_sanctions_screen_reads_the_database(self):
        for name in ("Crimson Harbor Trading Pvt Ltd", "Orion Trade Links FZE"):
            run = self.sample("clean", lambda f, d: f.update(legalName=name, taxCertName=name, bankHolder=name))
            self.assertEqual(run["status"], "rejected")
            self.assertIn("Sanctions screen", self.titles(run, "block"))
        run = self.sample("clean", lambda f, d: f.update(bankHolder="Blackwater Shell Holdings"))
        self.assertIn("Sanctions screen", self.titles(run, "block"))
        with db.connect() as c:
            c.execute("INSERT OR REPLACE INTO watchlist VALUES ('Acme Industrial Supplies', 'Test')")
        try:
            self.assertEqual(self.sample("clean")["status"], "rejected")
        finally:
            with db.connect() as c:
                c.execute("DELETE FROM watchlist WHERE why='Test'")

    def test_duplicates_and_lookalike(self):
        run = self.sample("bankchange", lambda f, d: (f.update(accountNumber="50100234567812", contactEmail="m@kaverisupplies.example"), d.pop("bank")))
        self.assertEqual(run["status"], "rejected")
        self.assertIn("already registered", run["message_body"])
        name = "Contoso Supplies LLC"
        run = self.sample("clean", lambda f, d: f.update(legalName=name, taxCertName=name, bankHolder=name))
        self.assertEqual(run["status"], "rejected")
        self.assertIn("Duplicate vendor", self.titles(run, "block"))
        name = "Kaveri Industrial Suppliers Private Limited"
        run = self.sample("clean", lambda f, d: f.update(legalName=name, taxCertName=name, bankHolder=name))
        self.assertIn("Name closely resembles a registered vendor", self.titles(run, "verify"))
        self.assertNotIn("V-1042", run["message_body"])

    def test_other_country_goes_to_a_person(self):
        run = self.sample("sharedbank", lambda f, d: (f.update(country="OT", accountNumber="DE89370400440532013000"), d.pop("bank")))
        self.assertEqual(run["status"], "pending")
        self.assertIn("No validation rules for this country", self.titles(run, "verify"))

    def _foreign(self, name, country, tax_id, account, code, extra_docs):
        slug = country.lower()
        form = {"legalName": name, "country": country, "taxId": tax_id, "taxCertName": name, "bankHolder": name, "bankCode": code, "accountNumber": account, "contactEmail": f"lee@{slug}vendor.example", "website": f"https://{slug}vendor.example", "certExpiry": iso(100)}
        docs = {
            "tax": doc("t.pdf", "Tax Registration Certificate", [f"Tax ID: {tax_id}", f"Legal Name: {name}"]),
            "bank": doc("b.pdf", "Bank Confirmation Letter", [f"Account Holder: {name}", f"Account Number: {account}", f"Routing Number: {code}"]),
            "inc": doc("i.pdf", "Certificate of Formation", [f"Company Name: {name}", "Date of Incorporation: 2015-04-01"]),
            **extra_docs,
        }
        return self.submit(form, docs)

    def test_us_vendor_needs_w9(self):
        name = "Prairie Valve Works Inc"
        w9 = {"w9": doc("w.pdf", "Form W-9", [f"Name: {name}"])}
        run = self._foreign(name, "US", "36-4418207", "883301245", v.make_aba("07100001"), w9)
        self.assertEqual(run["status"], "approved", self.titles(run, "fixable"))
        db.clear_history()
        run = self._foreign(name, "US", "36-4418207", "883301245", v.make_aba("07100001"), {})
        self.assertEqual(run["outcome"]["missing"], ["Form W-9 (US vendors only) (document)"])
        self.assertIn("ABA routing number matches the cheque or bank letter", self.titles(run, "pass"))

    def test_german_and_uk_vendors(self):
        iban = v.make_iban("DE", "370400440532013000")
        run = self._foreign("Rhein Werkzeug GmbH", "DE", "DE123456789", iban, "", {})
        self.assertEqual(run["status"], "approved", self.titles(run, "fixable"))
        self.assertIn("IBAN", self.titles(run, "pass"))
        run = self._foreign("Rhein Maschinen GmbH", "DE", "DE12345", iban[:-1] + "9", "", {})
        self.assertIn("Tax ID format", self.titles(run, "fixable"))
        self.assertIn("IBAN", self.titles(run, "fixable"))
        uk = v.make_iban("GB", "NWBK60161331926819")
        run = self._foreign("Harbour Clean Ltd", "GB", v.make_gb_vat("3915208"), uk, "", {})
        self.assertEqual(run["status"], "approved", self.titles(run, "fixable"))


class FormApi(Base):
    def new(self, **body):
        res = self.client.post("/api/drafts", json=body)
        self.assertEqual(res.status_code, 201)
        return res.get_json()

    @staticmethod
    def keys(view):
        return [f["key"] for s in view["sections"] for f in s["fields"]]

    def test_main_section_has_the_reference_fields_in_order(self):
        d = self.new()
        self.assertEqual([f["label"] for f in d["sections"][0]["fields"]], ["Legal name", "Country", "Tax ID (GSTIN / EIN / VAT)", "Name on tax certificate", "Bank account holder name", "IFSC / routing no. (blank for IBAN)", "Account no. / IBAN", "Contact email", "Website", "Tax certificate valid until"])
        self.assertEqual([o["label"] for o in d["sections"][0]["fields"][1]["options"]], ["India", "United States", "United Kingdom", "Germany", "Other country"])
        self.assertEqual([s["title"] for s in d["sections"][1:]], ["Tax details", "Organisation", "Contact person", "Bank branch", "Services"])
        self.assertEqual([k for k, _ in forms.required_fields(forms.blank_values())], ["legalName", "taxId", "taxCertName", "bankHolder", "accountNumber", "contactEmail"])

    def test_server_decides_fields_and_documents(self):
        d = self.new()
        slots = lambda view: {x["slot"]: x["required"] for x in view["documents"]}
        self.assertEqual([s for s, req in slots(d).items() if req], ["inc", "tax", "bank"])
        self.assertIn("pan", self.keys(d))
        d = self.client.patch(f"/api/drafts/{d['id']}", json={"vendorType": "PAN without GST"}).get_json()
        self.assertNotIn("taxId", self.keys(d))
        self.assertNotIn("tax", slots(d))
        self.assertIn("PAN number", d["missing"])
        d = self.client.patch(f"/api/drafts/{d['id']}", json={"country": "US"}).get_json()
        self.assertIn("taxId", self.keys(d))
        self.assertNotIn("pan", self.keys(d))
        self.assertTrue(slots(d)["w9"])
        self.assertFalse(slots(d)["trc"])
        d = self.client.patch(f"/api/drafts/{d['id']}", json={"category": "On-site services"}).get_json()
        self.assertTrue(slots(d)["ins"])

    def test_scenario_loads_and_editing_clears_it(self):
        d = self.new(sample_id="clean")
        self.assertEqual(d["sample_id"], "clean")
        self.assertEqual(d["missing"], [])
        d = self.client.patch(f"/api/drafts/{d['id']}", json={"website": "https://www.acmeindustrial.in"}).get_json()
        self.assertEqual(d["sample_id"], "clean", "no real change")
        d = self.client.patch(f"/api/drafts/{d['id']}", json={"city": "Salem"}).get_json()
        self.assertIsNone(d["sample_id"])

    def test_discard_draft(self):
        d = self.new()
        self.assertEqual(self.client.delete(f"/api/drafts/{d['id']}").status_code, 204)
        self.assertEqual(self.client.get(f"/api/drafts/{d['id']}").status_code, 404)
        d = self.new(sample_id="clean")
        self.submit_draft(d["id"])
        self.assertEqual(self.client.delete(f"/api/drafts/{d['id']}").status_code, 404)
        d = self.new(sample_id="subtle")
        self.submit_draft(d["id"])
        self.assertEqual(self.client.delete(f"/api/drafts/{d['id']}").status_code, 409, "kept for correction")

    def test_bad_edits_rejected(self):
        d = self.new()
        self.assertEqual(self.client.patch(f"/api/drafts/{d['id']}", json={"nope": "x"}).status_code, 400)
        self.assertEqual(self.client.patch(f"/api/drafts/{d['id']}", json={"city": 5}).status_code, 400)
        self.assertEqual(self.client.patch(f"/api/drafts/{d['id']}", json={"city": "x" * 301}).status_code, 400)
        self.assertEqual(self.client.patch("/api/drafts/none", json={}).status_code, 404)

    def test_upload_download_remove(self):
        d = self.new()
        pdf = make_pdf("Cancelled Cheque", ["Account Holder: X"])
        res = self.client.post(f"/api/drafts/{d['id']}/documents/bank", data={"file": (io.BytesIO(pdf), "cheque.pdf")}, content_type="multipart/form-data")
        self.assertEqual(res.status_code, 200)
        row = next(x for x in res.get_json()["documents"] if x["slot"] == "bank")
        self.assertEqual(row["file"]["name"], "cheque.pdf")
        got = self.client.get(f"/api/drafts/{d['id']}/documents/bank")
        self.assertEqual(got.data, pdf)
        res = self.client.delete(f"/api/drafts/{d['id']}/documents/bank")
        self.assertIsNone(next(x for x in res.get_json()["documents"] if x["slot"] == "bank")["file"])

    def test_upload_checks_contents_size_and_slot(self):
        d = self.new()
        post = lambda slot, data, name: self.client.post(f"/api/drafts/{d['id']}/documents/{slot}", data={"file": (io.BytesIO(data), name)}, content_type="multipart/form-data")
        res = post("bank", b"this is not a pdf", "fake.pdf")
        self.assertEqual(res.status_code, 400)
        self.assertIn("not a PDF", res.get_json()["error"])
        self.assertEqual(post("bank", b"%PDF" + b"x" * (5 * 1024 * 1024), "big.pdf").status_code, 400)
        self.assertEqual(post("nope", make_pdf("A", []), "a.pdf").status_code, 404)

    def test_submit_rules(self):
        d = self.new()
        res = self.client.post(f"/api/drafts/{d['id']}/submit", json={})
        self.assertEqual(res.status_code, 202)
        run_id = res.get_json()["id"]
        again = self.client.post(f"/api/drafts/{d['id']}/submit", json={})
        self.assertEqual((again.status_code, again.get_json()["id"]), (200, run_id))
        self.assertEqual(self.client.patch(f"/api/drafts/{d['id']}", json={"city": "x"}).status_code, 409)
        self.wait(run_id)

    def test_resubmit_with_fix_sample_links_and_approves(self):
        d = self.new(sample_id="tradename")
        first = self.submit_draft(d["id"])
        res = self.client.post(f"/api/runs/{first['id']}/resubmit")
        self.assertEqual(res.status_code, 201)
        fixed = res.get_json()
        self.assertEqual(fixed["parent_id"], first["id"])
        self.assertIn("bank-letter", next(x for x in fixed["documents"] if x["slot"] == "bank")["file"]["name"])
        second = self.submit_draft(fixed["id"])
        self.assertEqual((second["status"], second["parent_id"]), ("approved", first["id"]), self.titles(second, "fixable"))
        self.assertIsNone(db.get_draft(fixed["id"]), "decided registration should lose its stored details")

    def test_resubmit_keeps_entries_and_files(self):
        d = self.new(sample_id="subtle")
        first = self.submit_draft(d["id"])
        self.assertIsNotNone(db.get_draft(d["id"]), "pending registration is kept for correction")
        reopened = self.client.post(f"/api/runs/{first['id']}/resubmit").get_json()
        self.assertEqual(reopened["id"], d["id"])
        self.assertIsNone(reopened["submitted_run_id"])
        self.assertIn("kaveri-precision-incorporation.pdf", [x["file"]["name"] for x in reopened["documents"] if x["file"]])
        self.assertEqual(self.client.post(f"/api/runs/{first['id']}/resubmit").get_json()["id"], d["id"])

    def test_resubmit_after_details_are_gone(self):
        first = self.sample("subtle")
        with db.connect() as c:
            c.execute("DELETE FROM drafts")
        d = self.client.post(f"/api/runs/{first['id']}/resubmit").get_json()
        self.assertEqual(d["parent_id"], first["id"])
        self.assertIn("Account no. / IBAN", d["missing"])
        self.assertEqual(d["sections"][0]["fields"][0]["value"], "Kaveri Precision Components Pvt Ltd")

    def test_who_can_resubmit(self):
        for sid in ("clean", "sharedbank"):
            run = self.sample(sid)
            self.assertEqual(self.client.post(f"/api/runs/{run['id']}/resubmit").status_code, 400, sid)
        run = self.sample("clean", lambda f, d: f.update(bankHolder="Zephyr Quantum Foods"))
        self.assertEqual(self.client.post(f"/api/runs/{run['id']}/resubmit").status_code, 201)


class VendorMessage(Base):
    def test_person_can_edit_until_sent(self):
        run = self.sample("subtle")
        url = f"/api/runs/{run['id']}/message"
        res = self.client.patch(url, json={"text": "Subject: New subject\n\nNew body"}).get_json()
        self.assertEqual((res["message_subject"], res["message_body"]), ("New subject", "New body"))
        res = self.client.patch(url, json={"text": "Only a body"}).get_json()
        self.assertEqual((res["message_subject"], res["message_body"]), ("New subject", "Only a body"))
        self.assertEqual(self.client.patch(url, json={"text": "  "}).status_code, 400)
        self.assertEqual(self.client.patch(url, json={"text": "Subject: only"}).status_code, 400)
        self.assertEqual(self.client.patch(url, json={"text": "x" * 20001}).status_code, 400)
        self.client.post(f"/api/runs/{run['id']}/mark-sent")
        self.assertEqual(self.client.patch(url, json={"text": "Too late"}).status_code, 409)
        self.assertEqual(self.client.get(f"/api/runs/{run['id']}").get_json()["message_body"], "Only a body")

    def test_polish_only_with_ai(self):
        run = self.sample("subtle")
        url = f"/api/runs/{run['id']}/polish"
        self.assertEqual(self.client.post(url).status_code, 503)
        self.assertFalse(self.client.get("/api/config").get_json()["ai"])
        with mock.patch("app.ai_available", return_value=True), mock.patch("portal.messages.polish", return_value="Reworded"):
            self.assertEqual(self.client.post(url).get_json()["message_body"], "Reworded")
        with mock.patch("app.ai_available", return_value=True), mock.patch("portal.messages.polish", return_value=None):
            self.assertEqual(self.client.post(url).status_code, 502)
        self.assertEqual(self.client.get(f"/api/runs/{run['id']}").get_json()["message_body"], "Reworded")

    def test_email_only_when_configured_and_never_for_scenarios(self):
        scenario = self.sample("subtle")
        real = self.sample("subtle", as_test_case=False)
        self.assertEqual(self.client.post(f"/api/runs/{real['id']}/send").status_code, 503)
        with mock.patch.dict(os.environ, {"SMTP_HOST": "mail.test", "SMTP_FROM": "ap@abc.test"}), mock.patch("smtplib.SMTP") as smtp:
            self.assertTrue(self.client.get("/api/config").get_json()["email"])
            self.assertEqual(self.client.post(f"/api/runs/{scenario['id']}/send").status_code, 400)
            smtp.assert_not_called()

            smtp.return_value.__enter__.return_value.send_message.side_effect = smtplib.SMTPException("down")
            self.assertEqual(self.client.post(f"/api/runs/{real['id']}/send").status_code, 502)
            self.assertIsNone(self.client.get(f"/api/runs/{real['id']}").get_json()["message_sent_at"])

            smtp.return_value.__enter__.return_value.send_message.side_effect = None
            res = self.client.post(f"/api/runs/{real['id']}/send")
            self.assertEqual(res.status_code, 200)
            self.assertTrue(res.get_json()["message_sent_at"])
            sent = smtp.return_value.__enter__.return_value.send_message.call_args[0][0]
            self.assertEqual(sent["To"], "accounts@kavericomponents.in")
            self.assertEqual(self.client.post(f"/api/runs/{real['id']}/send").status_code, 409)


class Password(Base):
    def test_password_gate(self):
        with mock.patch.dict(os.environ, {"APP_PASSWORD": "s3cret"}):
            self.assertEqual(self.client.get("/").status_code, 401)
            self.assertEqual(self.client.delete("/api/runs").status_code, 401)
            self.assertEqual(self.client.get("/api/config", auth=("admin", "wrong")).status_code, 401)
            self.assertEqual(self.client.get("/api/config", auth=("admin", "s3cret")).status_code, 200)
        self.assertEqual(self.client.get("/api/config").status_code, 200)


class BadFiles(Base):
    def test_wrong_scanned_and_damaged(self):
        def wrong(f, d):
            d["bank"] = d["tax"]
        run = self.sample("clean", wrong)
        self.assertTrue(any(t.startswith("Wrong file uploaded") for t in self.titles(run, "fixable")))
        for data in (make_pdf("", []), b"%PDF-1.4 broken"):
            run = self.sample("clean", lambda f, d: d.update(tax={"name": "x.pdf", "bytes": data, "type": "application/pdf"}))
            self.assertEqual(run["status"], "pending")
            self.assertTrue(any(t.startswith("Could not read") for t in self.titles(run, "fixable")), data)

    def test_ai_reader_values_still_face_the_rules(self):
        d = read_document({"name": "scan.pdf", "bytes": make_pdf("", [])}, "tax", ai_reader=lambda data, mime: {"docType": "tax", "fields": {"legalName": ACME}})
        self.assertEqual((d["method"], d["docType"]), ("ai", "tax"))
        d = read_document({"name": "scan.pdf", "bytes": make_pdf("", [])}, "tax", ai_reader=lambda data, mime: None)
        self.assertEqual(d["error"], "scanned")


class Storage(Base):
    def test_sqlite_contents(self):
        run = self.sample("clean", as_test_case=False)
        self.assertEqual(run["status"], "approved")
        self.assertTrue(run["added_to_master"])
        with sqlite3.connect(db.DB_PATH) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 1)
            self.assertGreater(c.execute("SELECT COUNT(*) FROM checks").fetchone()[0], 10)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM drafts").fetchone()[0], 0)
            self.assertEqual(c.execute("SELECT COUNT(*) FROM draft_documents").fetchone()[0], 0)
            dump = "\n".join(str(row) for t in ("runs", "checks", "vendors") for row in c.execute(f"SELECT * FROM {t}"))
        self.assertNotIn(ACCOUNT, dump)
        self.assertNotIn(ACCOUNT, str(run["submission"]))
        self.assertNotIn("Select", str(run["submission"]), "a blank choice is left out")
        vendors = self.client.get("/api/vendors").get_json()
        self.assertEqual(len(vendors), 4)
        self.assertNotIn("acct_hash", vendors[0])
        self.assertEqual(self.sample("clean", as_test_case=False)["status"], "rejected")
        sent = self.client.post(f"/api/runs/{run['id']}/mark-sent").get_json()
        self.assertTrue(sent["message_sent_at"])
        self.assertEqual(self.client.get(f"/api/runs/{run['id']}/audit").status_code, 200)

    def test_search_filter_totals_and_paging_in_sql(self):
        ids = {sid: self.sample(sid)["id"] for sid in ("clean", "subtle", "sharedbank")}
        data = self.client.get("/api/runs").get_json()
        self.assertEqual(data["stats"], {"all": 3, "approved": 1, "pending": 1, "rejected": 1})
        self.assertEqual(data["total"], 3)
        self.assertEqual(data["items"][0]["id"], ids["sharedbank"])
        self.assertEqual(data["items"][0]["main_reason"], "Bank account belongs to another vendor")
        self.assertEqual(data["items"][0]["country"], "IN")
        data = self.client.get("/api/runs?status=pending").get_json()
        self.assertEqual([r["id"] for r in data["items"]], [ids["subtle"]])
        self.assertEqual(data["stats"]["all"], 3)
        self.assertEqual(data["items"][0]["issue_count"], 3)
        self.assertEqual(self.client.get("/api/runs?q=meridian").get_json()["total"], 1)
        self.assertEqual(self.client.get(f"/api/runs?q={ids['clean']}").get_json()["total"], 1)
        self.assertEqual(self.client.get("/api/runs?q=%25").get_json()["total"], 0, "a % must be searched literally")
        self.assertEqual(self.client.get("/api/runs?offset=2").get_json()["items"][0]["id"], ids["clean"])

    def test_total_includes_runs_in_progress(self):
        self.sample("clean")
        db.create_run("REG-BUSY", "X", "IN", None, None, {})
        self.assertEqual(self.client.get("/api/runs").get_json()["stats"]["all"], 2)

    def test_test_cases_leave_master_alone(self):
        for _ in range(2):
            self.assertEqual(self.sample("clean")["status"], "approved")
        self.assertEqual(len(self.client.get("/api/vendors").get_json()), 3)

    def test_clear_history(self):
        self.sample("clean", as_test_case=False)
        self.sample("subtle")
        self.assertEqual(self.client.delete("/api/runs").status_code, 204)
        self.assertEqual(self.client.get("/api/runs").get_json()["total"], 0)
        self.assertEqual(len(self.client.get("/api/vendors").get_json()), 3)
        with sqlite3.connect(db.DB_PATH) as c:
            self.assertEqual(c.execute("SELECT COUNT(*) FROM drafts").fetchone()[0], 0)

    def test_vendor_ids_do_not_repeat(self):
        first = self.sample("clean", as_test_case=False)["vendor_id"]
        self.assertEqual(first, "V-2001")
        def different(f, d):
            f.update(legalName="Blue Finch Traders", taxCertName="Blue Finch Traders", bankHolder="Blue Finch Traders", taxId=v.make_gstin("27", "AAFCB1234C"), accountNumber="998877665544")
            d.clear()
            d.update(tax=doc("t.pdf", "GST Registration Certificate", ["Legal Name: Blue Finch Traders", f"GSTIN: {f['taxId']}"]), inc=doc("i.pdf", "Certificate of Incorporation", ["Company Name: Blue Finch Traders"]), bank=doc("b.pdf", "Cancelled Cheque", ["Account Holder: Blue Finch Traders", "Account Number: 998877665544", f"IFSC: {BANK_CODE}"]))
        second = self.sample("clean", different, as_test_case=False)
        self.assertEqual(second["vendor_id"], "V-2002", self.titles(second, "fixable"))

    def test_restart_ends_stuck_runs_and_old_drafts(self):
        db.create_run("REG-STUCK", "X", "IN", None, None, {})
        old = db.create_draft({})
        with db.connect() as c:
            c.execute("UPDATE drafts SET updated_at='2000-01-01T00:00:00+00:00' WHERE id=?", (old,))
        db.init()
        self.assertEqual(db.get_run("REG-STUCK")["state"], "error")
        self.assertIsNone(db.get_draft(old))

    def test_missing_records(self):
        self.assertEqual(self.client.get("/api/runs/none").status_code, 404)
        self.assertEqual(self.client.post("/api/runs/none/mark-sent").status_code, 404)
        self.assertEqual(self.client.post("/api/runs/none/resubmit").status_code, 404)
        self.assertEqual(self.client.post("/api/runs/none/send").status_code, 404)
        self.assertEqual(self.client.post("/api/runs/none/polish").status_code, 404)
        self.assertEqual(self.client.patch("/api/runs/none/message", json={"text": "x"}).status_code, 404)
        self.assertEqual(self.client.post("/api/drafts", json={"sample_id": "none"}).status_code, 404)
        rules = self.client.get("/api/rules").get_json()
        self.assertEqual([s["title"] for s in rules["statuses"]], ["Approved", "Pending", "Rejected"])


if __name__ == "__main__":
    unittest.main()
