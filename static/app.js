"use strict";
/* Display only. Form rules, checks, decisions, filtering and totals all come from the server. */
const $ = (s) => document.querySelector(s);
const view = () => $("#view");
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const state = { config: {}, options: [], draft: null, formError: "", saving: Promise.resolve(), run: null, openSections: new Set(), list: { status: "", q: "", offset: 0 }, confirmClear: false, routeToken: 0, busy: false };
const BADGES = { approved: "ok", pending: "warn", rejected: "block" };
const when = (iso) => new Date(iso).toLocaleString("en-IN", { day: "numeric", month: "short", year: "numeric", hour: "numeric", minute: "2-digit" });
const pageHead = (title, sub = "", right = "") => `<div class="page-head"><div><h1>${title}</h1>${sub ? `<p>${sub}</p>` : ""}</div>${right}</div>`;

async function api(path, options = {}) {
  const res = await fetch(path, options);
  if (!res.ok) {
    let message = `Something went wrong (${res.status}). Please try again.`;
    try { message = (await res.json()).error || message; } catch (e) { /* keep the default */ }
    const err = new Error(message);
    err.status = res.status;
    throw err;
  }
  return res.status === 204 ? null : res.json();
}
const sendJson = (path, method, body) => api(path, { method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(body ?? {}) });
function toast(text) {
  const t = $("#toast");
  t.textContent = text;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, 2200);
}
const showError = (message) => { view().innerHTML = `<div class="alert" role="alert">${esc(message)}</div>`; };
const go = (hash) => { if (location.hash === hash) route(); else location.hash = hash; };

/* ---------- Routes ---------- */
const NAV = [["#/new", "New run"], ["#/dashboard", "Dashboard"], ["#/vendors", "Vendor master"], ["#/rules", "Rules"]];
function renderNav(active) {
  $("#nav").innerHTML = NAV.map(([href, label]) => `<a class="${active === href ? "on" : ""}"${active === href ? ' aria-current="page"' : ""} href="${href}">${label}</a>`).join("");
}
async function route() {
  const token = ++state.routeToken;
  const [, page, id] = (location.hash || "#/new").split("/");
  renderNav(page === "runs" ? "#/new" : `#/${page}`);
  window.scrollTo(0, 0);
  try {
    if (page === "new" && id) await showDraft(id);
    else if (page === "new") await startDraft(state.options[0] ? state.options[0].id : null);
    else if (page === "runs" && id) await showRun(id, token);
    else if (page === "dashboard") await showDashboard();
    else if (page === "vendors") await showVendors();
    else if (page === "rules") await showRules();
    else location.replace("#/new");
  } catch (e) {
    showError(e.message);
  }
}

