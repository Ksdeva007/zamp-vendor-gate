"""VendorGate: Flask API over SQLite, with a thin web UI in static/.

Run with:  python app.py   then open http://127.0.0.1:5000
"""
import hmac
import io
import json
import logging
import os
import secrets
import smtplib
import threading
from datetime import datetime

from flask import Flask, Response, jsonify, request, send_file, send_from_directory

from portal import config, db, forms, mailer, messages, views
from portal.documents import ai_available, sniff_type
from portal.pipeline import mask_form, normalise, run_pipeline
from portal.samples import SAMPLES

log = logging.getLogger(__name__)
app = Flask(__name__, static_folder="static", static_url_path="/static")
app.config["MAX_CONTENT_LENGTH"] = config.MAX_UPLOAD_BYTES + 1024 * 1024
db.init()

PAGE_SIZE = 20
MAX_MESSAGE_LENGTH = 20000


def error(message, code):
    return jsonify(error=message), code


@app.before_request
def require_password():
    """Set APP_PASSWORD to ask for a password on every page. Any username works."""
    password = os.environ.get("APP_PASSWORD")
    if not password:
        return None
    given = request.authorization.password if request.authorization and request.authorization.type == "basic" else None
    if given and hmac.compare_digest(given.encode(), password.encode()):
        return None
    return Response("Password required.", 401, {"WWW-Authenticate": 'Basic realm="VendorGate"'})


def _process(run_id, draft_id, sub, docs, sample_id, pace):
    try:
        run_pipeline(run_id, sub, docs, sample_id=sample_id, pace=pace, draft_id=draft_id)
    except Exception:
        log.exception("Run %s stopped on an unexpected error", run_id)
        db.fail_run(run_id)


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/config")
def get_config():
    return jsonify(name=config.APP_NAME, tagline=config.APP_TAGLINE, buyer=config.BUYER, stages=config.STAGES, ai=ai_available(), email=mailer.available())


@app.get("/api/start-options")
def start_options():
    return jsonify([{"id": k, "label": v["label"]} for k, v in SAMPLES.items() if not v.get("hidden")])


# Drafts: the submission being filled in
def _new_draft_from_sample(sample_id, parent_id=None):
    form, docs = SAMPLES[sample_id]["build"]()
    return db.create_draft({**forms.blank_values(), **form}, sample_id=sample_id, parent_id=parent_id, documents=docs)


@app.post("/api/drafts")
def create_draft():
    body = request.get_json(silent=True) or {}
    sample_id = body.get("sample_id")
    if sample_id and sample_id not in SAMPLES:
        return error("Unknown test scenario.", 404)
    draft_id = _new_draft_from_sample(sample_id) if sample_id else db.create_draft(forms.blank_values())
    return jsonify(forms.draft_view(db.get_draft(draft_id))), 201


def _open_draft(draft_id):
    """Returns (draft, None), or (None, error response) if it's missing or already submitted."""
    draft = db.get_draft(draft_id)
    if not draft:
        return None, error("This registration is no longer available.", 404)
    if draft["submitted_run_id"]:
        return None, error("This registration has already been submitted.", 409)
    return draft, None


@app.get("/api/drafts/<draft_id>")
def get_draft(draft_id):
    draft = db.get_draft(draft_id)
    return jsonify(forms.draft_view(draft)) if draft else error("This registration is no longer available.", 404)


@app.patch("/api/drafts/<draft_id>")
def edit_draft(draft_id):
    draft, problem = _open_draft(draft_id)
    if problem:
        return problem
    edits = request.get_json(silent=True)
    if not isinstance(edits, dict):
        return error("The form data could not be read.", 400)
    values, sample_id, message = forms.apply_edits(draft["values"], edits, draft["sample_id"])
    if message:
        return error(message, 400)
    db.update_draft(draft_id, values, sample_id)
    return jsonify(forms.draft_view(db.get_draft(draft_id)))


