"""Turns stored runs into what the screens show, so the browser doesn't interpret any results."""
from . import forms
from .documents import ai_available
from .samples import SAMPLES

STATUS_LABELS = {"pass": "Passed", "warn": "Note", "fixable": "Vendor action", "verify": "Internal check", "block": "Failed", "info": "Info", "skip": "Skipped"}
TONES = {"pass": "ok", "warn": "note", "fixable": "warn", "verify": "warn", "block": "block", "info": "note", "skip": "note"}
SYMBOLS = {"ok": "✓", "note": "i", "warn": "!", "block": "✕"}
DECISION_TITLES = {"approved": "Approved", "pending": "Pending: information needed", "rejected": "Rejected"}
DECISION_TONES = {"approved": "ok", "pending": "warn", "rejected": "block"}
ISSUES = ("fixable", "verify", "block")


def can_resubmit(run):
    """A pending vendor can correct their entries. So can one rejected for a reason they were told about."""
    if run["status"] == "pending":
        return True
    blocks = [c for s in run["stages"] for c in s["checks"] if c["status"] == "block"]
    return run["status"] == "rejected" and bool(blocks) and all(c["kind"] == "correctable" for c in blocks)


def _stage_tone(stage):
    statuses = {c["status"] for c in stage["checks"]}
    if "block" in statuses:
        return "block"
    return "warn" if statuses & {"fixable", "verify"} else "ok"


def run_view(run):
    all_checks = [c for s in run["stages"] for c in s["checks"]]
    for c in all_checks:
        c["status_label"] = STATUS_LABELS[c["status"]]
        c["tone"] = TONES[c["status"]]
        c["symbol"] = SYMBOLS[c["tone"]]
    for s in run["stages"]:
        s["state"] = "done" if s["ms"] is not None else "active" if run["active_stage"] == s["idx"] else "waiting"
        s["tone"] = _stage_tone(s) if s["state"] == "done" else "run"

    counted = [c for c in all_checks if c["status"] not in ("info", "skip")]
    issues = [c for c in counted if c["status"] in ISSUES]
    run["counts"] = {"total": len(counted), "passed": len(counted) - len(issues), "issues": len(issues)}
    run["progress"] = {"done": sum(s["state"] == "done" for s in run["stages"]), "total": len(run["stages"])}
    run["decision"] = {"title": DECISION_TITLES[run["status"]], "tone": DECISION_TONES[run["status"]]} if run["status"] else None
    run["outcome"] = {
        "issues": issues,
        "entries": [c for c in issues if c["kind"] == "entry"],
        "missing": [c["field"] for c in issues if c["kind"] == "missing"],
        "internal": [c for c in issues if c["status"] in ("verify", "block")],
        "notes": [c for c in counted if c["status"] == "warn"],
    }
    run["message_text"] = f"Subject: {run['message_subject']}\n\n{run['message_body']}" if run["message_body"] else None
    run["reasons_withheld"] = any(c["status"] == "block" and c["kind"] == "risk" for c in all_checks)

    run["next_action"] = None
    if can_resubmit(run):
        sample = SAMPLES.get(run["sample_id"] or "")
        run["next_action"] = {"label": "Load the vendor's corrected file" if sample and sample.get("fix") else "Correct and resubmit"}
    run["submission"] = forms.submission_summary(run.pop("form", {}))
    return run


def rules_view():
    return {
        "statuses": [
            {
                "title": "Approved", "tone": "ok", "when": "Nothing is left to fix or confirm.",
                "examples": ["All details match the documents", "All required documents are uploaded", "Only a note is left, such as a savings account"],
                "message": "A welcome message with the Vendor ID.",
            },
            {
                "title": "Pending", "tone": "warn", "when": "Something must be fixed or confirmed first.",
                "examples": ["GST number differs from the certificate", "A required document is missing", "Registered vendor changes bank account"],
                "message": "A message saying what is needed.",
            },
            {
                "title": "Rejected", "tone": "block", "when": "The registration cannot go ahead.",
                "examples": ["Bank account belongs to another vendor", "Company is already registered", "Company is on the sanctions list"],
                "message": "A rejection message. Fraud and sanctions reasons are not shared.",
            },
        ],
        "checks": [
            {"title": "Documents and tax", "items": ["All required documents are uploaded", "Tax number has a valid format", "Tax number and legal name match the certificate", "PAN matches the GST number"]},
            {"title": "Bank account", "items": ["Account number and bank code are valid", "Account holder is the legal name", "Cheque or bank letter matches the form", "Account is not used by another vendor"]},
            {"title": "Fraud and risk", "items": ["Company is not already registered", "Bank changes are confirmed by a call", "Contact email is a company email", "Company is not on the sanctions list"]},
        ],
        "notes": [
            {"title": "Countries", "text": "Rules cover India, the US, the UK and Germany. A person reviews other countries."},
            {"title": "Use of AI", "text": "Rules make every decision. AI only reads unclear documents and rewords messages. AI is currently " + ("on." if ai_available() else "off.")},
            {"title": "Sample data", "text": "The vendor list and sanctions list are sample data. Nothing is checked live with the GST portal or banks yet."},
        ],
    }