/* ---------- New run: the submission form and an empty live panel ---------- */
async function startDraft(sampleId) {
  const d = await sendJson("/api/drafts", "POST", sampleId ? { sample_id: sampleId } : {});
  state.draft = null;
  location.replace(`#/new/${d.id}`);
}
async function showDraft(id) {
  const token = state.routeToken;
  const draft = await api(`/api/drafts/${id}`);
  if (token !== state.routeToken) return;
  if (draft.submitted_run_id) { location.replace(`#/runs/${draft.submitted_run_id}`); return; }
  state.draft = draft;
  state.formError = "";
  renderDraft();
}
/* How the main form is laid out on screen. Which fields exist still comes from the server. */
const FORM_GROUPS = [["Company", ["legalName", "country", "website", "contactEmail"]], ["Tax registration", ["taxId", "certExpiry", "taxCertName"]], ["Bank account", ["bankHolder", "bankCode", "accountNumber"]]];
const WIDE_FIELDS = new Set(["legalName", "country", "website", "contactEmail", "taxCertName", "bankHolder", "addr1", "addr2", "serviceDesc"]);
function fieldHtml(f) {
  const id = `f-${f.key}`, req = f.required ? " <i>*</i>" : "", hint = f.hint ? `<span class="hint">${esc(f.hint)}</span>` : "", label = `<label for="${id}">${esc(f.label)}${req}</label>`;
  let control;
  if (f.type === "select") control = `<select id="${id}" data-field="${f.key}">${f.options.map((o) => `<option value="${esc(o.value)}"${o.value === f.value ? " selected" : ""}>${esc(o.label)}</option>`).join("")}</select>`;
  else if (f.type === "textarea") control = `<textarea id="${id}" data-field="${f.key}">${esc(f.value)}</textarea>`;
  else {
    const list = f.suggestions ? `<datalist id="${id}-list">${f.suggestions.map((s) => `<option value="${esc(s)}">`).join("")}</datalist>` : "";
    const type = f.type === "email" || f.type === "date" ? f.type : "text";
    control = `<input id="${id}" data-field="${f.key}" type="${type}" class="${f.mono ? "mono" : ""}" value="${esc(f.value)}" autocomplete="off"${f.suggestions ? ` list="${id}-list"` : ""}>${list}`;
  }
  return `<div class="f${WIDE_FIELDS.has(f.key) ? " wide" : ""}">${label}${control}${hint}</div>`;
}
function mainFieldsHtml(fields) {
  const byKey = Object.fromEntries(fields.map((f) => [f.key, f]));
  const placed = new Set(FORM_GROUPS.flatMap(([, keys]) => keys));
  const groups = FORM_GROUPS.map(([title, keys]) => [title, keys.filter((k) => byKey[k]).map((k) => byKey[k])]);
  groups.push(["Other details", fields.filter((f) => !placed.has(f.key))]);
  return groups.filter(([, list]) => list.length).map(([title, list]) => `<fieldset><legend>${title}</legend><div class="fields">${list.map(fieldHtml).join("")}</div></fieldset>`).join("");
}
function docHtml(x, draftId) {
  const size = x.file ? ` · ${Math.max(1, Math.round(x.file.size / 1024))} KB` : "";
  return `<div class="doc"><div class="doc-name">${esc(x.label)}${x.required ? " <i>*</i>" : ""}<span class="${x.file ? "has" : ""}">${x.file ? esc(x.file.name) + size : "Not attached"}</span></div>
    <div class="doc-acts">${x.file ? `<a class="lnk" href="/api/drafts/${esc(draftId)}/documents/${x.slot}">Download</a><button type="button" class="lnk" data-act="doc-remove" data-slot="${x.slot}">Remove</button>` : ""}
    <label class="lnk" for="up-${x.slot}" tabindex="0">${x.file ? "Replace" : "Attach"}</label><input id="up-${x.slot}" data-upload="${x.slot}" type="file" accept=".pdf,.jpg,.jpeg,.png" hidden></div></div>`;
}
function renderDraft() {
  const d = state.draft, [main, ...extra] = d.sections;
  const required = d.documents.filter((x) => x.required), optional = d.documents.filter((x) => !x.required);
  const open = (name) => (state.openSections.has(name) ? " open" : "");
  const known = state.options.some((o) => o.id === d.sample_id);
  const sub = d.parent_id ? `Correction of <a href="#/runs/${esc(d.parent_id)}">${esc(d.parent_id)}</a>` : "Fill in the vendor's details, attach their documents, then run the check.";
  view().innerHTML = `${pageHead("New vendor check", sub)}<div class="g">
    <aside class="side"><h2>Live run</h2><p class="note">Each stage fills in here as it runs.</p>
      <ol class="stages">${state.config.stages.map((title, idx) => stageHtml({ idx, title, state: "waiting", checks: [] })).join("")}</ol></aside>
    <form id="draft-form" novalidate>
      <div class="scenario"><label for="pre">Test scenario</label>
        <select id="pre"><option value="">Blank form, or your own entries</option>${state.options.map((o) => `<option value="${esc(o.id)}"${known && o.id === d.sample_id ? " selected" : ""}>${esc(o.label)}</option>`).join("")}</select></div>
      ${mainFieldsHtml(main.fields)}
      <fieldset><legend>Documents</legend><div class="docs">${required.map((x) => docHtml(x, d.id)).join("")}</div>
        <p class="note">PDF, JPG or PNG, up to 5 MB each. PDFs are read and compared with the form.</p></fieldset>
      <div class="more">
      <details data-section="Other documents"${open("Other documents")}><summary>Other documents</summary><div class="docs">${optional.map((x) => docHtml(x, d.id)).join("")}</div></details>
      ${extra.map((s) => `<details data-section="${esc(s.title)}"${open(s.title)}><summary>${esc(s.title)}</summary><div class="fields">${s.fields.map(fieldHtml).join("")}</div></details>`).join("")}
      </div>
      ${state.formError ? `<div class="alert" role="alert">${esc(state.formError)}</div>` : ""}
      ${d.missing.length ? `<div class="alert warn">${d.missing.length} required item${d.missing.length > 1 ? "s are" : " is"} missing: ${esc(d.missing.join(", "))}. You can still run the check, and the message to the vendor will ask for them.</div>` : ""}
      <div class="actions"><button type="submit" class="p"${state.busy ? " disabled" : ""}>Run onboarding check</button></div>
    </form>
  </div>`;
}
const fieldShape = (d) => JSON.stringify([d.sections.map((s) => s.fields.map((f) => [f.key, f.label, f.required])), d.documents.map((x) => [x.slot, x.required])]);
function saveEdits(edits) {
  const draftId = state.draft.id;
  state.saving = state.saving.then(async () => {
    try {
      const updated = await sendJson(`/api/drafts/${draftId}`, "PATCH", edits);
      if (!state.draft || state.draft.id !== draftId) return;
      const reshaped = fieldShape(updated) !== fieldShape(state.draft) || updated.missing.length !== state.draft.missing.length || updated.sample_id !== state.draft.sample_id;
      state.draft = updated;
      state.formError = "";
      if (reshaped) {
        const focused = document.activeElement && document.activeElement.id;
        renderDraft();
        if (focused && document.getElementById(focused)) document.getElementById(focused).focus();
      }
    } catch (e) { state.formError = e.message; renderDraft(); }
  });
  return state.saving;
}
async function submitDraft() {
  await state.saving;
  state.busy = true;
  try {
    const { id } = await sendJson(`/api/drafts/${state.draft.id}/submit`, "POST", { pace: true });
    state.draft = null;
    go(`#/runs/${id}`);
  } catch (e) { state.formError = e.message; renderDraft(); }
  state.busy = false;
}
async function changeScenario(sampleId) {
  await state.saving;
  const old = state.draft;
  if (old && !old.parent_id) await api(`/api/drafts/${old.id}`, { method: "DELETE" }).catch(() => {});
  await startDraft(sampleId);
}