@app.delete("/api/drafts/<draft_id>")
def discard_draft(draft_id):
    """Removes an unsubmitted form, such as when another scenario is loaded."""
    _, problem = _open_draft(draft_id)
    if problem:
        return problem
    db.delete_draft(draft_id)
    return "", 204


@app.post("/api/drafts/<draft_id>/documents/<slot>")
def upload_document(draft_id, slot):
    _, problem = _open_draft(draft_id)
    if problem:
        return problem
    if slot not in config.DOC_SLOTS:
        return error("Unknown document type.", 404)
    upload = request.files.get("file")
    if not upload:
        return error("No file was received.", 400)
    data = upload.read()
    name = os.path.basename(upload.filename or slot)
    if len(data) > config.MAX_UPLOAD_BYTES:
        return error(f"{name} is larger than 5 MB.", 400)
    mime = sniff_type(data)
    if not mime:  # Judged by the file's contents, not its name.
        return error(f"{name} is not a PDF, JPG or PNG.", 400)
    db.put_document(draft_id, slot, name, mime, data)
    return jsonify(forms.draft_view(db.get_draft(draft_id)))


@app.delete("/api/drafts/<draft_id>/documents/<slot>")
def remove_document(draft_id, slot):
    _, problem = _open_draft(draft_id)
    if problem:
        return problem
    db.delete_document(draft_id, slot)
    return jsonify(forms.draft_view(db.get_draft(draft_id)))


@app.get("/api/drafts/<draft_id>/documents/<slot>")
def download_document(draft_id, slot):
    doc = db.get_document(draft_id, slot)
    if not doc:
        return error("No such document.", 404)
    return send_file(io.BytesIO(doc["data"]), mimetype=doc["mime"] or "application/octet-stream", as_attachment=True, download_name=doc["name"])


@app.post("/api/drafts/<draft_id>/submit")
def submit_draft(draft_id):
    draft = db.get_draft(draft_id)
    if not draft:
        return error("This registration is no longer available.", 404)
    if draft["submitted_run_id"]:  # A double click returns the run already started.
        return jsonify(id=draft["submitted_run_id"]), 200

    sub = normalise(draft["values"])
    docs = db.load_documents(draft_id, [*config.required_docs(sub), *config.optional_docs(sub)])
    parent_id = draft["parent_id"] if draft["parent_id"] and db.run_exists(draft["parent_id"]) else None
    run_id = f"REG-{datetime.now():%y%m%d}-{secrets.token_hex(2).upper()}"
    db.create_run(run_id, sub["legalName"] or "(unnamed)", sub["country"], draft["sample_id"], parent_id, mask_form(sub))
    db.mark_draft_submitted(draft_id, run_id)
    pace = bool((request.get_json(silent=True) or {}).get("pace"))
    threading.Thread(target=_process, args=(run_id, draft_id, sub, docs, draft["sample_id"], pace), daemon=True).start()
    return jsonify(id=run_id), 202


# Submissions
@app.get("/api/runs")
def list_runs():
    status = request.args.get("status")
    if status not in ("approved", "pending", "rejected"):
        status = None
    offset = max(request.args.get("offset", 0, type=int), 0)
    return jsonify(db.search_runs(status=status, query=(request.args.get("q") or "").strip() or None, limit=PAGE_SIZE, offset=offset))


@app.get("/api/runs/<run_id>")
def get_run(run_id):
    run = db.get_run(run_id)
    return jsonify(views.run_view(run)) if run else error("No such submission.", 404)


@app.get("/api/runs/<run_id>/audit")
def audit(run_id):
    run = db.get_run(run_id)
    if not run:
        return error("No such submission.", 404)
    return send_file(io.BytesIO(json.dumps(run, indent=2).encode()), mimetype="application/json", as_attachment=True, download_name=f"{run_id}-audit.json")


