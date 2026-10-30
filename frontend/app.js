const $ = (id) => document.getElementById(id);
const esc = (s) => (s == null ? "" : String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])));
const tidy = (s) => String(s == null ? "" : s).replace(/\s+/g, " ").trim();

let files = [];
let ref = { waste_types: [], carriers: [], storage_kinds: [], waste_categories: ["Fit-out", "Strip-out"], retention_years: { controlled: 2, hazardous: 3 } };
const results = {};
const statuses = {};
const drafts = {};    // file -> [ticket draft]
const records = {};   // "file#ticketIndex" -> confirmed record
const ticked = new Set();
let selected = null;
let viewerFile = null;
let instance = null;
let highlights = [];
let activeRowEl = null;
let busy = false;
let stopRequested = false;
let currentAbort = null;
let webSdkLicenseKey = "";  // empty = Web SDK evaluation mode

const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};

/* ---------------------------------------------------------------- init */

async function init() {
  try {
    const [f, r, recs, cfg] = await Promise.all([
      fetch("/api/files").then((x) => x.json()),
      fetch("/api/reference").then((x) => x.json()),
      fetch("/api/records").then((x) => x.json()),
      fetch("/api/config").then((x) => x.json()),
    ]);
    files = f;
    ref = r;
    webSdkLicenseKey = cfg.web_sdk_license_key || "";
    recs.forEach((rec) => (records[`${rec.document.source_file}#${rec.document.ticket_index}`] = rec));
  } catch {
    toast("Could not reach the API.", true);
    return;
  }
  const cat = $("defaultCategory");
  cat.innerHTML = `<option value="">Ask for each ticket</option>` + ref.waste_categories.map((c) => `<option>${esc(c)}</option>`).join("");
  cat.value = store.get("defaultCategory") || "";
  cat.onchange = () => store.set("defaultCategory", cat.value);
  $("carrierList").innerHTML = ref.carriers.map((c) => `<option value="${esc(c)}">`).join("");
  $("storageList").innerHTML = ref.storage_kinds.map((c) => `<option value="${esc(c)}">`).join("");

  files.forEach((f) => (statuses[f.name] = "pending"));
  // Reopen previous readings so a demo starts from the last results.
  await Promise.all(files.filter((f) => f.cached).map(async (f) => {
    try {
      const res = await fetch(`/api/result?file=${encodeURIComponent(f.name)}`);
      if (res.ok) { results[f.name] = await res.json(); statuses[f.name] = "done"; }
    } catch { /* read it again */ }
  }));
  $("docCount").textContent = files.length;
  renderFileList();
  updateButtons();
}

function fmtPages(pages) {
  if (!pages) return "unknown length";
  return pages + (pages > 1 ? " pages" : " page");
}

function ticketState(name, ti) {
  if (records[`${name}#${ti}`]) return "sent";
  return "draft";
}

function renderFileList() {
  const list = $("fileList");
  list.innerHTML = "";
  for (const f of files) {
    const st = statuses[f.name];
    const card = document.createElement("div");
    card.className = "file-card" + (st === "done" ? " done" : "") + (selected === f.name ? " selected" : "");
    const r = results[f.name];
    const labels = { pending: "Not read", processing: "Reading", done: "Read", error: "Error" };
    let score = "";
    if (r) {
      const n = r.tickets.length;
      const sent = r.tickets.filter((t, ti) => ticketState(f.name, ti) === "sent").length;
      const toCheck = r.summary.to_review;
      score = `<div class="fscore">${n} ticket${n === 1 ? "" : "s"} · ${
        sent === n && n ? `<b class="good">all sent to the register</b>`
          : sent ? `<b class="good">${sent} sent</b> · ${n - sent} to confirm`
            : toCheck ? `<b class="poor">${toCheck} entr${toCheck === 1 ? "y" : "ies"} to check</b>` : `<b class="good">ready to confirm</b>`}</div>`;
    }
    card.innerHTML = `
      <div class="pick-circle ${ticked.has(f.name) ? "on" : ""}" role="checkbox" aria-checked="${ticked.has(f.name)}">&#10003;</div>
      <div>
        <div class="fname">${esc(f.name)}</div>
        <div class="fmeta">
          <span>${fmtPages(f.pages)}</span>
          <span class="status-pill ${st}">${labels[st]}</span>
        </div>
        ${score}
      </div>
    `;
    card.querySelector(".pick-circle").onclick = (e) => {
      e.stopPropagation();
      if (ticked.has(f.name)) ticked.delete(f.name); else ticked.add(f.name);
      updateButtons();
      renderFileList();
    };
    if (st === "done") card.onclick = () => selectFile(f.name);
    list.appendChild(card);
  }
}

