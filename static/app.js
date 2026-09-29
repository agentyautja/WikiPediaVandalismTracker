"use strict";

// The server only sends confirmed vandalism that Wikipedia has already cleaned up.
const VIEWS = [["all", "All"], ["favorites", "★ Favourites"], ["watchlist", "👁 Watchlist"],
  ["history", "🔍 Page history"], ["stats", "📊 Stats"]];
const FEED_VIEWS = ["all", "favorites", "watchlist"];
const STATUS_LABEL = {reverted: "Reverted", hidden: "Hidden by admins", deleted: "Page deleted"};
const STATS_RANGES = [[1, "24 hours"], [7, "7 days"], [14, "14 days"]];
// Time-to-revert filter: "min-max" in seconds (max empty = no limit). Same buckets as the Stats chart.
const REVERT_RANGES = [["", "Any time"], ["0-60", "< 1 min"], ["60-300", "1–5 min"], ["300-900", "5–15 min"],
  ["900-3600", "15–60 min"], ["3600-21600", "1–6 h"], ["21600-", "> 6 h"]];
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
const wikiUrl = (title) => `https://en.wikipedia.org/wiki/${encodeURIComponent(title.replace(/ /g, "_"))}`;

const state = Object.assign({view: "all", category: "", q: "", sort: "newest", historyId: null, depth: 500,
  user: "", statsDays: 7, revert: ""}, loadState());
delete state.verdicts;   // left over from older versions
if (!VIEWS.some(([key]) => key === state.view)) state.view = "all";
let limit = PAGE_SIZE;
let lastSignature = "";
let knownRevids = null;   // revids already shown, so new arrivals can be highlighted
let status = null;
let busy = false;
let offenders = {};                // user -> number of confirmed vandal edits (only for 2+)
let watchedPages = new Set();      // pageids on the watchlist

function loadState() {
  try { return JSON.parse(localStorage.getItem("vt-filters")) || {}; } catch { return {}; }
}

function saveState() {
  try { localStorage.setItem("vt-filters", JSON.stringify(state)); } catch { /* storage unavailable */ }
}

function stored(key, fallback) {
  try { const v = localStorage.getItem(key); return v === null ? fallback : JSON.parse(v); } catch { return fallback; }
}

function store(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* storage unavailable */ }
}

function ago(iso) {
  if (!iso) return "";
  const s = Math.max(0, (Date.now() - Date.parse(iso)) / 1000);
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  if (s < 30 * 86400) return `${Math.round(s / 86400)} d ago`;
  return day(iso);   // old edits from a page-history search
}

function day(iso) {
  return new Date(iso).toLocaleDateString(undefined, {day: "numeric", month: "short", year: "numeric"});
}

function until(iso) {
  const s = (Date.parse(iso) - Date.now()) / 1000;
  if (s <= 30) return "any moment";
  if (s < 3600) return `in ${Math.round(s / 60)} min`;
  return `in ${Math.round(s / 3600)} h`;
}

function duration(seconds) {
  if (seconds == null) return "–";
  const s = Math.round(seconds);
  if (s < 60) return `${s} s`;
  if (s < 3600) return `${Math.round(s / 60)} min`;
  if (s < 86400) return `${(s / 3600).toFixed(s < 36000 ? 1 : 0)} h`;
  return `${Math.round(s / 86400)} days`;
}

async function getJSON(url) {
  const resp = await fetch(url, {cache: "no-store"});
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  return resp.json();
}