@app.patch("/api/runs/<run_id>/message")
def edit_message(run_id):
    """Saves the message as edited. An edited "Subject:" first line becomes the subject."""
    text = (request.get_json(silent=True) or {}).get("text")
    run = db.get_run(run_id)
    if not run:
        return error("No such submission.", 404)
    if not isinstance(text, str) or not text.strip():
        return error("The message can't be empty.", 400)
    if len(text) > MAX_MESSAGE_LENGTH:
        return error("That message is too long.", 400)
    subject, body = run["message_subject"], text.strip()
    if body.startswith("Subject:"):
        first, _, rest = body.partition("\n")
        subject, body = first[len("Subject:"):].strip() or subject, rest.strip()
    if not body:
        return error("The message can't be empty.", 400)
    if not db.update_message(run_id, subject, body):
        return error("This message has already been sent, so it can't be changed.", 409)
    return jsonify(views.run_view(db.get_run(run_id)))


@app.post("/api/runs/<run_id>/polish")
def polish_message(run_id):
    run = db.get_run(run_id)
    if not run or not run["message_body"]:
        return error("This submission has no message yet.", 404)
    if run["message_sent_at"]:
        return error("This message has already been sent, so it can't be changed.", 409)
    if not ai_available():
        return error("AI is not switched on.", 503)
    reworded = messages.polish(run["message_body"])
    if not reworded:
        return error("AI could not reword the message. It has been left as it was.", 502)
    db.update_message(run_id, run["message_subject"], reworded)
    return jsonify(views.run_view(db.get_run(run_id)))


@app.post("/api/runs/<run_id>/send")
def send_message(run_id):
    run = db.get_run(run_id)
    if not run or not run["message_body"]:
        return error("This submission has no message yet.", 404)
    if run["message_sent_at"]:
        return error("This message has already been sent.", 409)
    if not mailer.available():
        return error("Email is not set up on this server.", 503)
    if run["sample_id"]:  # Test scenarios use made-up addresses, which could belong to someone.
        return error("Test scenarios aren't emailed. Use Mark as sent.", 400)
    if "@" not in (run["message_to"] or ""):
        return error("This vendor gave no contact email.", 400)
    try:
        mailer.send(run["message_to"], run["message_subject"], run["message_body"])
    except (smtplib.SMTPException, OSError, ValueError):
        log.exception("Could not email the message for %s", run_id)
        return error("The email could not be sent. Please try again, or send it by hand and use Mark as sent.", 502)
    db.mark_sent(run_id)
    return jsonify(views.run_view(db.get_run(run_id)))


@app.post("/api/runs/<run_id>/mark-sent")
def mark_sent(run_id):
    if not db.mark_sent(run_id):
        return error("This submission has no message yet.", 404)
    return jsonify(views.run_view(db.get_run(run_id)))


@app.post("/api/runs/<run_id>/resubmit")
def resubmit(run_id):
    """Starts the correction of a registration and returns the draft to edit."""
    run = db.get_run(run_id)
    if not run:
        return error("No such submission.", 404)
    if not views.can_resubmit(run):
        return error("This registration can't be resubmitted.", 400)
    existing = db.find_draft_for_run(run_id)
    fix = SAMPLES.get(run["sample_id"] or "", {}).get("fix")
    if fix:
        if existing:
            db.delete_draft(existing)
        draft_id = _new_draft_from_sample(fix, parent_id=run_id)
    elif existing:
        db.reopen_draft(existing, run_id)
        draft_id = existing
    else:  # Full details are gone, so start from the masked record. Account number and files must be entered again.
        values = {**forms.blank_values(), **{k: v for k, v in run["form"].items() if k in forms.EDITABLE}, "accountNumber": ""}
        draft_id = db.create_draft(values, parent_id=run_id)
    return jsonify(forms.draft_view(db.get_draft(draft_id))), 201


@app.delete("/api/runs")
def clear_runs():
    db.clear_history()
    return "", 204


@app.get("/api/vendors")
def list_vendors():
    return jsonify([{k: v for k, v in row.items() if k != "acct_hash"} for row in db.list_vendors()])


@app.get("/api/rules")
def rules():
    return jsonify(views.rules_view())


@app.errorhandler(413)
def too_large(_):
    return error("That file is too large. Each file can be up to 5 MB.", 413)


if __name__ == "__main__":
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "5000")), threaded=True)
