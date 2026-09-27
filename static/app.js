"use strict";

// The server only sends confirmed vandalism that Wikipedia has already cleaned up.
const VIEWS = [["all", "All"], ["favorites", "★ Favourites"]];
const STATUS_LABEL = {reverted: "Reverted", hidden: "Hidden by admins", deleted: "Page deleted"};
const PAGE_SIZE = 50;
const REFRESH_MS = 5000;

const $ = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => (
  {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const pct = (x) => (x == null ? "–" : `${Math.round(x * 100)}%`);
const level = (x) => (x == null ? "" : x >= 0.7 ? "hi" : x >= 0.4 ? "mid" : "lo");
const fmt = (n) => Number(n || 0).toLocaleString();
const plain = (wikitext) => String(wikitext || "").replace(/\[\[(?:[^|\]]*\|)?([^\]]*)\]\]/g, "$1");
const short = (text, n) => (text.length > n ? `${text.slice(0, n - 1)}…` : text);

const state = Object.assign({view: "all", category: "", q: "", sort: "newest"}, loadState());
delete state.verdicts;   // left over from older versions
if (!VIEWS.some(([key]) => key === state.view)) state.view = "all";
let limit = PAGE_SIZE;
let lastSignature = "";
let knownRevids = null;   // revids already shown, so new arrivals can be highlighted
let status = null;
let busy = false;

function loadState() {
  try { return JSON.parse(localStorage.getItem("vt-filters")) || {}; } catch { return {}; }
}

function saveState() {
  try { localStorage.setItem("vt-filters", JSON.stringify(state)); } catch { /* storage unavailable */ }
}

function ago(iso) {
  if (!iso) return "";
  const s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
}

function until(iso) {
  const s = (Date.parse(iso) - Date.now()) / 1000;
  if (s <= 30) return "any moment";
  if (s < 3600) return `in ${Math.round(s / 60)} min`;
  return `in ${Math.round(s / 3600)} h`;
}