/* ---------- Run: what was submitted, the live stages, the decision and the message ---------- */
async function showRun(id, token) {
  for (;;) {
    let run;
    try { run = await api(`/api/runs/${id}`); }
    catch (e) { if (e.status === 404) throw e; await new Promise((r) => setTimeout(r, 1000)); if (token !== state.routeToken) return; continue; }
    if (token !== state.routeToken) return;
    state.run = run;
    renderRun();
    if (run.state !== "running") return;
    await new Promise((r) => setTimeout(r, 250));
    if (token !== state.routeToken) return;
  }
}
function stageHtml(s) {
  const passed = s.checks.filter((c) => c.tone !== "warn" && c.tone !== "block").length;
  const tally = s.state === "done" ? `${passed} of ${s.checks.length} passed` : s.state === "active" ? "Checking" : "";
  return `<li class="stage ${s.state === "waiting" ? "wait" : s.tone}"><span class="num">${s.idx + 1}</span><div><h3>${esc(s.title)}${tally ? `<small>${tally}</small>` : ""}</h3>
    ${s.checks.map((c) => `<div class="ch ${c.tone}"><b>${c.symbol}</b><span class="t">${esc(c.title)}</span><span class="v">${esc(c.detail)}</span></div>`).join("")}</div></li>`;
}
function resultHtml(run) {
  if (run.state === "error") return '<div class="alert" role="alert">Processing stopped on an unexpected error, so no decision was made. Please submit the registration again.</div>';
  if (run.state === "running") return "";
  const issues = run.outcome.issues, notes = run.outcome.notes;
  const sentLine = run.message_sent_at ? `Sent ${esc(when(run.message_sent_at))}` : "Draft";
  const editable = !run.message_sent_at;
  return `<div class="dec ${run.decision.tone}"><div class="dec-head"><h2>${esc(run.decision.title)}</h2><span>${run.counts.passed} of ${run.counts.total} checks passed</span></div>
      ${issues.length ? "" : `<p>${esc(run.headline)}</p>`}
      ${issues.length ? `<ul>${issues.map((c) => `<li><b>${esc(c.title)}.</b> ${esc(c.detail)}</li>`).join("")}</ul>` : ""}
      ${notes.length ? `<ul>${notes.map((c) => `<li>Note: ${esc(c.title)}. ${esc(c.detail)}</li>`).join("")}</ul>` : ""}
      <div class="facts"><span><b>Next owner:</b> ${esc(run.owner)}</span>${run.vendor_id ? `<span><b>Vendor ID:</b> ${esc(run.vendor_id)}${run.added_to_master ? "" : " (test scenario, not added)"}</span>` : ""}${run.ai_used.length ? `<span><b>AI used:</b> ${esc(run.ai_used.join(", "))}</span>` : ""}</div>
    </div>
    <div class="block"><div class="head"><h2>Message back to vendor</h2><small>To ${esc(run.message_to)} · ${sentLine}</small></div>
    <textarea id="message" rows="${Math.min(32, run.message_text.split("\n").length + 2)}"${editable ? "" : " readonly"} aria-label="Message back to vendor">${esc(run.message_text)}</textarea>
    ${run.reasons_withheld ? '<p class="note">No reasons are listed on purpose. The vendor is not told what was checked or which record matched.</p>' : ""}
    <div class="row msg-acts"><button class="s" data-act="copy">Copy</button>
      ${editable && state.config.ai ? '<button class="s" data-act="polish">Polish with AI</button>' : ""}
      ${editable && state.config.email && !run.sample_id ? '<button class="s" data-act="send">Send email</button>' : ""}
      ${editable ? '<button class="s" data-act="mark-sent">Mark as sent</button>' : ""}
      <a class="s" href="/api/runs/${esc(run.id)}/audit">Download audit record</a></div>
    ${run.next_action ? `<div class="next"><button class="p" data-act="resubmit">${esc(run.next_action.label)}</button></div>` : ""}</div>`;
}
function renderRun() {
  const run = state.run, running = run.state === "running";
  const sub = [`<span class="mono">${esc(run.id)}</span>`, esc(when(run.created_at)), run.sample_id ? "Test scenario" : "", run.parent_id ? `Correction of <a href="#/runs/${esc(run.parent_id)}">${esc(run.parent_id)}</a>` : ""].filter(Boolean).join(" · ");
  const step = Math.min(run.progress.done + 1, run.progress.total);
  view().innerHTML = `${pageHead(esc(run.vendor), sub, '<a class="s" href="#/new">Start a new run</a>')}<div class="g run">
    <section><h2>Submitted details</h2>
      <dl class="kv">${run.submission.map((r) => `<dt>${esc(r.label)}</dt><dd>${esc(r.value)}</dd>`).join("")}</dl>
    </section>
    <section>${resultHtml(run)}
      <div class="head"><h2>${running ? "Live run" : "Stage by stage"}</h2>${running ? `<small>Stage ${step} of ${run.progress.total}</small>` : ""}</div>
      ${running ? `<div class="bar"><i style="width:${Math.round((run.progress.done / run.progress.total) * 100)}%"></i></div>` : ""}
      <ol class="stages">${run.stages.map(stageHtml).join("")}</ol></section></div>`;
}
function saveMessage() {
  const box = $("#message"), run = state.run;
  if (!box || box.readOnly || box.value === run.message_text) return state.saving;
  const text = box.value;
  state.saving = state.saving.then(async () => {
    try { state.run = await sendJson(`/api/runs/${run.id}/message`, "PATCH", { text }); }
    catch (e) { toast(e.message); }
  });
  return state.saving;
}