function updateButtons() {
  const btn = $("reviewBtn");
  if (ticked.size) btn.textContent = `Read selected (${ticked.size})`;
  else btn.textContent = files.some((f) => statuses[f.name] === "done") ? "Read all again" : "Read all tickets";
  const sent = Object.keys(records).length;
  $("exportBtn").hidden = !sent;
  $("exportBtn").textContent = `Register entries (${sent})`;
}

/* ---------------------------------------------------------- processing */

async function reviewAll() {
  if (busy) return;
  busy = true;
  stopRequested = false;
  $("reviewBtn").disabled = true;
  $("stopBtn").hidden = false;
  $("stopBtn").disabled = false;
  $("progressTrack").hidden = false;
  $("progressLabel").hidden = false;

  const queue = ticked.size ? files.filter((f) => ticked.has(f.name)) : files.slice();
  const total = queue.length;
  let done = 0;
  let stopped = false;
  const t0 = Date.now();

  for (const f of queue) {
    if (stopRequested) { stopped = true; break; }
    statuses[f.name] = "processing";
    renderFileList();
    setProgress(done / total, `Reading ${f.name} (${done + 1} of ${total})`);

    const job = (crypto.randomUUID && crypto.randomUUID()) || String(Date.now());
    const timer = setInterval(async () => {
      try {
        const p = await (await fetch(`/api/progress?job=${encodeURIComponent(job)}`)).json();
        if (!p.known) return;
        setProgress((done + 0.5) / total, `Reading ${f.name} (${done + 1} of ${total}) · ${p.stage}`);
      } catch { /* keep polling */ }
    }, 900);

    currentAbort = new AbortController();
    try {
      const res = await fetch(`/api/process?file=${encodeURIComponent(f.name)}&job=${encodeURIComponent(job)}`,
        { method: "POST", signal: currentAbort.signal });
      const data = await res.json();
      if (data.detail || data.error) throw new Error(data.detail || data.error);
      results[f.name] = data;
      delete drafts[f.name];
      statuses[f.name] = "done";
      done++;
      if (selected === f.name || !selected) selectFile(f.name);
    } catch (err) {
      if (err.name === "AbortError") {
        statuses[f.name] = results[f.name] ? "done" : "pending";
        stopped = true;
      } else {
        statuses[f.name] = "error";
        toast(`${f.name}: ${err.message}`, true);
        done++;
      }
    } finally {
      clearInterval(timer);
      currentAbort = null;
    }
    renderFileList();
    if (stopped) break;
    setProgress(done / total, `Read ${done} of ${total}`);
  }

  const secs = Math.round((Date.now() - t0) / 1000);
  if (stopped) setProgress(done / total, `Stopped · ${done} of ${total} read`);
  else setProgress(1, `Done · ${total} ticket file${total > 1 ? "s" : ""} in ${secs >= 60 ? Math.floor(secs / 60) + "m " + (secs % 60) + "s" : secs + "s"}`);
  busy = false;
  stopRequested = false;
  ticked.clear();
  $("reviewBtn").disabled = false;
  $("stopBtn").hidden = true;
  renderFileList();
  updateButtons();
}

function stopRun() {
  if (!busy) return;
  stopRequested = true;
  $("stopBtn").disabled = true;
  $("progressLabel").textContent = "Stopping...";
  if (currentAbort) currentAbort.abort();
}

function setProgress(frac, label) {
  $("progressBar").style.width = Math.round(frac * 100) + "%";
  $("progressLabel").textContent = label;
}

/* -------------------------------------------------------------- upload */

