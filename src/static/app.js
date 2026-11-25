const state = {
  leads: [],
  queue: [],
  analytics: null,
};

const STATUSES = ["new", "enriched", "contacted", "replied", "booked", "won", "lost", "unsubscribed", "bounced"];

function esc(value) {
  const div = document.createElement("div");
  div.textContent = value ?? "";
  return div.innerHTML;
}

function showToast(msg) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.classList.remove("hidden");
  setTimeout(() => el.classList.add("hidden"), 3200);
}

async function api(url, opts = {}) {
  const res = await fetch(url, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

async function loadAnalytics() {
  const data = await api("/api/analytics");
  state.analytics = data;
  document.getElementById("kpi-total").textContent = data.total_leads;
  document.getElementById("kpi-avg-score").textContent = `Avg score: ${data.average_score}`;
  document.getElementById("kpi-grade-a").textContent = data.by_grade?.A || 0;
  document.getElementById("kpi-grades-meta").textContent =
    `B: ${data.by_grade?.B || 0} · C: ${data.by_grade?.C || 0} · D: ${data.by_grade?.D || 0}`;
  document.getElementById("kpi-email-cov").textContent = `${data.email_coverage_pct}%`;
  document.getElementById("kpi-email-count").textContent = `${data.with_email} reachable emails`;
  document.getElementById("kpi-due").textContent = data.due_today_count || 0;
  document.getElementById("kpi-reply-rate").textContent = `${data.reply_rate_pct}%`;
  document.getElementById("kpi-reply-meta").textContent =
    `${data.positive_replies} positive / ${data.contacted_count} contacted`;
  renderRuns(data.recent_runs || []);
}

async function loadLeads() {
  const params = new URLSearchParams();
  const search = document.getElementById("filter-search").value.trim();
  const status = document.getElementById("filter-status").value;
  const grade = document.getElementById("filter-grade").value;
  if (search) params.set("search", search);
  if (status) params.set("status", status);
  if (grade) params.set("grade", grade);
  const data = await api(`/api/leads?${params.toString()}`);
  state.leads = data.leads || [];
  renderLeads();
}

async function loadQueue() {
  const includeInitial = document.getElementById("queue-include-initial").checked;
  const data = await api(`/api/queue?include_initial=${includeInitial}`);
  state.queue = data.queue || [];
  renderQueue();
}

function renderLeads() {
  const tbody = document.getElementById("leads-tbody");
  if (!state.leads.length) {
    tbody.innerHTML = `<tr><td colspan="8" class="muted">No leads found. Click "Run Pipeline" to discover leads.</td></tr>`;
    return;
  }
  tbody.innerHTML = state.leads.map((lead) => {
    const statusOpts = STATUSES.map(
      (s) => `<option value="${s}" ${s === lead.status ? "selected" : ""}>${s}</option>`
    ).join("");
    return `
      <tr>
        <td>
          <strong>${esc(lead.business_name)}</strong>
          <div class="muted">${esc(lead.owner_name || lead.niche || lead.source || "")}</div>
        </td>
        <td>${esc(lead.city || "—")}</td>
        <td><span class="badge grade-${esc(lead.grade)}">${esc(lead.grade)} · ${lead.score}</span></td>
        <td>
          <div>${esc(lead.email || "—")}</div>
          <div class="muted">${esc(lead.phone || "")}</div>
        </td>
        <td>
          <select class="status-select" data-lead-status="${lead.id}">${statusOpts}</select>
        </td>
        <td>${esc(lead.outreach_date || "Not started")}</td>
        <td class="muted">${esc(lead.outreach_angle || "")}</td>
        <td>
          <button class="btn btn-sm btn-ghost" data-inspect="${lead.id}">Inspect</button>
          <button class="btn btn-sm btn-secondary" data-preview="${lead.id}" data-stage="initial">Draft</button>
        </td>
      </tr>
    `;
  }).join("");
}

function renderQueue() {
  const tbody = document.getElementById("queue-tbody");
  if (!state.queue.length) {
    tbody.innerHTML = `<tr><td colspan="7" class="muted">No follow-ups due for the selected view.</td></tr>`;
    return;
  }
  tbody.innerHTML = state.queue.map((item) => `
    <tr>
      <td><span class="badge grade-${esc(item["Lead Grade"] || "C")}">${esc(item["Follow-Up Stage"])}</span></td>
      <td>${esc(item["Due Date"])}${Number(item["Overdue Days"]) > 0 ? ` <span class="muted">(${esc(item["Overdue Days"])}d overdue)</span>` : ""}</td>
      <td><strong>${esc(item["Business Name"])}</strong><div class="muted">${esc(item["City"])}</div></td>
      <td>${esc(item["Lead Grade"])} · ${esc(item["Lead Score"])}</td>
      <td>${esc(item["Email"] || "—")}</td>
      <td class="muted">${esc(item["Outreach Angle"] || "")}</td>
      <td>
        <button class="btn btn-sm btn-secondary" data-preview="${esc(item["Lead ID"])}" data-stage="${esc(item["Follow-Up Stage"])}">Preview</button>
        <button class="btn btn-sm btn-primary" data-mark-sent="${esc(item["Lead ID"])}" data-stage="${esc(item["Follow-Up Stage"])}">Mark Sent</button>
      </td>
    </tr>
  `).join("");
}

function renderRuns(runs) {
  const tbody = document.getElementById("runs-tbody");
  if (!runs.length) {
    tbody.innerHTML = `<tr><td colspan="10" class="muted">No pipeline executions logged yet.</td></tr>`;
    return;
  }
  tbody.innerHTML = runs.map((r) => `
    <tr>
      <td><code>${esc(r.run_id)}</code></td>
      <td>${esc(r.mode)}</td>
      <td>${esc(r.city)} · ${esc(r.niche)}</td>
      <td>${r.fetched_count}</td>
      <td>${r.added_count}</td>
      <td>${r.skipped_count}</td>
      <td>${r.queued_count}</td>
      <td>${r.drafted_count}</td>
      <td>${r.duration_ms} ms</td>
      <td><span class="badge ${r.status === "success" ? "grade-A" : "grade-C"}">${esc(r.status)}</span></td>
    </tr>
  `).join("");
}

async function openLeadDrawer(leadId) {
  const lead = await api(`/api/leads/${leadId}`);
  document.getElementById("drawer-title").textContent = lead.business_name;
  document.getElementById("drawer-subtitle").textContent =
    `${lead.city || "Unknown city"} · Grade ${lead.grade} (${lead.score}/100) · ${lead.status}`;

  const sig = lead.enrichment || {};
  const eventsHtml = (lead.events || []).length
    ? lead.events.map((e) => `<div class="muted">${esc(e.created_at)} · <strong>${esc(e.stage)}</strong> (${esc(e.status)}) -> ${esc(e.recipient || "")}</div>`).join("")
    : `<div class="muted">No outreach events logged yet.</div>`;

  const body = document.getElementById("drawer-body");
  body.innerHTML = `
    <div class="detail-card">
      <h3>Contact &amp; Follow-Up Schedule</h3>
      <div><strong>Owner:</strong> ${esc(lead.owner_name || "—")}</div>
      <div><strong>Email:</strong> ${esc(lead.email || "—")}</div>
      <div><strong>Phone:</strong> ${esc(lead.phone || "—")}</div>
      <div><strong>Website:</strong> ${lead.website ? `<a href="${esc(lead.website)}" target="_blank" rel="noopener">${esc(lead.website)}</a>` : "—"}</div>
      <div><strong>Initial Outreach:</strong> ${esc(lead.outreach_date || "Not scheduled")}</div>
      <div class="muted" style="margin-top:6px;">F #1: ${esc(lead.followups["F #1"] || "—")} · F #2: ${esc(lead.followups["F #2"] || "—")} · F #3: ${esc(lead.followups["F #3"] || "—")}</div>
      <div class="muted">F #4: ${esc(lead.followups["F #4"] || "—")} · F #5: ${esc(lead.followups["F #5"] || "—")} · F #6: ${esc(lead.followups["F #6"] || "—")}</div>
    </div>
    <div class="detail-card">
      <h3>Website Enrichment Signals</h3>
      <div><strong>Reachable:</strong> ${sig.reachable ? "Yes" : "No"} · <strong>HTTPS:</strong> ${sig.uses_https ? "Yes" : "No"}</div>
      <div><strong>CMS:</strong> ${esc(sig.cms_detected || "Unknown")} · <strong>Booking Widget:</strong> ${sig.has_booking_widget ? "Detected" : "Missing"} · <strong>Live Chat:</strong> ${sig.has_live_chat ? "Detected" : "Missing"}</div>
      <div><strong>Recommended Angle:</strong> ${esc(lead.outreach_angle || "—")}</div>
    </div>
    <div class="detail-card">
      <h3>Quick Edit Notes</h3>
      <textarea id="drawer-notes" rows="3" style="width:100%;background:#0b1120;color:#f1f5f9;border:1px solid #283548;border-radius:6px;padding:8px;">${esc(lead.notes || "")}</textarea>
      <div style="margin-top:8px;display:flex;gap:8px;">
        <button class="btn btn-sm btn-primary" id="save-notes-btn">Save Notes</button>
        <button class="btn btn-sm btn-secondary" id="drawer-mark-sent-btn">Mark Initial Sent Today</button>
      </div>
    </div>
    <div class="detail-card">
      <h3>Outreach Timeline</h3>
      ${eventsHtml}
    </div>
  `;

  document.getElementById("save-notes-btn").onclick = async () => {
    const notes = document.getElementById("drawer-notes").value;
    await api(`/api/leads/${lead.id}`, { method: "PATCH", body: JSON.stringify({ notes }) });
    showToast("Notes updated");
    await refreshAll();
  };
  document.getElementById("drawer-mark-sent-btn").onclick = async () => {
    await api(`/api/leads/${lead.id}/mark-sent`, { method: "POST", body: JSON.stringify({ stage: "initial" }) });
    showToast("Logged initial outreach & computed F #1-F #6");
    await openLeadDrawer(lead.id);
    await refreshAll();
  };

  document.getElementById("lead-drawer").classList.remove("hidden");
}

async function openEmailPreview(leadId, stage) {
  const rendered = await api(`/api/leads/${leadId}/preview-email?stage=${encodeURIComponent(stage)}`);
  document.getElementById("email-modal-title").textContent = `Draft Preview (${stage})`;
  document.getElementById("preview-to").textContent = rendered.to || "[No email on file]";
  document.getElementById("preview-subject").textContent = rendered.subject;
  document.getElementById("preview-body").textContent = rendered.body;
  document.getElementById("email-modal").classList.remove("hidden");
}

async function refreshAll() {
  await Promise.all([loadAnalytics(), loadLeads(), loadQueue()]);
}

function bindEvents() {
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((t) => t.classList.remove("active"));
      document.querySelectorAll(".panel").forEach((p) => p.classList.remove("active"));
      tab.classList.add("active");
      document.getElementById(`tab-${tab.dataset.tab}`).classList.add("active");
    });
  });

  document.getElementById("filter-search").addEventListener("input", () => loadLeads());
  document.getElementById("filter-status").addEventListener("change", () => loadLeads());
  document.getElementById("filter-grade").addEventListener("change", () => loadLeads());
  document.getElementById("refresh-btn").addEventListener("click", () => refreshAll());
  document.getElementById("refresh-queue-btn").addEventListener("click", () => loadQueue());
  document.getElementById("queue-include-initial").addEventListener("change", () => loadQueue());

  document.getElementById("leads-tbody").addEventListener("change", async (e) => {
    const id = e.target.dataset.leadStatus;
    if (!id) return;
    await api(`/api/leads/${id}`, { method: "PATCH", body: JSON.stringify({ status: e.target.value }) });
    showToast(`Updated status to ${e.target.value}`);
    await loadAnalytics();
    await loadQueue();
  });

  document.body.addEventListener("click", async (e) => {
    const inspectId = e.target.dataset.inspect;
    if (inspectId) return openLeadDrawer(inspectId);

    const previewId = e.target.dataset.preview;
    if (previewId) return openEmailPreview(previewId, e.target.dataset.stage || "initial");

    const markSentId = e.target.dataset.markSent;
    if (markSentId) {
      await api(`/api/leads/${markSentId}/mark-sent`, {
        method: "POST",
        body: JSON.stringify({ stage: e.target.dataset.stage || "initial" }),
      });
      showToast("Marked stage as sent");
      await refreshAll();
    }
  });

  document.getElementById("close-drawer").onclick = () => document.getElementById("lead-drawer").classList.add("hidden");
  document.getElementById("open-run-modal").onclick = () => document.getElementById("run-modal").classList.remove("hidden");
  document.getElementById("close-run-modal").onclick = () => document.getElementById("run-modal").classList.add("hidden");
  document.getElementById("open-add-lead").onclick = () => document.getElementById("add-lead-modal").classList.remove("hidden");
  document.getElementById("close-add-modal").onclick = () => document.getElementById("add-lead-modal").classList.add("hidden");
  document.getElementById("close-email-modal").onclick = () => document.getElementById("email-modal").classList.add("hidden");

  document.getElementById("copy-email-btn").onclick = async () => {
    const subj = document.getElementById("preview-subject").textContent;
    const body = document.getElementById("preview-body").textContent;
    await navigator.clipboard.writeText(`Subject: ${subj}\n\n${body}`);
    showToast("Copied email draft to clipboard");
  };

  document.getElementById("run-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const form = e.target;
    const btn = document.getElementById("run-submit-btn");
    btn.disabled = true;
    btn.textContent = "Running...";
    try {
      const payload = {
        mode: form.mode.value,
        city: form.city.value,
        niche: form.niche.value,
        limit: Number(form.limit.value),
        date: form.date.value || null,
        enrich: form.enrich.checked,
        draft_emails: form.draft_emails.checked,
      };
      const res = await api("/api/pipeline/run", { method: "POST", body: JSON.stringify(payload) });
      document.getElementById("run-modal").classList.add("hidden");
      showToast(`Pipeline complete: +${res.summary.added_count} leads, ${res.queue_count} queued`);
      await refreshAll();
    } catch (err) {
      showToast(`Error: ${err.message}`);
    } finally {
      btn.disabled = false;
      btn.textContent = "Execute Pipeline";
    }
  });

  document.getElementById("add-lead-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const fd = new FormData(e.target);
    const payload = Object.fromEntries(fd.entries());
    await api("/api/leads", { method: "POST", body: JSON.stringify(payload) });
    e.target.reset();
    document.getElementById("add-lead-modal").classList.add("hidden");
    showToast("Lead added to CRM");
    await refreshAll();
  });
}

document.addEventListener("DOMContentLoaded", () => {
  document.getElementById("run-date-input").value = new Date().toISOString().slice(0, 10);
  bindEvents();
  refreshAll().catch((err) => showToast(`Failed loading CRM: ${err.message}`));
});
