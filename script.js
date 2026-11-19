/* Product Safety Intelligence - front-end logic */
const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const show = (v) => {
  if (v === null || v === undefined || v === "") return "Not found";
  if (Array.isArray(v)) return v.length ? v.map(show).join("; ") : "None";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
};
const badge = (text) => {
  const t = String(text || "Unknown");
  const k = t.toLowerCase().split(" ")[0];
  return `<span class="badge ${esc(k)}">${esc(t)}</span>`;
};

async function api(url, opts = {}) {
  const r = await fetch(url, opts);
  let data = {};
  try { data = await r.json(); } catch (e) { /* not json */ }
  if (!r.ok) throw new Error(data.error || "Request failed (" + r.status + ")");
  return data;
}
const postJSON = (url, body) =>
  api(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });

let toastTimer;
function toast(msg, bad) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast show" + (bad ? " bad" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.className = "toast"), 3500);
}
function busy(btn, on, label) {
  if (on) { btn.dataset.label = btn.textContent; btn.textContent = label || "Please wait..."; btn.disabled = true; }
  else { btn.textContent = btn.dataset.label || btn.textContent; btn.disabled = false; }
}
const formData = (form) => Object.fromEntries(new FormData(form).entries());
const errBox = (m) => `<div class="error-box">${esc(m)}</div>`;