async function sendJSON(url, method, body) {
  const resp = await fetch(url, {
    method, headers: {"Content-Type": "application/json"}, body: JSON.stringify(body || {}),
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
  return data;
}

const postJSON = (url, body) => sendJSON(url, "POST", body);

// ---------------------------------------------------------------- controls

function renderControls() {
  const unseen = stored("vt-watch-unseen", 0);
  $("#views").innerHTML = VIEWS.map(([key, label]) => {
    const badge = key === "watchlist" && unseen ? `<span class="badge-count">${unseen}</span>` : "";
    return `<button class="tab" role="tab" data-view="${key}" aria-selected="${state.view === key}">${esc(label)}${badge}</button>`;
  }).join("");
  $("#search").value = state.q;
  $("#sort").value = state.sort;
  const feed = FEED_VIEWS.includes(state.view);
  $("#feed-tools").hidden = !feed;
  $("#feed-filters").hidden = !feed;
  $("#history-panel").hidden = state.view !== "history";
  $("#watch-panel").hidden = state.view !== "watchlist";
  $("#stats-panel").hidden = state.view !== "stats";
  $("#list").hidden = state.view === "stats";
  $("#footer-row").hidden = state.view === "stats";
  const userBar = $("#user-filter");
  userBar.hidden = !(feed && state.user);
  if (state.user) {
    userBar.innerHTML = `Showing the vandal edits by <b>${esc(state.user)}</b> · ` +
      `<a href="https://en.wikipedia.org/wiki/Special:Contributions/${encodeURIComponent(state.user)}" target="_blank" rel="noopener">their contributions ↗</a>` +
      `<button class="btn" data-user="">✕ Show everyone</button>`;
  }
  renderNotifyButton();
  renderRevertChips();
}

function revertLabel(range) {
  const known = REVERT_RANGES.find(([value]) => value === range);
  if (known) return known[1];
  const [low, high] = range.split("-");
  const m = (s) => duration(Number(s));
  return high ? `${m(low)} – ${m(high)}` : `${m(low)} or more`;
}

function renderRevertChips() {
  const custom = state.revert && !REVERT_RANGES.some(([value]) => value === state.revert);
  $("#revert-chips").innerHTML = `<span class="recent-label">Time to revert:</span>` +
    REVERT_RANGES.map(([value, label]) =>
      `<button class="fchip" data-revert="${value}" aria-pressed="${state.revert === value}">${esc(label)}</button>`).join("") +
    `<button class="fchip" data-revert-custom aria-pressed="${custom}">${custom ? esc(revertLabel(state.revert)) : "Custom…"}</button>`;
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
  if (state.view === "watchlist") store("vt-watch-unseen", 0);
  renderControls();
  refresh();
}

// ---------------------------------------------------------------- status

function renderStatus(s) {
  status = s;
  watchedPages = new Set(s.watched_pageids || []);
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
  notifyWatched(s.watch_latest || []);
}

function setPill(kind, text) {
  const pill = $("#status-pill");
  pill.className = `pill ${kind}`;
  pill.querySelector(".pill-text").textContent = text;
}

// ---------------------------------------------------------------- watchlist notifications

function notifyWatched(latest) {
  if (!latest.length) return;
  const newest = Math.max(...latest.map((e) => e.revid));
  const seen = stored("vt-watch-seen", null);
  store("vt-watch-seen", Math.max(newest, seen || 0));
  if (seen === null) return;   // first visit: don't announce what was already there
  const fresh = latest.filter((e) => e.revid > seen);
  if (!fresh.length) return;
  if (state.view !== "watchlist") {
    store("vt-watch-unseen", stored("vt-watch-unseen", 0) + fresh.length);
    renderControls();
  }
  if ("Notification" in window && Notification.permission === "granted") {
    for (const e of fresh.slice(0, 3)) {
      const note = new Notification(`Vandalism on ${e.title}`, {
        body: `Confirmed vandalism by ${e.user}, already cleaned up. Click to see it.`, tag: `vt-${e.revid}`,
      });
      note.onclick = () => { window.focus(); state.view = "watchlist"; changed(); };
    }
  }
}

function renderNotifyButton() {
  const button = $("#notify-button");
  if (!("Notification" in window)) {
    button.textContent = "🔕 Notifications not supported";
    button.disabled = true;
  } else if (Notification.permission === "granted") {
    button.textContent = "🔔 Notifications on";
    button.disabled = true;
  } else if (Notification.permission === "denied") {
    button.textContent = "🔕 Blocked in your browser settings";
    button.disabled = true;
  } else {
    button.textContent = "🔔 Turn on desktop notifications";
    button.disabled = false;
  }
}

function renderWatchPages(pages) {
  $("#watch-pages").innerHTML = pages.length ? pages.map((p) =>
    `<span class="watch-chip"><a href="${esc(wikiUrl(p.title))}" target="_blank" rel="noopener">${esc(p.title)}</a>` +
    `<span class="n">${fmt(p.vandal_edits)}</span>` +
    `<button class="icon-btn" data-history-page="${esc(p.title)}" title="Search this page's history">🔍</button>` +
    `<button class="icon-btn" data-unwatch="${p.pageid}" title="Stop watching">✕</button></span>`).join("")
    : `<span class="muted">You're not watching any pages yet.</span>`;
}

function showWatchMessage(text) {
  const box = $("#watch-status");
  box.hidden = !text;
  box.querySelector(".history-status-text").innerHTML = text || "";
}

// ---------------------------------------------------------------- page history

const RUNNING = ["queued", "running"];
let historyError = null;   // e.g. "There's no page called …", shown until the next search

function renderHistoryControls(data) {
  const depth = $("#history-depth");
  if (!depth.options.length) {
    depth.innerHTML = data.depths.map((n) => `<option value="${n}">Last ${fmt(n)} edits</option>`).join("");
  }
  depth.value = String(state.depth);
  $("#history-recent").innerHTML = data.searches.length ? `<span class="recent-label">Recent:</span>` +
    data.searches.map((s) => {
      const mark = RUNNING.includes(s.status) ? " …" : s.status === "error" ? " ⚠" : "";
      return `<button class="fchip" data-search="${s.id}" aria-pressed="${s.id === state.historyId}">` +
        `${esc(s.title || s.query)}${mark}</button>`;
    }).join("") : "";
}

function renderHistoryStatus(search, total) {
  const box = $("#history-status");
  box.hidden = !search;
  if (!search) return;
  const name = search.title || search.query;
  const bar = box.querySelector(".bar");
  let text;
  if (search.status === "error") {
    text = `<b class="error">${esc(search.error || "The search failed.")}</b>`;
  } else if (RUNNING.includes(search.status)) {
    text = `<b>${esc(name)}</b> — ${esc(search.phase || "Starting")}…`;
  } else {
    const range = search.revisions ? ` (${day(search.oldest)} – ${day(search.newest)})` : "";
    text = `<b>${esc(name)}</b>: <b>${fmt(total)}</b> confirmed vandal edit${total === 1 ? "" : "s"} ` +
      `in the last ${fmt(search.revisions)} edits${range} · ${fmt(search.reverted)} of those edits were reverted`;
  }
  box.querySelector(".history-status-text").innerHTML = text;
  bar.hidden = !RUNNING.includes(search.status) || !search.to_check;
  const done = search.to_check ? Math.min(100, Math.round(100 * search.checked / search.to_check)) : 0;
  bar.querySelector("span").style.width = `${done}%`;
}

async function searchHistoryOf(page) {
  historyError = null;
  state.historyId = (await postJSON("/api/history", {page, depth: state.depth})).id;
  state.view = "history";
  changed();
}

// ---------------------------------------------------------------- stats

function tile(label, value, note) {
  return `<div class="tile"><span class="tile-label">${esc(label)}</span><span class="tile-value">${value}</span>` +
    `${note ? `<span class="tile-note">${esc(note)}</span>` : ""}</div>`;
}

// A single-series column chart: [{label, value, tip}], `tick` = which labels to print under the axis.
function columns(title, subtitle, data, tick = () => true) {
  const max = Math.max(1, ...data.map((d) => d.value));
  const cols = data.map((d, i) =>
    `<div class="col${d.revert != null ? " clickable" : ""}" data-tip="${esc(d.tip)}" aria-label="${esc(d.tip)}" tabindex="0"` +
    `${d.revert != null ? ` data-revert="${esc(d.revert)}" role="button"` : ""}>` +
    `<span class="col-bar" style="height:${d.value ? Math.max(2, (100 * d.value) / max) : 0}%"></span></div>`).join("");
  const labels = data.map((d, i) => `<span>${tick(i) ? esc(d.label) : ""}</span>`).join("");
  const rows = data.map((d) => `<tr><td>${esc(d.label)}</td><td>${fmt(d.value)}</td></tr>`).join("");
  return `<figure class="chart"><figcaption><b>${esc(title)}</b><span>${esc(subtitle)}</span></figcaption>` +
    `<div class="plot"><span class="plot-max">${fmt(max)}</span><div class="cols">${cols}</div></div>` +
    `<div class="col-labels">${labels}</div>` +
    `<details class="as-table"><summary>Show as table</summary><table>${rows}</table></details></figure>`;
}

// Horizontal bars with the label and value as text beside each bar.
function barList(title, rows, empty) {
  const max = Math.max(1, ...rows.map((r) => r.count));
  const body = rows.length ? rows.map((r) =>
    `<li data-tip="${esc(`${r.name}: ${fmt(r.count)}`)}"><span class="bl-name">${r.html || esc(r.name)}</span>` +
    `<span class="bl-track"><span class="bl-bar" style="width:${Math.max(2, (100 * r.count) / max)}%"></span></span>` +
    `<span class="bl-value">${fmt(r.count)}</span></li>`).join("") : `<li class="muted">${esc(empty)}</li>`;
  return `<figure class="chart"><figcaption><b>${esc(title)}</b></figcaption><ul class="barlist">${body}</ul></figure>`;
}

function renderStats(d) {
  $("#stats-range").innerHTML = STATS_RANGES.map(([n, label]) =>
    `<button class="tab" data-stats-days="${n}" aria-selected="${state.statsDays === n}">${esc(label)}</button>`).join("");
  if (!d.total) {
    $("#stats-body").innerHTML = `<div class="empty"><b>No confirmed vandalism in this period yet</b>` +
      `Stats appear once the tracker has confirmed some vandalism.</div>`;
    return;
  }
  const r = d.revert;
  const pageLink = (p) => `<a href="${esc(wikiUrl(p.title))}" target="_blank" rel="noopener">${esc(p.title)}</a>`;
  const userButton = (u) => `<button class="linkish" data-user="${esc(u)}">${esc(u)}</button>`;
  const hours = d.per_hour.map((n, h) => ({label: String(h).padStart(2, "0"), value: n,
    tip: `${String(h).padStart(2, "0")}:00–${String(h).padStart(2, "0")}:59 · ${fmt(n)} vandal edits`}));
  const days = d.per_day.map((x) => ({label: new Date(x.day).toLocaleDateString(undefined, {day: "numeric", month: "short"}),
    value: x.count, tip: `${day(x.day)} · ${fmt(x.count)} vandal edits`}));
  const buckets = r.buckets.map((b) => ({label: b.label, value: b.count, revert: b.range,
    tip: `Reverted ${b.label}: ${fmt(b.count)} · click to see them`}));
  const longest = r.longest.length ? r.longest.map((e) =>
    `<li><a href="https://en.wikipedia.org/w/index.php?oldid=${e.revid}" target="_blank" rel="noopener">${esc(e.title)}</a>` +
    ` <span class="muted">by ${esc(e.user)} · stayed up ${esc(duration(e.seconds))}</span></li>`).join("") : "<li class='muted'>No revert times known yet.</li>";
  const offenderRows = d.repeat_offenders.map((o) =>
    `<tr><td>${userButton(o.user)}</td><td class="num">${fmt(o.edits)}</td>` +
    `<td>${o.pages.map((t) => esc(t)).join(", ")}${o.page_count > 3 ? ` +${o.page_count - 3} more` : ""}</td>` +
    `<td>${o.block ? `<span class="chip hi">Blocked</span> <span class="muted">${esc(short(plain(o.block), 60))}</span>` : "<span class='muted'>no</span>"}</td>` +
    `<td class="muted">${esc(ago(o.last))}</td></tr>`).join("");

  $("#stats-body").innerHTML = `
    <div class="tiles">
      ${tile("Confirmed vandal edits", fmt(d.total), `last ${d.days === 1 ? "24 hours" : `${d.days} days`}`)}
      ${tile("Median time to revert", esc(duration(r.median)), `from ${fmt(r.known)} reverts with a known time`)}
      ${tile("Reverted within a minute", pct(r.within_minute), r.fastest != null ? `fastest: ${duration(r.fastest)}` : "")}
      ${tile("Reverted by bots", pct(d.bot_share), "ClueBot NG and friends")}
      ${tile("Repeat offenders", fmt(d.repeat_offender_count), "editors with 2+ vandal edits")}
    </div>
    <div class="chart-grid">
      ${columns("When vandalism happens", "Vandal edits per hour of the day, in your local time", hours, (i) => i % 3 === 0)}
      ${columns("Per day", "Vandal edits per day", days, (i) => days.length <= 7 || i % 2 === 0)}
      ${columns("Time to revert", "How long vandalism stayed up before it was reverted · click a bar to see those edits", buckets)}
      ${barList("Who cleaned it up", d.cleaned_by.map((c) => ({name: c.name, count: c.count})), "Nothing yet")}
      ${barList("Most vandalized pages", d.top_pages.map((p) => ({name: p.title, count: p.count, html: pageLink(p)})), "Nothing yet")}
      ${barList("Categories", d.top_categories, "Nothing categorised yet")}
      ${barList("Top reverters", d.top_reverters.map((u) => ({name: u.user, count: u.count})), "No reverters known yet")}
      <figure class="chart"><figcaption><b>Longest-surviving vandalism</b><span>Before someone reverted it</span></figcaption>
        <ul class="plain-list">${longest}</ul>
        <button class="linkish see-all" data-revert="3600-">See all vandalism that stayed up over an hour →</button></figure>
    </div>
    <figure class="chart wide"><figcaption><b>Repeat offenders</b><span>Editors with more than one confirmed vandal edit in this period · click one to see their edits</span></figcaption>
      ${offenderRows ? `<table class="offenders"><thead><tr><th>Editor</th><th class="num">Vandal edits</th><th>Pages</th><th>Blocked?</th><th>Last</th></tr></thead><tbody>${offenderRows}</tbody></table>`
        : `<p class="muted">Nobody has more than one confirmed vandal edit in this period.</p>`}
    </figure>`;
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
  let statusText = e.status === "reverted" && community.cleaned ? "Cleaned up" : (STATUS_LABEL[e.status] || e.status);
  if (e.revert_seconds != null) statusText = `Reverted after ${duration(e.revert_seconds)}`;
  const cats = (e.categories || []).map((c) => `<span class="cat">${esc(c)}</span>`).join("");
  const byline = [`by <a href="${esc(e.links.user)}" target="_blank" rel="noopener">${esc(e.user)}</a>`];
  const repeat = offenders[e.user];
  if (repeat && state.user !== e.user) {
    byline[0] += ` <button class="offender" data-user="${esc(e.user)}" title="Repeat offender: show all their vandal edits">⚠ ${fmt(repeat)} vandal edits</button>`;
  }
  if (e.edit_type === "new") byline.push("<b>created a new page</b>");
  if (e.size_change) {
    byline.push(`<span class="${e.size_change < 0 ? "size-minus" : "size-plus"}">${e.size_change > 0 ? "+" : "−"}` +
      `${fmt(Math.abs(e.size_change))} bytes</span>`);
  }
  byline.push(e.comment ? `<span class="summary">“${esc(short(plain(e.comment), 200))}”</span>`
    : `<span class="summary">no edit summary</span>`);
  const watched = watchedPages.has(e.pageid);
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
        <button class="btn" data-action="watch" data-pageid="${e.pageid}" data-title="${esc(e.title)}" aria-pressed="${watched}">
          ${watched ? "👁 Watching" : "👁 Watch page"}</button>
        ${e.source === "history" ? "" : `<button class="btn" data-action="recheck">↻ Re-check now</button>`}
        ${checkedHtml(e)}
      </div>
    </article>`;
}

function emptyHtml(search) {
  if (state.view === "history") {
    if (!search) {
      return `<div class="empty"><b>Search a page's history</b>Type a page name above, or paste a link to any Wikipedia page, to find the vandalism in its past.</div>`;
    }
    if (RUNNING.includes(search.status)) {
      return `<div class="empty"><b>Searching…</b>Confirmed vandalism shows up here as soon as it's found.</div>`;
    }
    if (search.status === "error") return "";
    return `<div class="empty"><b>No confirmed vandalism found</b>Nothing in the last ${fmt(search.revisions)} edits passed both checks. Try looking further back.</div>`;
  }
  if (state.view === "watchlist") {
    if (!watchedPages.size) {
      return `<div class="empty"><b>Watch your favourite pages</b>Add a page above, or click “👁 Watch page” on any vandalism. Confirmed vandalism on those pages will show up here.</div>`;
    }
    return `<div class="empty"><b>All quiet on your watched pages</b>No confirmed vandalism yet. Tip: click 🔍 next to a page to search its history.</div>`;
  }
  if (state.view === "favorites") {
    return `<div class="empty"><b>No favourites yet</b>Click the ☆ next to any vandalism to keep it here forever.</div>`;
  }
  if (status && status.stats.scanned === 0) {
    return `<div class="empty"><b>Warming up…</b>The tracker is reading the latest edits. The first results usually show up within a few minutes.</div>`;
  }
  if (state.category || state.q || state.user || state.revert) {
    return `<div class="empty"><b>Nothing matches</b>No confirmed vandalism matches these filters.</div>`;
  }
  return `<div class="empty"><b>Nothing confirmed yet</b>Vandalism shows up here once it's confirmed and Wikipedia has cleaned it up. The tracker keeps re-checking every suspicious edit in the background.</div>`;
}