async function getJSON(url) {
  const resp = await fetch(url, {cache: "no-store"});
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

async function postJSON(url, body) {
  const resp = await fetch(url, {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body || {}),
  });
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

// ---------------------------------------------------------------- controls

function renderControls() {
  $("#views").innerHTML = VIEWS.map(([key, label]) =>
    `<button class="tab" role="tab" data-view="${key}" aria-selected="${state.view === key}">${esc(label)}</button>`,
  ).join("");
  $("#search").value = state.q;
  $("#sort").value = state.sort;
}

function renderCategories(counts, totalAll) {
  const names = Object.keys(counts).sort((a, b) => counts[b] - counts[a] || a.localeCompare(b));
  if (state.category && !names.includes(state.category)) names.push(state.category);
  const chip = (value, label, n) =>
    `<button class="fchip" data-category="${esc(value)}" aria-pressed="${state.category === value}">` +
    `${esc(label)}<span class="n">${fmt(n)}</span></button>`;
  $("#category-chips").innerHTML = chip("", "All categories", totalAll) +
    names.map((name) => chip(name, name, counts[name] || 0)).join("");
}

function changed() {
  saveState();
  limit = PAGE_SIZE;
  lastSignature = "";
  knownRevids = null;
  renderControls();
  refresh();
}

// ---------------------------------------------------------------- status

function renderStatus(s) {
  status = s;
  $("#n-scanned").textContent = fmt(s.stats.scanned);
  $("#n-check1").textContent = fmt(s.stats.candidates);
  $("#n-check2").textContent = fmt(s.stats.shown);

  const m = s.monitor;
  let kind = "ok";
  let text = `Watching ${s.wiki} · scanned ${ago(m.last_scan)}`;
  if (m.state === "starting" || !m.last_scan) {
    kind = "busy";
    text = "Starting up…";
  } else if (m.state === "backfilling") {
    kind = "busy";
    text = "Looking back at recent edits…";
  }
  if (s.queue.analysing) text += ` · analysing ${s.queue.analysing}`;
  if (m.last_error) {
    kind = m.state === "error" ? "bad" : "warn";
    text = m.last_error;
  }
  setPill(kind, text);

  const banner = $("#banner");
  banner.hidden = !s.slow_mode;
  if (s.slow_mode) {
    banner.innerHTML = "<b>Slow mode.</b> Wikipedia lets anonymous tools make only 10 requests a minute, so " +
      `checks take longer. Put your email address or website in <code>CONTACT</code> in <code>${esc(s.settings_where)}</code> ` +
      `and restart to get 200 a minute. (Using ${s.api_used}/${s.api_budget} requests this minute.)`;
  }
  $("#how-threshold").textContent = pct(s.threshold);
  $("#how-schedule").textContent = s.recheck_minutes.join(", ");
}

function setPill(kind, text) {
  const pill = $("#status-pill");
  pill.className = `pill ${kind}`;
  pill.querySelector(".pill-text").textContent = text;
}

// ---------------------------------------------------------------- cards

function diffHtml(e) {
  const diff = e.diff || {};
  if (diff.unavailable === "deleted") return `<div class="diff unavailable">The page was deleted, so the text can't be shown any more.</div>`;
  if (diff.unavailable) return `<div class="diff unavailable">Wikipedia admins hid this text (too offensive or private to show).</div>`;
  const hunks = diff.hunks || [];
  if (!hunks.length) {
    return e.stage === "new" ? `<div class="diff unavailable">Analysing the edit…</div>` : "";
  }
  const piece = ([op, text]) =>
    op === "+" ? `<ins>${esc(text)}</ins>` : op === "-" ? `<del>${esc(text)}</del>` : `<span>${esc(text)}</span>`;
  const shown = hunks.slice(0, 3).map((h) => `<div class="hunk">${h.map(piece).join("")}</div>`).join("");
  const more = hunks.length > 3
    ? `<div class="more-changes">+ ${hunks.length - 3} more change${hunks.length > 4 ? "s" : ""} — open the diff to see everything</div>` : "";
  return `<div class="diff">${shown}${more}</div>`;
}

function evidenceHtml(e) {
  const chips = [];
  if (e.revert_risk != null) {
    chips.push(`<span class="chip ${level(e.revert_risk)}" title="Wikimedia's revert-risk model (independent of check 1)">Revert risk ${pct(e.revert_risk)}</span>`);
  }
  if (e.damaging != null) {
    chips.push(`<span class="chip ${level(e.damaging)}" title="ORES damaging model">Damaging ${pct(e.damaging)}</span>`);
  }
  for (const finding of e.findings || []) chips.push(`<span class="chip finding">${esc(finding.label)}</span>`);
  for (const reason of (e.reasons || []).slice(1)) chips.push(`<span class="chip">${esc(reason)}</span>`);
  const why = (e.reasons || [])[0] || (e.stage === "new" ? "Analysing the edit…" : "");
  const revert = (e.community || {}).revert;
  const revertLine = revert && revert.comment
    ? `<div class="revert-comment">${esc(revert.user || "Reverter")}: “${esc(short(plain(revert.comment), 220))}”</div>` : "";
  return `
    <div class="evidence">
      <div class="check"><span class="check-name">Check 1</span>
        <div class="check-body chips">
          <span class="chip ${level(e.badfaith)}">Bad faith ${pct(e.badfaith)}</span>
          <span class="hint">Wikipedia's ORES good-faith model</span>
        </div>
      </div>
      <div class="check"><span class="check-name">Check 2</span>
        <div class="check-body">
          ${why ? `<div class="why">${esc(why)}</div>` : ""}
          <div class="chips">${chips.join("")}</div>
          ${revertLine}
        </div>
      </div>
    </div>`;
}

function checkedHtml(e) {
  return `<span class="checked" data-count="${e.check_count || 0}" data-last="${esc(e.last_checked || "")}" ` +
    `data-next="${esc(e.next_check || "")}" data-final="${e.stage === "final"}">${checkedText(e.check_count, e.last_checked, e.next_check, e.stage === "final")}</span>`;
}

function checkedText(count, last, next, final) {
  if (!count) return "Waiting for first check…";
  let text = `Checked ${count}× · last ${ago(last)}`;
  if (next) text += ` · next ${until(next)}`;
  else if (final) text += " · done";
  return text;
}

function card(e, fresh) {
  const community = e.community || {};
  const statusText = e.status === "reverted" && community.cleaned ? "Cleaned up" : (STATUS_LABEL[e.status] || e.status);
  const cats = (e.categories || []).map((c) => `<span class="cat">${esc(c)}</span>`).join("");
  const byline = [`by <a href="${esc(e.links.user)}" target="_blank" rel="noopener">${esc(e.user)}</a>`];
  if (e.edit_type === "new") byline.push("<b>created a new page</b>");
  if (e.size_change) {
    byline.push(`<span class="${e.size_change < 0 ? "size-minus" : "size-plus"}">${e.size_change > 0 ? "+" : "−"}` +
      `${fmt(Math.abs(e.size_change))} bytes</span>`);
  }
  byline.push(e.comment ? `<span class="summary">“${esc(short(plain(e.comment), 200))}”</span>`
    : `<span class="summary">no edit summary</span>`);
  return `
    <article class="card verdict-${esc(e.verdict)}${fresh ? " fresh" : ""}" data-revid="${e.revid}">
      <div class="card-head">
        <button class="star" data-action="favorite" aria-pressed="${e.favorite}"
          title="${e.favorite ? "Remove from favourites" : "Add to favourites"}">${e.favorite ? "★" : "☆"}</button>
        <div class="head-main">
          <h2><a href="${esc(e.primary.url)}" target="_blank" rel="noopener">${esc(e.title)}</a></h2>
          <div class="meta">
            <span class="badge verdict-confirmed">Confirmed vandalism</span>
            <span class="badge status-${esc(e.status)}">${esc(statusText)}</span>
            ${cats}
          </div>
        </div>
        <time class="ago" datetime="${esc(e.timestamp)}" title="${esc(new Date(e.timestamp).toLocaleString())}">${ago(e.timestamp)}</time>
      </div>
      <div class="byline">${byline.join(" · ")}</div>
      ${diffHtml(e)}
      ${evidenceHtml(e)}
      <div class="actions">
        <a class="btn primary" href="${esc(e.primary.url)}" target="_blank" rel="noopener">${esc(e.primary.label)} ↗</a>
        <a class="btn" href="${esc(e.links.diff)}" target="_blank" rel="noopener">Diff ↗</a>
        ${e.status !== "deleted" ? `<a class="btn" href="${esc(e.links.page)}" target="_blank" rel="noopener">Current page ↗</a>` : ""}
        <button class="btn" data-action="recheck">↻ Re-check now</button>
        ${checkedHtml(e)}
      </div>
    </article>`;
}

function emptyHtml() {
  if (state.view === "favorites") {
    return `<div class="empty"><b>No favourites yet</b>Click the ☆ next to any vandalism to keep it here forever.</div>`;
  }
  if (status && status.stats.scanned === 0) {
    return `<div class="empty"><b>Warming up…</b>The tracker is reading the latest edits. The first results usually show up within a few minutes.</div>`;
  }
  if (state.category || state.q) {
    return `<div class="empty"><b>Nothing matches</b>No confirmed vandalism matches this category or search yet.</div>`;
  }
  return `<div class="empty"><b>Nothing confirmed yet</b>Vandalism shows up here once it's confirmed and Wikipedia has cleaned it up. The tracker keeps re-checking every suspicious edit in the background.</div>`;
}

// Everything that changes a card's look, except the "checked N× · last …" line (updated in place).
function cardSignature(e) {
  return JSON.stringify([e.verdict, e.status, e.favorite, e.categories, e.reasons, e.findings, e.revert_risk,
    e.stage === "new", e.diff.hunks.length, e.primary.url]);
}

function renderList(data) {
  const list = $("#list");
  $("#more").hidden = data.items.length >= data.total;
  $("#count").textContent = data.total ? `Showing ${fmt(data.items.length)} of ${fmt(data.total)}` : "";
  if (!data.items.length) {
    const html = emptyHtml();
    if (lastSignature !== html) list.innerHTML = html;
    lastSignature = html;
    return;
  }
  list.querySelectorAll(":scope > :not(.card)").forEach((node) => node.remove());   // the empty-state box
  lastSignature = "cards";
  const fresh = new Set(knownRevids ? data.items.filter((e) => !knownRevids.has(e.revid)).map((e) => e.revid) : []);
  knownRevids = new Set([...(knownRevids || []), ...data.items.map((e) => e.revid)]);
  const existing = new Map([...list.querySelectorAll(".card")].map((el) => [el.dataset.revid, el]));
  let previous = null;
  for (const e of data.items) {
    const key = String(e.revid);
    const sig = cardSignature(e);
    let el = existing.get(key);
    if (!el || el.dataset.sig !== sig) {
      const template = document.createElement("template");
      template.innerHTML = card(e, fresh.has(e.revid)).trim();
      const replacement = template.content.firstElementChild;
      replacement.dataset.sig = sig;
      if (el) el.replaceWith(replacement);
      el = replacement;
    }
    existing.delete(key);
    const checked = el.querySelector(".checked");
    Object.assign(checked.dataset, {
      count: e.check_count || 0, last: e.last_checked || "", next: e.next_check || "", final: e.stage === "final",
    });
    const expected = previous ? previous.nextElementSibling : list.firstElementChild;
    if (el !== expected) list.insertBefore(el, expected);
    previous = el;
  }
  existing.forEach((el) => el.remove());
  updateTimes();
}

function updateTimes() {
  document.querySelectorAll("time.ago").forEach((t) => { t.textContent = ago(t.dateTime); });
  document.querySelectorAll(".checked").forEach((el) => {
    const d = el.dataset;
    el.textContent = checkedText(Number(d.count), d.last, d.next, d.final === "true");
  });
}

// ---------------------------------------------------------------- data

async function refresh() {
  if (busy) return;
  busy = true;
  try {
    const params = new URLSearchParams({view: state.view, sort: state.sort, limit});
    if (state.category) params.set("category", state.category);
    if (state.q) params.set("q", state.q);
    const [s, data] = await Promise.all([getJSON("/api/status"), getJSON(`/api/edits?${params}`)]);
    renderStatus(s);
    renderCategories(data.category_counts, data.total_all);
    renderList(data);
  } catch (err) {
    setPill("bad", "Can't reach the tracker — is main.py still running?");
  } finally {
    busy = false;
  }
}

document.addEventListener("click", async (ev) => {
  const view = ev.target.closest("[data-view]");
  if (view) {
    state.view = view.dataset.view;
    return changed();
  }
  const categoryChip = ev.target.closest("[data-category]");
  if (categoryChip) {
    state.category = categoryChip.dataset.category;
    return changed();
  }
  const action = ev.target.closest("[data-action]");
  if (!action) return;
  const revid = action.closest(".card").dataset.revid;
  if (action.dataset.action === "favorite") {
    const wanted = action.getAttribute("aria-pressed") !== "true";
    action.setAttribute("aria-pressed", String(wanted));
    action.textContent = wanted ? "★" : "☆";
    try {
      await postJSON(`/api/edits/${revid}/favorite`, {favorite: wanted});
    } catch {
      action.setAttribute("aria-pressed", String(!wanted));
      action.textContent = wanted ? "☆" : "★";
    }
    refresh();
  } else if (action.dataset.action === "recheck") {
    const cardEl = action.closest(".card");
    action.disabled = true;
    action.textContent = "Queued…";
    try {
      await postJSON(`/api/edits/${revid}/recheck`);
    } catch {
      action.textContent = "Failed";
    }
    setTimeout(() => { cardEl.dataset.sig = ""; refresh(); }, 4000);   // redraw this card afterwards
  }
});

let searchTimer = null;
$("#search").addEventListener("input", (ev) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.q = ev.target.value.trim(); changed(); }, 300);
});
$("#sort").addEventListener("change", (ev) => { state.sort = ev.target.value; changed(); });
$("#more").addEventListener("click", () => { limit += PAGE_SIZE; lastSignature = ""; refresh(); });

renderControls();
refresh();
setInterval(refresh, REFRESH_MS);