/* ---------- Dashboard ---------- */
async function showDashboard() {
  const { status, q, offset } = state.list;
  const params = new URLSearchParams({ offset });
  if (status) params.set("status", status);
  if (q) params.set("q", q);
  const token = state.routeToken, seq = (state.listSeq = (state.listSeq || 0) + 1);
  const data = await api(`/api/runs?${params}`);
  if (token !== state.routeToken || seq !== state.listSeq) return; /* A newer search or page has taken over. */
  const fig = (value, label, n) => `<button class="fig${status === value ? " on" : ""}" aria-pressed="${status === value}" data-act="filter" data-v="${value}"><b>${n}</b><span>${label}</span></button>`;
  const from = data.total ? data.offset + 1 : 0, to = data.offset + data.items.length;
  const focused = document.activeElement && document.activeElement.id === "search" ? document.activeElement.selectionStart : null;
  const reset = state.confirmClear
    ? '<span class="row"><span class="muted">Delete all runs and the vendors they added?</span><button class="s danger" data-act="clear-yes">Delete</button><button class="s" data-act="clear-no">Keep</button></span>'
    : '<button class="s" data-act="clear">Reset history</button>';
  view().innerHTML = `${pageHead("Dashboard", "Every vendor check so far. Click a figure to filter the list.", data.stats.all ? reset : "")}
    <div class="figs">${fig("", "Total runs", data.stats.all)}${fig("approved", "Approved", data.stats.approved)}${fig("pending", "Pending", data.stats.pending)}${fig("rejected", "Rejected", data.stats.rejected)}</div>
    <section><div class="head"><h2>Past runs</h2><input id="search" type="search" placeholder="Search vendor or reference" aria-label="Search" value="${esc(q)}"></div>
      ${data.items.length ? `<div class="tw"><table><thead><tr><th>Date</th><th>Reference</th><th>Vendor</th><th>Country</th><th>Status</th><th>Issues</th></tr></thead><tbody>
        ${data.items.map((r) => `<tr class="r" tabindex="0" data-act="open-run" data-id="${esc(r.id)}"><td class="nowrap">${esc(when(r.created_at))}</td><td class="mono nowrap">${esc(r.id)}</td><td>${esc(r.vendor)}<small>${esc([r.main_reason, r.parent_id && "Correction", r.sample_id && "Test"].filter(Boolean).join(" · "))}</small></td><td>${esc(r.country)}</td><td><span class="tag b-${BADGES[r.status] || "none"}">${esc(r.status || r.state)}</span></td><td>${r.issue_count}</td></tr>`).join("")}
        </tbody></table></div>
        <div class="pager"><span>${from}–${to} of ${data.total}</span><div class="row"><button class="s" data-act="page" data-o="${Math.max(0, data.offset - data.limit)}"${data.offset === 0 ? " disabled" : ""}>Previous</button><button class="s" data-act="page" data-o="${data.offset + data.limit}"${to >= data.total ? " disabled" : ""}>Next</button></div></div>`
      : `<p class="note">${data.stats.all ? "No matching runs. Try a different filter or search." : 'No runs yet. <a href="#/new">Run a vendor check</a> to see it here.'}</p>`}
    </section>`;
  if (focused !== null) { const s = $("#search"); s.focus(); s.setSelectionRange(focused, focused); }
  if (data.items.some((r) => r.state === "running")) { /* Keep the list current until those runs finish. */
    setTimeout(() => { if (token === state.routeToken && seq === state.listSeq) showDashboard().catch(() => {}); }, 1000);
  }
}