function table(headers, rows, empty) {
  if (!rows.length) return `<p class="muted">${esc(empty || "Nothing to show yet.")}</p>`;
  return `<table><thead><tr>${headers.map((h) => `<th>${esc(h)}</th>`).join("")}</tr></thead><tbody>${rows
    .map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
}
function sourcesHtml(list) {
  if (!list || !list.length) return `<p class="muted">No sources cited.</p>`;
  return list.map((s) => `<div class="evidence"><b>${esc(s.id)}</b> ${esc(s.filename)}${s.page ? " (page " + esc(s.page) + ")" : ""}
    ${s.note ? "<br><i>" + esc(s.note) + "</i>" : ""}<br>${esc(s.excerpt || s.snippet || "")}</div>`).join("");
}

/* ---------------- Navigation ---------------- */
const loaders = {
  dashboard: loadDashboard, documents: loadDocuments, recalls: loadSources,
  history: loadHistory, settings: loadSettings,
};
document.querySelectorAll(".nav-btn").forEach((btn) =>
  btn.addEventListener("click", () => goto(btn.dataset.page)));
function goto(page) {
  document.querySelectorAll(".nav-btn").forEach((b) => b.classList.toggle("active", b.dataset.page === page));
  document.querySelectorAll(".page").forEach((p) => p.classList.toggle("active", p.id === "page-" + page));
  if (loaders[page]) loaders[page]();
}

/* ---------------- Dashboard ---------------- */
async function loadDashboard() {
  try {
    const d = await api("/api/dashboard");
    const labels = {
      total_investigations: "Total investigations", documents_uploaded: "Documents uploaded",
      products_identified: "Products identified", recall_alerts: "Recall findings",
      high_risk: "High / critical risk", verified: "Verified products",
    };
    $("#stats").innerHTML = Object.keys(labels).map((k) =>
      `<div class="stat"><div class="num">${esc(d.stats[k])}</div><div class="lbl">${labels[k]}</div></div>`).join("");

    const max = Math.max(1, ...Object.values(d.risk_distribution));
    $("#risk-dist").innerHTML = Object.entries(d.risk_distribution).map(([lvl, n]) =>
      `<div class="bar-row"><span class="name">${esc(lvl)}</span><div class="bar-track"><div class="bar-fill ${lvl.toLowerCase()}" style="width:${(n / max) * 100}%"></div></div><span>${n}</span></div>`).join("");

    $("#recent-docs").innerHTML = table(["File", "Status"],
      d.documents.map((x) => [esc(x.filename), badge(x.status)]), "No documents uploaded yet.");

    $("#dash-history").innerHTML = historyTable(d.history, false);
    $("#dash-recalls").innerHTML = table(["Product", "Date", "Reason", "Source"],
      d.recalls.map((r) => [esc(r.product_name), esc(r.recall_date || "n/a"), esc(r.reason || "n/a"), esc(r.source_file || r.source_type || "")]),
      "No recall information found in your documents yet.");
  } catch (e) { toast(e.message, true); }
}

/* ---------------- Documents ---------------- */
async function loadDocuments() {
  try {
    const docs = await api("/api/documents");
    $("#doc-list").innerHTML = table(["File", "Type", "Pages", "Chunks", "Status", "Uploaded", ""],
      docs.map((d) => [esc(d.filename), esc(d.file_type), esc(d.pages || "-"), esc(d.chunk_count),
        badge(d.status) + (d.error ? `<div class="muted">${esc(d.error)}</div>` : ""), esc(d.uploaded_at),
        `<button class="btn small danger" data-del="${d.id}">Delete</button>`]),
      "No documents uploaded yet.");
    document.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", async () => {
      if (!confirm("Delete this document?")) return;
      try { await api("/api/documents/" + b.dataset.del, { method: "DELETE" }); toast("Document deleted."); loadDocuments(); }
      catch (e) { toast(e.message, true); }
    }));
  } catch (e) { toast(e.message, true); }
}
$("#upload-btn").addEventListener("click", async () => {
  const input = $("#file-input");
  if (!input.files.length) return toast("Choose at least one file first.", true);
  const fd = new FormData();
  [...input.files].forEach((f) => fd.append("files", f));
  const btn = $("#upload-btn");
  busy(btn, true, "Uploading...");
  try {
    const r = await api("/api/documents/upload", { method: "POST", body: fd });
    $("#upload-result").innerHTML = table(["File", "Result"],
      r.results.map((x) => [esc(x.filename), badge(x.status) + (x.error ? " " + esc(x.error) : "")]));
    input.value = "";
    loadDocuments();
  } catch (e) { $("#upload-result").innerHTML = errBox(e.message); }
  busy(btn, false);
});

/* ---------------- Investigation ---------------- */
let pollTimer;
$("#inv-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const btn = $("#inv-btn");
  $("#inv-result").innerHTML = "";
  busy(btn, true, "Starting...");
  try {
    const r = await postJSON("/api/investigations", formData(ev.target));
    pollInvestigation(r.id);
  } catch (e) { $("#inv-progress").innerHTML = errBox(e.message); }
  busy(btn, false);
});
function pollInvestigation(id) {
  clearTimeout(pollTimer);
  const tick = async () => {
    try {
      const inv = await api("/api/investigations/" + id);
      if (inv.status === "Running") {
        $("#inv-progress").innerHTML = `<div class="card"><h3>Investigation INV-${String(id).padStart(4, "0")} is running</h3>
          <div class="progress-track"><div class="progress-fill" style="width:${inv.progress || 0}%"></div></div>
          <div class="muted">${esc(inv.current_step || "Working...")} (${inv.progress || 0}%)</div></div>`;
        pollTimer = setTimeout(tick, 2000);
      } else if (inv.status === "Completed") {
        $("#inv-progress").innerHTML = "";
        $("#inv-result").innerHTML = renderInvestigation(inv);
        toast("Investigation completed.");
      } else {
        $("#inv-progress").innerHTML = errBox("Investigation failed: " + (inv.error || "Unknown error"));
      }
    } catch (e) { $("#inv-progress").innerHTML = errBox(e.message); }
  };
  tick();
}
function renderInvestigation(inv) {
  const r = inv.results || {};
  const a = r.analysis || {}, rc = r.recall || {}, v = r.verification || {}, k = r.risk || {}, rep = r.report || {};
  const list = (items) => items && items.length ? `<ul class="plain">${items.map((i) => `<li>${esc(show(i))}</li>`).join("")}</ul>` : "<p class='muted'>None found</p>";
  return `<div class="card">
    <div class="row" style="margin-top:0;justify-content:space-between">
      <h3 style="margin:0">Report INV-${String(inv.id).padStart(4, "0")}: ${esc(inv.product_name)}</h3>
      <a class="btn" href="/api/investigations/${inv.id}/pdf">Download PDF report</a>
    </div>
    <p class="muted">Created ${esc(inv.created_at)} | Risk ${badge(inv.risk_level)} | Verification ${badge(inv.verification_status)}</p>

    <h4>1. Product details</h4>
    ${table(["Field", "You entered", "Found in documents"], [
      ["Product name", esc(inv.product_name), esc(show(a.product_name))],
      ["Brand", esc(inv.brand), esc(show(a.brand))],
      ["Manufacturer", esc(inv.manufacturer), esc(show(a.manufacturer))],
      ["Model number", esc(inv.model_number), esc(show(a.model_number))],
      ["Batch number", esc(inv.batch_number), esc(show(a.batch_number))],
      ["Serial number", esc(inv.serial_number), esc(show(a.serial_number))],
      ["Manufacturing date", "", esc(show(a.manufacturing_date))],
    ])}
    <div class="result-box">${esc(show(rep.product_details))}</div>

    <h4>2. Recall findings</h4>
    <div class="result-box">
      Recall found in documents: <b>${rc.recall_found_in_documents ? "Yes" : "No"}</b><br>
      Information: ${esc(show(rc.recall_information))}<br>Date: ${esc(show(rc.recall_date))}<br>
      Reason: ${esc(show(rc.recall_reason))}<br>Affected models: ${esc(show(rc.affected_models))}<br>
      Affected batches: ${esc(show(rc.affected_batches))}<br>Hazards: ${esc(show(rc.safety_hazards))}
    </div>
    <div class="result-box">${esc(show(rep.recall_findings))}</div>

    <h4>3. Verification</h4>
    <p>Overall status: ${badge(v.overall_status)}</p>
    ${v.fields && v.fields.length ? table(["Field", "Provided", "Found in evidence", "Result"],
      v.fields.map((f) => [esc(f.field), esc(f.provided), esc(f.found_in_evidence), badge(f.status)])) : ""}
    <div class="result-box">${esc(show(v.explanation))}</div>

    <h4>4. Risk assessment</h4>
    <p>Risk level: ${badge(k.risk_level)}</p>
    <b>Hazards</b>${list(k.identified_hazards)}
    <div class="result-box">${esc(show(k.risk_explanation))}<br><br>Consumer impact: ${esc(show(k.potential_consumer_impact))}</div>
    <b>Safety precautions</b>${list(k.safety_precautions)}
    <p class="muted">This risk assessment was produced by an AI model. It is preliminary and is NOT an official regulatory classification.</p>

    <h4>5. Supporting evidence</h4>
    ${sourcesHtml(rep.supporting_evidence)}

    <h4>6. Summary</h4>
    <div class="result-box">${esc(show(rep.investigation_summary))}</div>
    <h4>7. Recommendations</h4>${list(rep.recommendations)}
    <h4>8. Limitations</h4>
    <div class="warn-box">${esc(show(rep.limitations))}<br><br>Always confirm recall status with the official regulator (US CPSC, US FDA, EU Safety Gate) before taking action.</div>
  </div>`;
}

/* ---------------- Recall search ---------------- */
async function loadSources() {
  try {
    const s = await api("/api/sources");
    $("#sources").innerHTML = `<ul class="plain">${s.map((x) =>
      `<li><a href="${esc(x.url)}" target="_blank" rel="noopener">${esc(x.name)}</a></li>`).join("")}</ul>`;
  } catch (e) { toast(e.message, true); }
}
$("#recall-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const btn = $("#recall-btn");
  busy(btn, true, "Searching...");
  try {
    const r = await postJSON("/api/recall-search", formData(ev.target));
    $("#recall-result").innerHTML = `<div class="card">
      <h3>Matches in your uploaded documents</h3>
      ${r.uploaded_document_matches.length ? r.uploaded_document_matches.map((m) =>
        `<div class="evidence"><b>${esc(m.filename)}</b>${m.page ? " (page " + esc(m.page) + ")" : ""} - keyword match ${Math.round(m.coverage * 100)}%<br>${esc(m.snippet)}</div>`).join("")
        : "<p class='muted'>No matching text found in uploaded documents.</p>"}
      <h3 style="margin-top:16px">Saved recall findings</h3>
      ${table(["Product", "Date", "Reason", "Batches"], r.saved_recall_findings.map((x) =>
        [esc(x.product_name), esc(x.recall_date || "n/a"), esc(x.reason || "n/a"), esc(x.affected_batches || "n/a")]), "No saved recall findings match.")}
      <div class="warn-box">${esc(r.limitation)}</div></div>`;
  } catch (e) { $("#recall-result").innerHTML = errBox(e.message); }
  busy(btn, false);
});

/* ---------------- Verification ---------------- */
$("#verify-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const btn = $("#verify-btn");
  busy(btn, true, "Verifying...");
  try {
    const r = await postJSON("/api/verify", formData(ev.target));
    $("#verify-result").innerHTML = `<div class="card">
      <h3>Overall status: ${badge(r.overall_status)}</h3>
      ${r.fields && r.fields.length ? table(["Field", "Provided", "Found in evidence", "Result"],
        r.fields.map((f) => [esc(f.field), esc(f.provided), esc(f.found_in_evidence), badge(f.status)])) : ""}
      <div class="result-box">${esc(show(r.explanation))}</div>
      <h4>Sources</h4>${sourcesHtml(r.sources)}
      <p class="muted">${esc(r.note || "")}</p></div>`;
  } catch (e) { $("#verify-result").innerHTML = errBox(e.message); }
  busy(btn, false);
});

/* ---------------- Ask documents ---------------- */
$("#ask-btn").addEventListener("click", async () => {
  const q = $("#ask-q").value.trim();
  if (!q) return toast("Type a question first.", true);
  const btn = $("#ask-btn");
  busy(btn, true, "Thinking...");
  try {
    const r = await postJSON("/api/ask", { question: q });
    $("#ask-result").innerHTML = `<div class="card">
      <h3>Answer</h3><div class="result-box">${esc(r.answer)}</div>
      <p class="muted">Confidence: ${esc(r.confidence)}${r.retrieval_match !== undefined ? " | Retrieval match: " + esc(r.retrieval_match) + "%" : ""}</p>
      <h4>Sources cited</h4>${sourcesHtml(r.cited)}
      ${r.retrieved && r.retrieved.length ? `<h4>All retrieved passages</h4>${sourcesHtml(r.retrieved)}` : ""}</div>`;
  } catch (e) { $("#ask-result").innerHTML = errBox(e.message); }
  busy(btn, false);
});

/* ---------------- History ---------------- */
function historyTable(items, withView) {
  return table(["ID", "Product", "Status", "Risk", "Verification", "Date"].concat(withView ? [""] : []),
    items.map((i) => [`INV-${String(i.id).padStart(4, "0")}`, esc(i.product_name), badge(i.status), badge(i.risk_level),
      badge(i.verification_status), esc(i.created_at)].concat(withView ? [`<button class="btn small" data-view="${i.id}">View</button>`] : [])),
    "No investigations yet.");
}
async function loadHistory() {
  try {
    const items = await api("/api/investigations");
    $("#history-list").innerHTML = historyTable(items, true);
    document.querySelectorAll("[data-view]").forEach((b) => b.addEventListener("click", async () => {
      try {
        const inv = await api("/api/investigations/" + b.dataset.view);
        $("#history-detail").innerHTML = inv.status === "Completed" ? renderInvestigation(inv)
          : errBox("Status: " + inv.status + (inv.error ? " - " + inv.error : ""));
        $("#history-detail").scrollIntoView({ behavior: "smooth" });
      } catch (e) { toast(e.message, true); }
    }));
  } catch (e) { toast(e.message, true); }
}

/* ---------------- Settings ---------------- */
async function loadSettings() {
  try {
    const s = await api("/api/settings");
    $("#set-key").textContent = s.api_key_configured ? "Configured" : "Not configured (add NEXUS_API_KEY to .env)";
    $("#set-url").textContent = s.base_url;
    $("#set-model").textContent = s.model || "Not selected";
  } catch (e) { toast(e.message, true); }
}
$("#test-btn").addEventListener("click", async () => {
  const btn = $("#test-btn");
  busy(btn, true, "Testing...");
  try {
    const r = await api("/api/settings/test", { method: "POST" });
    if (!r.ok) {
      $("#set-conn").textContent = "Failed: " + r.error;
    } else {
      $("#set-conn").textContent = "Connected (" + r.models.length + " models available)";
      $("#set-model").textContent = r.model || "Not selected";
      $("#model-select").innerHTML = r.models.map((m) =>
        `<option value="${esc(m)}" ${m === r.model ? "selected" : ""}>${esc(m)}</option>`).join("");
      $("#model-picker").classList.remove("hidden");
      if (r.model && !r.model_valid) toast("The saved model is not available. Choose another one.", true);
    }
  } catch (e) { $("#set-conn").textContent = "Failed: " + e.message; }
  busy(btn, false);
});
$("#model-save").addEventListener("click", async () => {
  try {
    const r = await postJSON("/api/settings/model", { model: $("#model-select").value });
    $("#set-model").textContent = r.model;
    toast("Model saved.");
  } catch (e) { toast(e.message, true); }
});

/* ---------------- Start ---------------- */
loadDashboard();