// Everything that changes a card's look, except the "checked N× · last …" line (updated in place).
function cardSignature(e) {
  return JSON.stringify([e.verdict, e.status, e.favorite, e.categories, e.reasons, e.findings, e.revert_risk,
    e.stage === "new", e.diff.hunks.length, e.primary.url, offenders[e.user] || 0, watchedPages.has(e.pageid),
    state.user]);
}

function renderList(data, search) {
  const list = $("#list");
  offenders = data.offenders || {};
  $("#more").hidden = data.items.length >= data.total;
  $("#count").textContent = data.total ? `Showing ${fmt(data.items.length)} of ${fmt(data.total)}` : "";
  if (!data.items.length) {
    const html = emptyHtml(search);
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

let refreshAgain = false;   // something changed while a refresh was running

async function refresh() {
  if (busy) {
    refreshAgain = true;
    return;
  }
  busy = true;
  refreshAgain = false;
  let again = false;
  try {
    if (state.view === "history") {
      again = await refreshHistory();
    } else if (state.view === "stats") {
      const tz = -new Date().getTimezoneOffset();
      const [s, d] = await Promise.all([getJSON("/api/status"), getJSON(`/api/stats?days=${state.statsDays}&tz=${tz}`)]);
      renderStatus(s);
      renderStats(d);
    } else {
      const params = new URLSearchParams({view: state.view, sort: state.sort, limit});
      if (state.category) params.set("category", state.category);
      if (state.q) params.set("q", state.q);
      if (state.user) params.set("user", state.user);
      if (state.revert) params.set("revert", state.revert);
      const watch = state.view === "watchlist" ? getJSON("/api/watchlist") : Promise.resolve(null);
      const [s, data, pages] = await Promise.all([getJSON("/api/status"), getJSON(`/api/edits?${params}`), watch]);
      renderStatus(s);
      if (pages) renderWatchPages(pages.pages);
      renderCategories(data.category_counts, data.total_all);
      renderList(data);
    }
  } catch (err) {
    setPill("bad", "Can't reach the tracker — is main.py still running?");
  } finally {
    busy = false;
  }
  if (refreshAgain) refresh();
  else if (again) setTimeout(refresh, 1500);   // a search is running: follow it closely
}

async function refreshHistory() {
  const results = state.historyId
    ? fetch(`/api/history/${state.historyId}?limit=${limit}`, {cache: "no-store"}) : Promise.resolve(null);
  const [s, recent, resp] = await Promise.all([getJSON("/api/status"), getJSON("/api/history"), results]);
  renderStatus(s);
  let data = {items: [], total: 0, search: null};
  if (resp && resp.status === 404) {
    state.historyId = null;   // that search was cleaned up
    saveState();
  } else if (resp) {
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    data = await resp.json();
  }
  renderHistoryControls(recent);
  renderHistoryStatus(historyError ? {status: "error", error: historyError} : data.search, data.total);
  renderList(data, data.search);
  return Boolean(data.search && RUNNING.includes(data.search.status));
}

// ---------------------------------------------------------------- events

document.addEventListener("click", async (ev) => {
  const target = ev.target;
  const view = target.closest("[data-view]");
  if (view) {
    state.view = view.dataset.view;
    return changed();
  }
  const statsDays = target.closest("[data-stats-days]");
  if (statsDays) {
    state.statsDays = Number(statsDays.dataset.statsDays);
    return changed();
  }
  const userLink = target.closest("[data-user]");
  if (userLink) {   // repeat offender: show all their vandal edits (or "show everyone" when empty)
    state.user = userLink.dataset.user;
    if (state.user) {
      state.view = "all";
      state.category = "";
      state.q = "";
    }
    window.scrollTo({top: 0, behavior: "smooth"});
    return changed();
  }
  const revertChip = target.closest("[data-revert]");
  if (revertChip) {   // a time-to-revert chip, or a bar in the Stats "Time to revert" chart
    state.revert = revertChip.dataset.revert;
    $("#revert-custom").hidden = true;
    if (!FEED_VIEWS.includes(state.view)) {
      state.view = "all";
      state.sort = "slowest";
      window.scrollTo({top: 0, behavior: "smooth"});
    }
    return changed();
  }
  if (target.closest("[data-revert-custom]")) {
    const form = $("#revert-custom");
    form.hidden = !form.hidden;
    if (!form.hidden) $("#revert-from").focus();
    return;
  }
  const categoryChip = target.closest("[data-category]");
  if (categoryChip) {
    state.category = categoryChip.dataset.category;
    return changed();
  }
  const searchChip = target.closest("[data-search]");
  if (searchChip) {
    state.historyId = Number(searchChip.dataset.search);
    historyError = null;
    return changed();
  }
  const historyPage = target.closest("[data-history-page]");
  if (historyPage) {
    try { await searchHistoryOf(historyPage.dataset.historyPage); } catch (err) { showWatchMessage(esc(err.message)); }
    return;
  }
  const unwatch = target.closest("[data-unwatch]");
  if (unwatch) {
    await sendJSON(`/api/watchlist/${unwatch.dataset.unwatch}`, "DELETE").catch(() => null);
    return refresh();
  }
  const action = target.closest("[data-action]");
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
  } else if (action.dataset.action === "watch") {
    const pageid = Number(action.dataset.pageid);
    action.disabled = true;
    try {
      if (watchedPages.has(pageid)) {
        await sendJSON(`/api/watchlist/${pageid}`, "DELETE");
        watchedPages.delete(pageid);
      } else {
        await postJSON("/api/watchlist", {pageid, title: action.dataset.title});
        watchedPages.add(pageid);
      }
    } catch { /* shown as unchanged */ }
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

// Chart tooltips: anything with data-tip, on hover or keyboard focus.
const tooltip = $("#tooltip");
function showTip(el, x, y) {
  tooltip.textContent = el.dataset.tip;
  tooltip.hidden = false;
  const box = tooltip.getBoundingClientRect();
  tooltip.style.left = `${Math.min(window.innerWidth - box.width - 8, Math.max(8, x - box.width / 2))}px`;
  tooltip.style.top = `${Math.max(8, y - box.height - 12)}px`;
}
document.addEventListener("mousemove", (ev) => {
  const el = ev.target.closest("[data-tip]");
  if (el) showTip(el, ev.clientX, ev.clientY); else tooltip.hidden = true;
});
document.addEventListener("focusin", (ev) => {
  const el = ev.target.closest("[data-tip]");
  if (el) {
    const r = el.getBoundingClientRect();
    showTip(el, r.left + r.width / 2, r.top);
  }
});
document.addEventListener("focusout", () => { tooltip.hidden = true; });

let searchTimer = null;
$("#search").addEventListener("input", (ev) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { state.q = ev.target.value.trim(); changed(); }, 300);
});
$("#sort").addEventListener("change", (ev) => { state.sort = ev.target.value; changed(); });
$("#more").addEventListener("click", () => { limit += PAGE_SIZE; lastSignature = ""; refresh(); });
$("#history-depth").addEventListener("change", (ev) => { state.depth = Number(ev.target.value); saveState(); });
$("#history-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const input = $("#history-page");
  const button = ev.target.querySelector("button");
  button.disabled = true;
  try {
    await searchHistoryOf(input.value);
    input.value = "";
  } catch (err) {
    historyError = err.message;
    renderHistoryStatus({status: "error", error: historyError}, 0);
  } finally {
    button.disabled = false;
  }
});
$("#history-page").addEventListener("input", () => { historyError = null; });
$("#watch-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const input = $("#watch-page");
  const button = ev.target.querySelector("button[type=submit]");
  button.disabled = true;
  try {
    const page = await postJSON("/api/watchlist", {page: input.value});
    input.value = "";
    showWatchMessage(`Now watching <b>${esc(page.title)}</b>. <button class="linkish" data-history-page="${esc(page.title)}">Search its history for past vandalism</button>`);
    refresh();
  } catch (err) {
    showWatchMessage(`<b class="error">${esc(err.message)}</b>`);
  } finally {
    button.disabled = false;
  }
});
$("#revert-custom").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const from = parseFloat($("#revert-from").value);
  const to = parseFloat($("#revert-to").value);
  const low = Number.isFinite(from) && from > 0 ? Math.round(from * 60) : 0;
  const high = Number.isFinite(to) && to > 0 ? Math.round(to * 60) : "";
  state.revert = low || high !== "" ? `${low}-${high}` : "";
  ev.target.hidden = true;
  changed();
});
$("#notify-button").addEventListener("click", async () => {
  if ("Notification" in window) await Notification.requestPermission();
  renderNotifyButton();
});

renderControls();
refresh();
setInterval(refresh, REFRESH_MS);