/* ---------- Vendor master and rules ---------- */
async function showVendors() {
  const token = state.routeToken;
  const vendors = await api("/api/vendors");
  if (token !== state.routeToken) return;
  view().innerHTML = `${pageHead("Vendor master", `New submissions are checked against these ${vendors.length} vendors.`)}
    <div class="tw"><table><thead><tr><th>Vendor ID</th><th>Vendor</th><th>Tax ID</th><th>Account</th><th>Contact on file</th><th>Since</th></tr></thead><tbody>
    ${vendors.map((v) => `<tr><td class="mono">${esc(v.vendor_id)}</td><td>${esc(v.legal_name)}</td><td class="mono">${esc(v.tax_id || v.pan)}</td><td class="mono">${esc(v.acct_last4)}</td><td>${esc(v.contact_name)}<small>${esc(v.contact_email)}</small></td><td class="nowrap">${esc(v.since)}</td></tr>`).join("")}
    </tbody></table></div>`;
}
async function showRules() {
  const token = state.routeToken;
  const r = await api("/api/rules");
  if (token !== state.routeToken) return;
  view().innerHTML = `${pageHead("Rules", "Every registration ends as approved, pending or rejected.")}
    <div class="outcomes">${r.statuses.map((s) => `<section class="outcome ${esc(s.tone)}">
      <h2>${esc(s.title)}</h2>
      <p>${esc(s.when)}</p>
      <ul>${s.examples.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>
      <p class="to"><b>Vendor receives:</b> ${esc(s.message)}</p>
    </section>`).join("")}</div>
    <h2 class="sec">What we check</h2>
    <div class="cols checks">${r.checks.map((g) => `<section><h3>${esc(g.title)}</h3><ul>${g.items.map((i) => `<li>${esc(i)}</li>`).join("")}</ul></section>`).join("")}</div>
    <h2 class="sec">Good to know</h2>
    <div class="cols">${r.notes.map((n) => `<section><h3>${esc(n.title)}</h3><p>${esc(n.text)}</p></section>`).join("")}</div>`;
}

/* ---------- Events ---------- */
document.addEventListener("click", async (ev) => {
  const t = ev.target.closest("[data-act]");
  if (!t || t.disabled) return;
  const a = t.dataset.act;
  try {
    if (a === "doc-remove") { state.draft = await api(`/api/drafts/${state.draft.id}/documents/${t.dataset.slot}`, { method: "DELETE" }); renderDraft(); }
    else if (a === "copy") {
      const box = $("#message");
      try { await navigator.clipboard.writeText(box.value); toast("Message copied"); }
      catch (e) { box.select(); toast("Text selected. Press Ctrl+C to copy."); }
    }
    else if (a === "polish") {
      await saveMessage();
      t.disabled = true; t.textContent = "Writing…";
      state.run = await api(`/api/runs/${state.run.id}/polish`, { method: "POST" });
      renderRun();
    }
    else if (a === "send") {
      await saveMessage();
      t.disabled = true; t.textContent = "Sending…";
      try { state.run = await api(`/api/runs/${state.run.id}/send`, { method: "POST" }); toast("Email sent"); }
      finally { renderRun(); }
    }
    else if (a === "mark-sent") { await saveMessage(); state.run = await api(`/api/runs/${state.run.id}/mark-sent`, { method: "POST" }); renderRun(); toast("Marked as sent"); }
    else if (a === "resubmit") { await saveMessage(); const d = await api(`/api/runs/${state.run.id}/resubmit`, { method: "POST" }); state.draft = null; go(`#/new/${d.id}`); }
    else if (a === "open-run") { go(`#/runs/${t.dataset.id}`); }
    else if (a === "filter") { state.list.status = t.dataset.v; state.list.offset = 0; await showDashboard(); }
    else if (a === "page") { state.list.offset = +t.dataset.o; await showDashboard(); }
    else if (a === "clear") { state.confirmClear = true; await showDashboard(); }
    else if (a === "clear-no") { state.confirmClear = false; await showDashboard(); }
    else if (a === "clear-yes") { state.confirmClear = false; await api("/api/runs", { method: "DELETE" }); state.list = { status: "", q: "", offset: 0 }; await showDashboard(); toast("History reset"); }
  } catch (e) {
    if (state.draft && $("#draft-form")) { state.formError = e.message; renderDraft(); } else { toast(e.message); if ($("#message")) renderRun(); }
  }
});
document.addEventListener("keydown", (ev) => {
  if ((ev.key === "Enter" || ev.key === " ") && ev.target.matches("tr[data-act], label.lnk")) { ev.preventDefault(); ev.target.click(); }
});
document.addEventListener("submit", (ev) => {
  if (ev.target.id === "draft-form") { ev.preventDefault(); submitDraft(); }
});
document.addEventListener("input", (ev) => {
  if (ev.target.id !== "search") return;
  state.list.q = ev.target.value;
  state.list.offset = 0;
  clearTimeout(state.searchTimer);
  state.searchTimer = setTimeout(() => showDashboard().catch((e) => toast(e.message)), 200);
});
document.addEventListener("toggle", (ev) => {
  const name = ev.target.dataset && ev.target.dataset.section;
  if (!name) return;
  if (ev.target.open) state.openSections.add(name); else state.openSections.delete(name);
}, true);
document.addEventListener("change", async (ev) => {
  const el = ev.target, d = el.dataset || {};
  if (el.id === "pre") { changeScenario(el.value || null).catch((e) => toast(e.message)); }
  else if (el.id === "message") { saveMessage(); }
  else if (d.field) saveEdits({ [d.field]: el.value });
  else if (d.upload && el.files[0]) {
    const body = new FormData();
    body.append("file", el.files[0]);
    try { state.draft = await api(`/api/drafts/${state.draft.id}/documents/${d.upload}`, { method: "POST", body }); state.formError = ""; }
    catch (e) { state.formError = e.message; }
    renderDraft();
  }
});
window.addEventListener("hashchange", route);

/* ---------- Boot ---------- */
(async () => {
  try {
    [state.config, state.options] = await Promise.all([api("/api/config"), api("/api/start-options")]);
    $("#tagline").textContent = `${state.config.buyer} · ${state.config.tagline}`;
    document.title = state.config.name;
    await route();
  } catch (e) { showError(`Could not reach the server. ${e.message}`); }
})();
