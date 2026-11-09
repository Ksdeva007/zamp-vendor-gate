# VendorGate

Takes a vendor submission with its documents, runs the checks in six stages, and returns approved, pending or rejected, with the reasons and a message back to the vendor.

## Run it

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
.venv\Scripts\python app.py
```

Then open http://127.0.0.1:5000. The SQLite database is created at `data\vendor_portal.db`.

## Test it

```powershell
.venv\Scripts\python -m unittest discover -s tests
```

Tests use a throwaway database.

## Optional settings

Everything works without these. Set them as environment variables before starting the app.

| Setting | What it turns on |
|---|---|
| `APP_PASSWORD` | Asks for a password on every page. Any username works. Set this before sharing a public link. |
| `ANTHROPIC_API_KEY` | Claude reads documents the text parser can't, such as scans, and **Polish with AI** appears on messages. The rules still make every decision. Without a key, an unreadable document becomes a request for a clearer copy. |
| `SMTP_HOST`, `SMTP_FROM` | **Send email** appears on messages. Also takes `SMTP_PORT` (default 587), `SMTP_USER` and `SMTP_PASSWORD`. Test scenarios are never emailed. |

## Deploy on Render

`render.yaml` holds the settings. In Render, choose New > Blueprint and connect the GitHub repository containing this code. Add the optional settings above under Environment.

On the free plan, the app sleeps after about 15 minutes without visitors, so the next visit takes about a minute. The SQLite file also resets on every restart or redeploy: runs are cleared and the three starting vendors come back.

## Where things are

| Path | What it does |
|---|---|
| `app.py` | Web API, background runs and the optional password |
| `portal/forms.py` | Which fields and documents apply, what's missing, applying edits |
| `portal/views.py` | Prepares results and rules for display |
| `portal/pipeline.py` | The six stages and all decision rules |
| `portal/messages.py` | The message back to the vendor, and AI rewording |
| `portal/mailer.py` | Sending the message by email |
| `portal/documents.py` | PDF reading and optional AI reading |
| `portal/validators.py` | GST, PAN, IFSC, EIN, ABA, VAT and IBAN checks, and name matching |
| `portal/db.py` | SQLite tables and queries: submissions in progress, uploaded files, runs, checks, vendors, watchlist, dashboard search and totals |
| `portal/config.py` | App and company name, documents required, reference data |
| `portal/samples.py` | Test scenarios with real PDFs |
| `static/` | Web interface, which only displays what the API returns |

To rename the company, change `BUYER` in `portal/config.py`.

## Limits

- The optional password is one shared password, not individual staff accounts. Without it, anyone who can reach the app can see runs and reset the history.
- Checks prove the details are consistent, not that the company exists. There is no live GST, Companies House or IRS lookup, and no penny-drop bank test.
- The vendor master and sanctions list are small mock datasets.
- Run history masks account numbers. A submission that can still be corrected keeps its full details and files in SQLite, unencrypted. These are deleted once it is approved or finally rejected, or after 30 days without changes.