async function uploadFiles(list) {
  const added = [];
  for (const file of list) {
    try {
      const res = await fetch(`/api/upload?name=${encodeURIComponent(file.name)}`, {
        method: "POST", headers: { "Content-Type": "application/pdf" }, body: file,
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || res.statusText);
      added.push(data.name);
    } catch (err) {
      toast(`${file.name}: ${err.message}`, true);
    }
  }
  if (!added.length) return;
  files = await (await fetch("/api/files")).json();
  files.forEach((f) => { if (!statuses[f.name]) statuses[f.name] = "pending"; });
  added.forEach((n) => { statuses[n] = "pending"; delete results[n]; delete drafts[n]; ticked.add(n); });
  $("docCount").textContent = files.length;
  renderFileList();
  updateButtons();
  toast(`${added.length} ticket file${added.length > 1 ? "s" : ""} added. Press "Read selected" to read ${added.length > 1 ? "them" : "it"}.`);
}

/* -------------------------------------------------------------- export */

function exportEntries() {
  const entries = [];
  for (const rec of Object.values(records)) {
    for (const e of rec.entries) {
      entries.push({
        ...e,
        retain_until: rec.retention.retain_until,
        document_url: rec.document.archive_url ? location.origin + rec.document.archive_url : null,
        source_file: rec.document.source_file,
        record_id: rec.id,
      });
    }
  }
  const blob = new Blob([JSON.stringify({ generated: new Date().toISOString(), entries }, null, 1)], { type: "application/json" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "waste-collections.json";
  document.body.appendChild(a);
  a.click();
  a.remove();
  toast(`${entries.length} register entr${entries.length === 1 ? "y" : "ies"} exported.`);
}

/* -------------------------------------------------------------- drafts */

const typeById = (id) => ref.waste_types.find((t) => t.id === id);
const isHazardous = (id) => (typeById(id) || {}).category === "hazardous";

function fmtDate(iso) {
  if (!iso) return "";
  const [y, m, d] = iso.split("-");
  return `${d}/${m}/${y}`;
}

function addYears(iso, years) {
  if (!iso || !/^\d{4}-\d{2}-\d{2}$/.test(iso)) return null;
  const [y, m, d] = iso.split("-").map(Number);
  const day = m === 2 && d === 29 ? 28 : d;
  return `${y + years}-${String(m).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
}

// The reviewer's working copy of a ticket, seeded from the reading. What the
// reader proposed is kept in `proposed` so the confirmed record can list the
// corrections made.
function draftFor(name, ti) {
  drafts[name] = drafts[name] || [];
  if (drafts[name][ti]) return drafts[name][ti];
  const t = results[name].tickets[ti];
  const first = t.entries[0] || {};
  const rec = records[`${name}#${ti}`];
  const d = {
    collection_date: first.collection_date || "",
    waste_ticket_number: t.fields.ticket_number.value || "",
    waste_carrier: first.waste_carrier || "",
    storage_type: first.storage_type || "",
    waste_category: $("defaultCategory").value || "",
    entries: t.entries.map((e, i) => ({ line: i, waste_type_id: e.waste_type_id, bins_collected: e.bins_collected, storage_type: e.storage_type })),
    edited: new Set(),
  };
  d.proposed = JSON.parse(JSON.stringify({ ...d, edited: undefined }));
  if (rec) {
    // A ticket already sent to the register shows what was sent.
    const e0 = rec.entries[0];
    Object.assign(d, {
      collection_date: e0.collection_date, waste_ticket_number: e0.waste_ticket_number, waste_carrier: e0.waste_carrier,
      storage_type: e0.storage_type, waste_category: e0.waste_category,
      entries: rec.entries.map((e, i) => ({ line: i < t.lines.length ? i : null, waste_type_id: e.waste_type_id, bins_collected: e.bins_collected, storage_type: e.storage_type })),
    });
  }
  drafts[name][ti] = d;
  return d;
}

function corrections(d) {
  const out = [];
  for (const k of ["collection_date", "waste_ticket_number", "waste_carrier", "storage_type"]) {
    if (tidy(d[k]) !== tidy(d.proposed[k])) out.push({ field: k, extracted: d.proposed[k] || null, corrected: d[k] || null });
  }
  const before = d.proposed.entries.map((e) => `${e.waste_type_id}x${e.bins_collected}`).join(",");
  const after = d.entries.map((e) => `${e.waste_type_id}x${e.bins_collected}`).join(",");
  if (before !== after) {
    const label = (e) => `${(typeById(e.waste_type_id) || {}).label || "?"} x ${e.bins_collected == null ? "?" : e.bins_collected}`;
    out.push({ field: "waste_lines", extracted: d.proposed.entries.map(label), corrected: d.entries.map(label) });
  }
  return out;
}

// Fields still flagged by the reader and not yet changed by the reviewer.
function openFlags(t, d) {
  const flags = new Set();
  t.entries.forEach((e) => e.review.forEach((k) => flags.add(k)));
  for (const c of t.checks || []) flags.add(c.field);
  flags.delete("waste_category");
  for (const k of d.edited) flags.delete(k);
  return flags;
}

function missingForConfirm(d) {
  const miss = [];
  if (!/^\d{4}-\d{2}-\d{2}$/.test(d.collection_date || "")) miss.push("collection date");
  if (!tidy(d.waste_ticket_number)) miss.push("ticket number");
  if (!tidy(d.waste_carrier)) miss.push("carrier");
  if (!d.waste_category) miss.push("waste category");
  if (!tidy(d.storage_type) && !d.entries.every((e) => tidy(e.storage_type))) miss.push("storage type");
  if (!d.entries.length) miss.push("a waste line");
  if (d.entries.some((e) => !e.waste_type_id)) miss.push("waste type");
  if (d.entries.some((e) => !(Number(e.bins_collected) >= 1))) miss.push("bins/skips");
  return miss;
}

/* -------------------------------------------------------------- review */

async function selectFile(name) {
  selected = name;
  renderFileList();
  renderReview(name);
  await openInViewer(name);
}

function confClass(c) { return c >= 80 ? "high" : c >= 55 ? "mid" : "low"; }

function confCell(field) {
  if (!field || field.value == null) return `<div class="conf"></div>`;
  if (field.image) return `<div class="conf"><div class="conf-num mid">&#9998;</div><div class="conf-cap">handwriting</div></div>`;
  return `<div class="conf"><div class="conf-num ${confClass(field.confidence)}">${field.confidence}%</div><div class="conf-cap">confidence</div></div>`;
}

// One row of the waste collection form: an editable value, the quote it was read
// from, the confidence, and a note when the reviewer should check it.
function row({ key, label, control, raw, field, note, ref: locate, locked }) {
  const quote = raw ? `<div class="fraw">${esc(tidy(raw))}</div>` : "";
  const noteHtml = note ? `<div class="fnote">${esc(note)}</div>` : "";
  return `
    <div class="form-row ${locate && !locked ? "clickable" : ""}" ${locate ? `data-ref="${esc(locate)}"` : ""} data-key="${esc(key || "")}">
      <div class="flabel">${esc(label)}</div>
      <div class="fcontrol">${control}${quote}<div class="fnote-slot">${noteHtml}</div></div>
      ${confCell(field)}
    </div>`;
}

function wasteTypeOptions(selectedId) {
  const group = (cat, title) => `<optgroup label="${title}">${ref.waste_types.filter((t) => t.category === cat)
    .sort((a, b) => a.label.localeCompare(b.label))
    .map((t) => `<option value="${t.id}" ${t.id === selectedId ? "selected" : ""}>${esc(t.label)}</option>`).join("")}</optgroup>`;
  return `<option value="" ${selectedId ? "" : "selected"}>Choose a waste type</option>` + group("controlled", "Non-hazardous") + group("hazardous", "Hazardous");
}

function renderReview(name) {
  const r = results[name];
  if (!r) {
    $("reviewEmpty").hidden = false;
    $("reviewBody").hidden = true;
    return;
  }
  $("reviewEmpty").hidden = true;
  const body = $("reviewBody");
  body.hidden = false;

  const s = r.summary;
  let html = `
    <div class="doc-title">
      <h2>${esc(name)}</h2>
      <p>${r.processing_seconds != null ? `Read in ${r.processing_seconds}s · ` : ""}${r.page_count} page${r.page_count > 1 ? "s" : ""} · ${s.tickets} ticket${s.tickets === 1 ? "" : "s"} · ${s.entries} register entr${s.entries === 1 ? "y" : "ies"}${s.image_pages ? ` · ${s.image_pages} page${s.image_pages > 1 ? "s" : ""} read as handwriting` : ""}</p>
    </div>
  `;
  if (!r.tickets.length) {
    html += `<div class="ticket-card"><div class="ticket-head"><div><h3>No waste transfer note found</h3><div class="tmeta">The document does not look like a ticket the reader knows.</div></div></div></div>`;
  }
  r.tickets.forEach((t, ti) => { html += `<div class="ticket-card" id="ticket-${ti}"></div>`; });
  body.innerHTML = html;
  r.tickets.forEach((t, ti) => renderTicket(name, ti));
}

function renderTicket(name, ti) {
  const r = results[name];
  const t = r.tickets[ti];
  const f = t.fields;
  const d = draftFor(name, ti);
  const rec = records[`${name}#${ti}`];
  const locked = !!rec;
  const first = t.entries[0] || {};
  const review = new Set(t.entries.flatMap((e) => e.review));
  const checks = t.checks || [];
  const checkFor = (k) => checks.filter((c) => c.field === k).map((c) => c.message).join(". ");
  const flag = (k, text) => (!locked && review.has(k) && !d.edited.has(k) ? text : "");
  const dis = locked ? "disabled" : "";
  const perLineStorage = t.lines.some((l) => l.container && l.container.value) && !f.container.value;
  const card = $(`ticket-${ti}`);

  const imageNote = (field) => (field && field.image ? (field.approx ? "Handwriting read from the page image (approximate position), please check" : "Handwriting read from the page image, please check") : "");
  const labelFix = f.ticket_number_label && f.ticket_number_label.value && !/ticket|note|advice|job|docket|consign|^no/i.test(f.ticket_number_label.value)
    ? `Taken from the "${f.ticket_number_label.value}" field, please confirm it is the ticket number` : "";

  const entriesHtml = d.entries.map((e, ei) => {
    const line = e.line != null ? t.lines[e.line] : null;
    const proposed = e.line != null ? t.entries[e.line] : null;
    const rawLine = line ? [line.description.value, line.ewc_code.value].filter(Boolean).join(" · ") : null;
    const qtyRaw = line && line.quantity && line.quantity.value ? line.quantity.value : null;
    const wtField = line ? (line.ewc_code.value ? line.ewc_code : line.description) : null;
    const typeNote = !locked && proposed && !d.edited.has(`type:${ei}`) && proposed.review.includes("waste_type")
      ? (!proposed.waste_type ? "No waste type matched, please choose one"
        : ["nearest", "description"].includes(proposed.match.waste_type.method) ? "EWC code not in the waste types list, nearest type suggested"
          : wtField && wtField.image ? "Read from handwriting, please check" : "Please confirm the waste type") : "";
    const binsNote = !locked && proposed && !d.edited.has(`bins:${ei}`) && (proposed.review.includes("bins_collected") || proposed.suspect)
      ? (proposed.suspect ? "Tick without a number: may be the crossing-out line"
        : e.bins_collected == null ? "Count not stated for this line"
          : proposed.bins_assumed ? "One container assumed (skip exchange)"
            : line && line.tick_only ? "Ticked without a number: counted as 1"
              : t.totals_agree ? "" : "Please confirm the count") : "";
    const haz = e.waste_type_id ? `<span class="cat ${isHazardous(e.waste_type_id) ? "hazardous" : "controlled"}">${isHazardous(e.waste_type_id) ? "Hazardous" : "Non-hazardous"}</span>` : "";
    return `
      <div class="entry ${proposed && proposed.suspect && !locked ? "suspect" : ""}" data-entry="${ei}">
        <div class="entry-title">
          <span>Waste line ${ei + 1}${d.entries.length > 1 ? ` of ${d.entries.length}` : ""}</span>
          ${haz}
          ${locked ? "" : `<button class="link-btn remove" data-remove="${ei}" title="Remove this waste line">Remove</button>`}
        </div>
        ${row({ key: `type:${ei}`, label: "Waste type", control: `<select class="in" data-field="waste_type_id" data-entry="${ei}" ${dis}>${wasteTypeOptions(e.waste_type_id)}</select>`, raw: rawLine, field: wtField, note: typeNote, ref: line ? `${ti}:line:${e.line}` : null, locked })}
        ${row({ key: `bins:${ei}`, label: "No. bins/skips collected", control: `<input class="in in-num" type="number" min="1" step="1" data-field="bins_collected" data-entry="${ei}" value="${e.bins_collected == null ? "" : e.bins_collected}" ${dis}>`, raw: qtyRaw && String(qtyRaw) !== String(e.bins_collected) ? `QTY: ${qtyRaw}` : null, field: line && line.quantity && line.quantity.value ? line.quantity : null, note: binsNote, ref: line ? `${ti}:qty:${e.line}` : null, locked })}
        ${perLineStorage ? row({ key: `storage:${ei}`, label: "Storage type", control: `<input class="in" list="storageList" data-field="storage_type" data-entry="${ei}" value="${esc(e.storage_type || "")}" ${dis}>`, raw: line && line.container ? line.container.value : null, field: line ? line.container : null, locked }) : ""}
      </div>`;
  }).join("");

  const totalNote = checkFor("bins_collected");
  const flags = openFlags(t, d);
  const missing = missingForConfirm(d);

  card.className = "ticket-card" + (locked ? " sent" : "");
  card.innerHTML = `
    <div class="ticket-head">
      <div>
        <h3>Ticket ${esc(d.waste_ticket_number || f.ticket_number.value || "?")}</h3>
        <div class="tmeta">${esc(d.waste_carrier || f.waste_carrier.value || "carrier unknown")}${t.pages && t.pages.length ? ` · page${t.pages.length > 1 ? "s" : ""} ${t.pages.join(", ")}` : t.page ? ` · page ${t.page}` : ""}${f.vehicle.value ? ` · ${esc(f.vehicle.value)}` : ""}${t.tick_table ? " · tick-box note" : ""}</div>
      </div>
      <span class="badge ${badge(locked, flags).cls}" data-badge>${badge(locked, flags).text}</span>
    </div>
    ${totalNote && !locked ? `<div class="ticket-alert">${esc(totalNote)}</div>` : ""}
    ${t.totals_agree && !locked ? `<div class="ticket-ok">Waste line counts add up to the ${esc(tidy(f.quantity.value))} total on the note</div>` : ""}
    <div class="form-section">Record waste collection</div>
    ${row({ key: "collection_date", label: "Collection date", control: `<input class="in" type="date" data-field="collection_date" value="${esc(d.collection_date)}" ${dis}>`, raw: f.collection_date.value && tidy(f.collection_date.value) !== fmtDate(d.collection_date) ? f.collection_date.value : null, field: f.collection_date, note: flag("collection_date", checkFor("collection_date") || imageNote(f.collection_date) || (d.collection_date ? "Date format unusual, please confirm" : "No date read on the note")), ref: `${ti}:collection_date`, locked })}
    ${row({ key: "waste_ticket_number", label: "Waste ticket number", control: `<input class="in" data-field="waste_ticket_number" value="${esc(d.waste_ticket_number)}" ${dis}>`, raw: f.ticket_number_label && f.ticket_number_label.value ? `${f.ticket_number_label.value.replace(/[:.\s]+$/, "")}: ${f.ticket_number.value}` : null, field: f.ticket_number, note: flag("waste_ticket_number", labelFix || imageNote(f.ticket_number) || "Please confirm the ticket number"), ref: `${ti}:ticket_number`, locked })}
    ${row({ key: "waste_carrier", label: "Waste carrier", control: `<input class="in" list="carrierList" data-field="waste_carrier" value="${esc(d.waste_carrier)}" ${dis}>`, raw: f.waste_carrier.value && tidy(f.waste_carrier.value) !== tidy(d.waste_carrier) ? f.waste_carrier.value : null, field: f.waste_carrier, note: flag("waste_carrier", first.waste_carrier ? "Carrier not matched with certainty to the carrier list" : "No carrier read on the note"), ref: `${ti}:waste_carrier`, locked })}
    ${perLineStorage ? "" : row({ key: "storage_type", label: "Storage type", control: `<input class="in" list="storageList" data-field="storage_type" value="${esc(d.storage_type)}" ${dis}>`, raw: f.container.value && tidy(f.container.value) !== tidy(d.storage_type) ? f.container.value : null, field: f.container, note: flag("storage_type", "Container type not recognised, please pick it"), ref: `${ti}:container`, locked })}
    ${row({ key: "waste_category", label: "Waste category", control: `<div class="seg" role="radiogroup">${ref.waste_categories.map((c) => `<button type="button" class="seg-btn ${d.waste_category === c ? "on" : ""}" data-category="${esc(c)}" ${dis}>${esc(c)}</button>`).join("")}</div>`, note: !locked && !d.waste_category ? "Not printed on tickets: choose fit-out or strip-out" : "", locked })}
    ${d.entries.length > 1 || f.quantity.value ? row({ key: "total", label: "Total on the note", control: `<div class="fvalue">${f.quantity.value ? esc(tidy(f.quantity.value)) : "Not stated"}${d.entries.length > 1 ? ` <span class="muted">· ${d.entries.reduce((a, e) => a + (Number(e.bins_collected) || 0), 0)} across the waste lines</span>` : ""}</div>`, field: f.quantity.value ? f.quantity : null, ref: f.quantity.value ? `${ti}:quantity` : null, locked }) : ""}
    <div class="form-section">Waste lines</div>
    ${entriesHtml || `<div class="entry"><div class="fnote pad">No waste line read: add one below.</div></div>`}
    ${locked ? "" : `<div class="entry-add"><button class="link-btn" data-add>+ Add a waste line</button></div>`}
    ${f.site.value ? `<div class="form-section">From the note</div>` + row({ key: "site", label: "Site", control: `<div class="fvalue">${esc(f.site.value)}</div>`, field: f.site, ref: `${ti}:site`, locked }) : ""}
    <div class="ticket-foot">
      <div class="retention">${retentionText(d)}</div>
      ${locked
        ? `<div class="sent-links">
            ${rec.document.archive_url ? `<a href="${esc(rec.document.archive_url)}" target="_blank" rel="noopener">Archived ticket PDF</a><a href="${esc(rec.document.archive_url)}&data=true" target="_blank" rel="noopener">Data read back from the PDF</a>` : `<span class="fnote">Archive failed: ${esc(rec.document.archive_error || "")}</span>`}
            <button class="btn btn-quiet-blue" data-reopen>Edit</button>
          </div>`
        : `<div class="confirm-bar">
            <span class="missing">${missingText(missing)}</span>
            <button class="btn btn-primary" data-confirm ${missing.length ? "disabled" : ""}>Confirm &amp; send to register</button>
          </div>`}
    </div>
  `;
  wireTicket(name, ti, card);
}

const badge = (locked, flags) => locked ? { cls: "ok", text: "Sent to register" }
  : flags.size ? { cls: "review", text: `${flags.size} to check` } : { cls: "ready", text: "Ready to confirm" };
const missingText = (missing) => (missing.length ? "Missing: " + esc(missing.join(", ")) : "");

function retentionText(d) {
  const hazardous = d.entries.some((e) => isHazardous(e.waste_type_id));
  const years = hazardous ? ref.retention_years.hazardous : ref.retention_years.controlled;
  const until = addYears(d.collection_date, years);
  return until ? `Keep the ticket until <b>${fmtDate(until)}</b> · ${hazardous ? "hazardous" : "non-hazardous"}, ${years} years`
    : "Retention date follows from the collection date";
}

// Typing in a text field updates the ticket's summary without re-rendering
// the form (which would steal the focus).
function refreshMeta(name, ti, card) {
  const t = results[name].tickets[ti];
  const d = draftFor(name, ti);
  const b = badge(false, openFlags(t, d));
  const el = card.querySelector("[data-badge]");
  el.className = `badge ${b.cls}`;
  el.textContent = b.text;
  const missing = missingForConfirm(d);
  card.querySelector(".missing").innerHTML = missingText(missing);
  card.querySelector("[data-confirm]").disabled = missing.length > 0;
  card.querySelector(".retention").innerHTML = retentionText(d);
  card.querySelector(".ticket-head h3").textContent = `Ticket ${d.waste_ticket_number || "?"}`;
}

function wireTicket(name, ti, card) {
  const r = results[name];
  const t = r.tickets[ti];
  const d = draftFor(name, ti);

  card.querySelectorAll(".in").forEach((el) => {
    const commit = (full) => {
      const field = el.dataset.field;
      const ei = el.dataset.entry;
      let value = el.value;
      if (field === "waste_type_id") value = value ? Number(value) : null;
      if (field === "bins_collected") value = value === "" ? null : Number(value);
      if (ei != null) {
        d.entries[Number(ei)][field] = value;
        d.edited.add(`${field === "waste_type_id" ? "type" : field === "bins_collected" ? "bins" : "storage"}:${ei}`);
        if (field === "bins_collected") d.edited.add("bins_collected");
        if (field === "waste_type_id") d.edited.add("waste_type");
      } else {
        d[field] = value;
        d.edited.add(field);
      }
      if (full) { renderTicket(name, ti); return; }
      const slot = el.closest(".form-row").querySelector(".fnote-slot");
      if (slot) slot.innerHTML = "";
      refreshMeta(name, ti, card);
    };
    // Selects and dates re-render (hazard chip, retention); text fields update in place.
    const full = el.tagName === "SELECT";
    el.oninput = () => commit(full);
    if (!full) el.onchange = () => commit(false);
    el.onfocus = () => { const rowEl = el.closest(".form-row"); if (rowEl && rowEl.dataset.ref) locate(name, rowEl); };
  });
  card.querySelectorAll("[data-category]").forEach((b) => {
    b.onclick = () => { d.waste_category = b.dataset.category; renderTicket(name, ti); };
  });
  card.querySelectorAll("[data-remove]").forEach((b) => {
    b.onclick = () => { d.entries.splice(Number(b.dataset.remove), 1); d.edited.add("waste_type"); d.edited.add("bins_collected"); renderTicket(name, ti); };
  });
  const add = card.querySelector("[data-add]");
  if (add) add.onclick = () => { d.entries.push({ line: null, waste_type_id: null, bins_collected: 1, storage_type: d.storage_type }); renderTicket(name, ti); };
  card.querySelectorAll(".form-row.clickable").forEach((el) => {
    el.onclick = (e) => { if (!e.target.closest(".in, .seg, button, a")) locate(name, el); };
  });
  const confirmBtn = card.querySelector("[data-confirm]");
  if (confirmBtn) confirmBtn.onclick = () => confirmTicket(name, ti, confirmBtn);
  const reopen = card.querySelector("[data-reopen]");
  if (reopen) reopen.onclick = () => reopenTicket(name, ti);
}

async function confirmTicket(name, ti, btn) {
  const t = results[name].tickets[ti];
  const d = draftFor(name, ti);
  btn.disabled = true;
  btn.textContent = "Sending...";
  const payload = {
    file: name,
    ticket_index: ti,
    pages: t.pages && t.pages.length ? t.pages : [t.page || 1],
    collection_date: d.collection_date,
    waste_ticket_number: d.waste_ticket_number,
    waste_carrier: d.waste_carrier,
    storage_type: d.storage_type,
    waste_category: d.waste_category,
    site: t.fields.site.value,
    entries: d.entries.map((e) => ({ waste_type_id: e.waste_type_id, bins_collected: e.bins_collected, storage_type: e.storage_type || d.storage_type })),
    corrections: corrections(d),
  };
  try {
    const res = await fetch("/api/confirm", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    const data = await res.json();
    if (!res.ok) throw new Error(data.detail || res.statusText);
    records[`${name}#${ti}`] = data;
    renderTicket(name, ti);
    renderFileList();
    updateButtons();
    toast(`Ticket ${data.entries[0].waste_ticket_number}: ${data.entries.length} entr${data.entries.length === 1 ? "y" : "ies"} sent to the register, ticket archived with its data.`);
  } catch (err) {
    toast(`Not sent: ${err.message}`, true);
    btn.disabled = false;
    btn.textContent = "Confirm & send to register";
  }
}

async function reopenTicket(name, ti) {
  const rec = records[`${name}#${ti}`];
  if (!rec) return;
  try {
    const res = await fetch(`/api/records?id=${encodeURIComponent(rec.id)}`, { method: "DELETE" });
    if (!res.ok && res.status !== 404) throw new Error(res.statusText);
  } catch (err) {
    toast(`Could not reopen: ${err.message}`, true);
    return;
  }
  delete records[`${name}#${ti}`];
  renderTicket(name, ti);
  renderFileList();
  updateButtons();
}

/* -------------------------------------------------------------- viewer */

function locate(name, el) {
  const r = results[name];
  const [ti, kind, idx] = el.dataset.ref.split(":");
  const t = r.tickets[Number(ti)];
  let boxes = [];
  let approx = false;
  if (kind === "line" || kind === "qty") {
    const line = t.lines[Number(idx)];
    const parts = kind === "qty" ? [line.quantity] : [line.ewc_code, line.description];
    const seen = new Set();
    for (const p of parts) for (const b of (p && p.boxes) || []) {
      const k = JSON.stringify(b);
      if (!seen.has(k)) { seen.add(k); boxes.push(b); }
    }
    approx = parts.some((p) => p && p.approx);
  } else {
    boxes = t.fields[kind].boxes;
    approx = !!t.fields[kind].approx;
  }
  if (!boxes.length) {
    toast(t.fields[kind] && t.fields[kind].image ? "Handwriting read from the page image, no position returned for it." : "No position recorded for this value.", true);
    if (t.page) jumpToPage(name, t.page);
    return;
  }
  if (activeRowEl) activeRowEl.classList.remove("active");
  el.classList.add("active");
  activeRowEl = el;
  showBoxes(name, boxes, r, approx);
}

async function jumpToPage(name, page) {
  await openInViewer(name);
  if (instance) await instance.setViewState((vs) => vs.set("currentPageIndex", page - 1));
}

async function openInViewer(name) {
  if (viewerFile === name && instance) return;
  $("viewerEmpty").style.display = "none";
  if (instance) { PSPDFKit.unload("#viewer"); instance = null; }
  highlights = [];
  viewerFile = null;

  try {
    instance = await PSPDFKit.load({
      container: "#viewer",
      document: `/api/pdf?name=${encodeURIComponent(name)}`,
      // The Web SDK assets load from the CDN the script came from.
      licenseKey: webSdkLicenseKey || undefined,
    });
    viewerFile = name;
  } catch (err) {
    const empty = $("viewerEmpty");
    empty.style.display = "";
    empty.querySelector("h3").textContent = "Viewer unavailable";
    empty.querySelector("p").textContent =
      "The document viewer could not start in this environment. Open this page in Chrome, Safari or Edge.";
    toast(`Could not open the viewer: ${err.message}`, true);
  }
}

async function clearHighlights() {
  if (!instance || highlights.length === 0) return;
  try { await instance.delete(highlights); } catch { /* already gone */ }
  highlights = [];
}

// The backend returns source bounding boxes (in the extraction's page space)
// plus each page's dimensions in the same space, so highlights map to the
// viewer with a simple ratio. Exact positions are highlighted; approximate
// ones (handwriting read from the page image) are outlined.
async function showBoxes(name, boxes, result, approx) {
  await openInViewer(name);
  if (!instance || !boxes.length) return;

  const annotations = [];
  for (const b of boxes) {
    const pageIndex = (b.page || 1) - 1;
    const meta = (result.pages || []).find((p) => p.page === (b.page || 1));
    const info = meta && instance.pageInfoForIndex(pageIndex);
    if (!info) continue;
    const sx = info.width / meta.width;
    const sy = info.height / meta.height;
    const rect = new PSPDFKit.Geometry.Rect({ left: b.x * sx, top: b.y * sy, width: b.width * sx, height: b.height * sy });
    annotations.push(approx
      ? new PSPDFKit.Annotations.RectangleAnnotation({
        pageIndex, boundingBox: rect, strokeWidth: 3,
        strokeColor: new PSPDFKit.Color({ r: 255, g: 149, b: 0 }),
      })
      : new PSPDFKit.Annotations.HighlightAnnotation({
        pageIndex, rects: PSPDFKit.Immutable.List([rect]), boundingBox: rect,
        color: new PSPDFKit.Color({ r: 255, g: 214, b: 0 }),
      }));
  }
  if (!annotations.length) { toast("No page geometry for this value.", true); return; }

  await clearHighlights();
  const firstPage = annotations[0].pageIndex;
  await instance.setViewState((vs) => vs.set("currentPageIndex", firstPage));
  instance.jumpToRect(firstPage, annotations[0].boundingBox);
  const created = await instance.create(annotations);
  highlights = created.map((a) => a.id);
}

/* --------------------------------------------------------------- toast */

let toastTimer = null;
function toast(msg, isError) {
  const el = $("toast");
  el.textContent = msg;
  el.className = "toast" + (isError ? " error" : "");
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.hidden = true), 5000);
}

$("reviewBtn").onclick = reviewAll;
$("stopBtn").onclick = stopRun;
$("exportBtn").onclick = exportEntries;
$("uploadBtn").onclick = () => $("uploadInput").click();
$("uploadInput").onchange = (e) => { uploadFiles([...e.target.files]); e.target.value = ""; };
init();
