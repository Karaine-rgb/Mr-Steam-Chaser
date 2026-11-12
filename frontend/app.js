/* Sharp Odds frontend (Phase 3: credit widget, rounds list, Discovery).
   Plain JS, no build step. Every value from the API is escaped before it
   reaches innerHTML. */
"use strict";

const API = "/api";
const $ = (id) => document.getElementById(id);

async function api(path, options = {}) {
  const res = await fetch(`${API}${path}`, { headers: { "Content-Type": "application/json" }, ...options });
  const ct = res.headers.get("content-type") || "";
  const body = ct.includes("application/json") ? await res.json() : await res.text();
  if (!res.ok) {
    const msg = body && body.detail ? (typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail)) : String(body);
    throw new Error(msg);
  }
  return body;
}

function esc(v) {
  return String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

const store = {
  get(key, fallback) {
    try { const v = localStorage.getItem(key); return v ? JSON.parse(v) : fallback; } catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* private mode etc. */ }
  },
};

const fmtWhen = (iso) => new Date(iso).toLocaleString([], { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
const fmtDay = (iso) => new Date(iso).toLocaleDateString([], { weekday: "long", day: "numeric", month: "long" });
const fmtTime = (iso) => new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const dayKey = (d) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;

function modal(id, show) { $(id).classList.toggle("hidden", !show); }

// ---------------------------------------------------------------------------
// Credit widget
// ---------------------------------------------------------------------------
let lastCredits = null;

function renderCredits(data) {
  lastCredits = data;
  const b = data.budget;
  const pill = $("credit-pill");
  pill.textContent = `credits ${b.used}/${b.limit}`;
  pill.classList.toggle("paused", b.paused);
  pill.classList.toggle("warn", !b.paused && b.used >= b.auto_line * 0.8);
  $("mock-pill").classList.toggle("hidden", !data.mock);

  const banner = $("credit-banner");
  if (b.paused) {
    banner.className = "credit-banner";
    banner.textContent = b.exhausted
      ? "Paid calls are paused: the Odds API reported this month's credits are used up. They resume when the API resets (the 1st of the month)."
      : `Paid calls are paused: ${b.used} of ${b.limit} credits used (pause line ${b.pause_line}). They resume next month or when MONTHLY_CREDIT_LIMIT is raised.`;
  } else if (b.api_quota_mismatch) {
    banner.className = "credit-banner warn";
    banner.textContent = `The API reports a quota of ${b.api_used + b.api_remaining} credits, but MONTHLY_CREDIT_LIMIT is ${b.limit}. Check the setting.`;
  } else {
    banner.className = "credit-banner hidden";
  }

  const pct = (n) => `${Math.max(0, Math.min(100, (n / b.limit) * 100))}%`;
  const used = $("credit-meter-used");
  used.style.width = pct(b.used);
  used.className = "credit-meter-used" + (b.paused ? " paused" : b.used >= b.auto_line * 0.8 ? " warn" : "");
  $("credit-meter-reserve").style.width = pct(b.limit - b.auto_line);

  const stat = (label, value) => `<div class="stat"><b>${esc(value)}</b><span>${esc(label)}</span></div>`;
  $("credit-stats").innerHTML = [
    stat("used this month", b.used),
    stat("left for auto polling", b.auto_left),
    stat("reserve (never auto)", b.limit - b.auto_line),
    stat("projected month end", b.projected_month_end),
    stat("today / soft cap", `${b.spent_today} / ${b.soft_cap_today}`),
    stat("days left", Math.round(b.days_left)),
  ].join("");
  const notes = [];
  if (b.uncertain) notes.push(`${b.uncertain} credit(s) are counted at their expected cost because a call ended without a clear answer; the next API reply settles it.`);
  notes.push(b.api_synced_at ? `API counter last read ${fmtWhen(b.api_synced_at)}: ${b.api_used} used, ${b.api_remaining} remaining.` : "The API's own counter has not been read this month yet.");
  notes.push("Months are counted in UTC; the API's reset time zone is not documented.");
  if (data.mock) notes.push("Mock mode: these are pretend credits.");
  $("credit-notes").textContent = notes.join(" ");

  const calls = data.recent_paid_calls || [];
  $("credit-calls").innerHTML = calls.length
    ? calls.map((c) => `<div class="call-row ${c.status === "ok" || c.status === "empty" ? "" : "bad"}">
        <span class="when">${esc(fmtWhen(c.created_at))}</span>
        <span>${esc(c.sport_key || c.endpoint)} · ${esc(c.kind || "")} · ${esc(c.status)}</span>
        <span class="cost">${c.charged == null ? `~${esc(c.expected_cost)}` : esc(c.charged)} cr</span></div>`).join("")
    : `<div class="dim small">No paid calls yet.</div>`;
}

async function loadCredits() {
  try { renderCredits(await api("/credits")); } catch (e) { $("credit-pill").textContent = "credits ?"; }
}

$("credit-pill").onclick = () => { modal("modal-credits", true); loadCredits(); };
$("btn-credits-close").onclick = () => modal("modal-credits", false);
$("btn-credit-sync").onclick = async () => {
  const btn = $("btn-credit-sync");
  btn.disabled = true;
  try { await api("/credits/sync", { method: "POST" }); await loadCredits(); } catch (e) { alert(`Re-sync failed: ${e.message}`); }
  btn.disabled = false;
};

// ---------------------------------------------------------------------------
// Rounds list
// ---------------------------------------------------------------------------
let rounds = [];

async function loadRounds() {
  try { rounds = await api("/rounds"); } catch { rounds = []; }
  const list = $("rounds-list");
  list.innerHTML = rounds.length
    ? rounds.map((r) => `<div class="round-card">
        <h4>${esc(r.round_label)}</h4>
        <div class="meta">${esc(r.matches)} matches (${esc(r.monitored)} tracked) · ${esc(fmtWhen(r.first_kickoff))} → ${esc(fmtWhen(r.last_kickoff))}</div>
        <div class="meta">${esc((r.bookmakers || []).length)} bookmakers · ${esc((r.markets || []).join(", "))} · ${esc(r.schedule_preset || "")}</div>
      </div>`).join("")
    : `<div class="dim">No rounds yet. Open Discovery to pick matches.</div>`;
  $("round-names").innerHTML = rounds.map((r) => `<option value="${esc(r.round_label)}"></option>`).join("");
}

// ---------------------------------------------------------------------------
// Discovery
// ---------------------------------------------------------------------------
const D = {
  config: null,
  leagues: [],
  selectedLeagues: new Set(store.get("so.leagues", [])),
  events: [],
  selectedEvents: new Map(),
  books: [],
  selectedBooks: null,
  markets: new Set(["h2h"]),
  estimateSeq: 0,
};

$("btn-discovery").onclick = openDiscovery;
$("btn-discovery-close").onclick = () => modal("modal-discovery", false);

async function openDiscovery() {
  modal("modal-discovery", true);
  $("add-status").textContent = "";
  if (!D.config) {
    D.config = await api("/config");
    $("preset").value = D.config.default_preset;
  }
  if (!D.selectedBooks) D.selectedBooks = new Set(store.get("so.books", D.config.default_bookmakers));
  if (!$("date-from").value) {
    const now = new Date();
    $("date-from").value = dayKey(now);
    $("date-to").value = dayKey(new Date(now.getTime() + 14 * 86400000));
  }
  await Promise.all([loadLeagues(false), loadBooks(), loadRounds()]);
  await loadEvents(false);
}

// ---- leagues ---------------------------------------------------------------
async function loadLeagues(refresh) {
  const group = $("league-groups");
  try {
    const q = `?inactive=${$("league-inactive").checked}&refresh=${refresh}`;
    D.leagues = (await api(`/discovery/leagues${q}`)).leagues;
  } catch (e) {
    group.innerHTML = `<div class="dim pad">Could not load leagues: ${esc(e.message)}</div>`;
    return;
  }
  const known = new Set(D.leagues.map((l) => l.key));
  for (const k of [...D.selectedLeagues]) if (!known.has(k)) D.selectedLeagues.delete(k);
  renderLeagues();
}

function renderLeagues() {
  const q = $("league-search").value.trim().toLowerCase();
  const shown = D.leagues.filter((l) => !q || `${l.title} ${l.country} ${l.description}`.toLowerCase().includes(q));
  const byCountry = new Map();
  for (const l of shown) {
    if (!byCountry.has(l.country)) byCountry.set(l.country, []);
    byCountry.get(l.country).push(l);
  }
  const openAll = q.length > 0;
  $("league-groups").innerHTML = shown.length
    ? [...byCountry.entries()].map(([country, ls]) => {
        const ticked = ls.filter((l) => D.selectedLeagues.has(l.key)).length;
        return `<details class="group" ${openAll || ticked ? "open" : ""}>
          <summary>${esc(country)}<span class="n">${ls.length}${ticked ? ` · ${ticked} ticked` : ""}</span></summary>
          ${ls.map((l) => `<label class="item"><input type="checkbox" data-league="${esc(l.key)}" ${D.selectedLeagues.has(l.key) ? "checked" : ""} />
            <span class="txt">${esc(l.title)}${l.active ? "" : '<span class="tag">out of season</span>'}</span></label>`).join("")}
        </details>`;
      }).join("")
    : `<div class="dim pad">No league matches "${esc(q)}".</div>`;
  $("popular-chips").innerHTML = D.leagues.filter((l) => l.popular).map((l) =>
    `<button class="chip" data-league="${esc(l.key)}" aria-pressed="${D.selectedLeagues.has(l.key)}">${esc(l.title)}</button>`).join("");
  $("league-count").textContent = `${D.selectedLeagues.size} ticked`;
}

function toggleLeague(key, on) {
  if (on) D.selectedLeagues.add(key); else D.selectedLeagues.delete(key);
  store.set("so.leagues", [...D.selectedLeagues]);
  renderLeagues();
  loadEventsSoon();
}

$("league-groups").addEventListener("change", (e) => {
  const key = e.target.dataset.league;
  if (key) toggleLeague(key, e.target.checked);
});
$("popular-chips").addEventListener("click", (e) => {
  const key = e.target.dataset.league;
  if (key) toggleLeague(key, !D.selectedLeagues.has(key));
});
$("league-search").addEventListener("input", renderLeagues);
$("league-inactive").addEventListener("change", () => loadLeagues(false));
$("btn-league-refresh").onclick = () => loadLeagues(true);

// ---- matches ---------------------------------------------------------------
let eventsTimer = null;
function loadEventsSoon() { clearTimeout(eventsTimer); eventsTimer = setTimeout(() => loadEvents(false), 250); }

async function loadEvents(refresh) {
  const list = $("match-list");
  const keys = [...D.selectedLeagues];
  if (!keys.length) {
    D.events = [];
    list.innerHTML = `<div class="dim pad">Tick a league to list its upcoming matches.</div>`;
    $("events-age").textContent = "";
    renderMatchCount();
    return;
  }
  list.setAttribute("aria-busy", "true");
  try {
    const data = await api(`/discovery/events?sports=${encodeURIComponent(keys.join(","))}&refresh=${refresh}`);
    D.events = data.events;
    $("events-age").textContent = data.max_age_s > 60 ? `list from ${Math.round(data.max_age_s / 60)} min ago` : "fresh list";
  } catch (e) {
    list.innerHTML = `<div class="dim pad">Could not load matches: ${esc(e.message)}</div>`;
    return;
  } finally {
    list.removeAttribute("aria-busy");
  }
  // keep only selections that are still listed
  const ids = new Set(D.events.map((e) => e.event_id));
  for (const id of [...D.selectedEvents.keys()]) if (!ids.has(id)) D.selectedEvents.delete(id);
  renderMatches();
}

function visibleEvents() {
  const q = $("team-search").value.trim().toLowerCase();
  const from = $("date-from").value ? new Date(`${$("date-from").value}T00:00:00`) : null;
  const to = $("date-to").value ? new Date(`${$("date-to").value}T23:59:59`) : null;
  return D.events.filter((e) => {
    const t = new Date(e.commence_time);
    if (from && t < from) return false;
    if (to && t > to) return false;
    return !q || `${e.home_team} ${e.away_team}`.toLowerCase().includes(q);
  });
}

function renderMatches() {
  const shown = visibleEvents();
  const list = $("match-list");
  if (!shown.length) {
    list.innerHTML = `<div class="dim pad">${D.events.length ? "No match fits the filters." : "No upcoming matches listed for the ticked leagues."}</div>`;
    renderMatchCount();
    return;
  }
  let html = "";
  let lastDay = "";
  for (const e of shown) {
    const d = dayKey(new Date(e.commence_time));
    if (d !== lastDay) { html += `<div class="day-head">${esc(fmtDay(e.commence_time))}</div>`; lastDay = d; }
    const taken = e.tracked_in;
    html += `<label class="item ${taken ? "disabled" : ""}">
      <input type="checkbox" data-event="${esc(e.event_id)}" ${D.selectedEvents.has(e.event_id) ? "checked" : ""} ${taken ? "disabled" : ""} />
      <span class="txt">${esc(e.home_team)} v ${esc(e.away_team)}${taken ? `<span class="tag tag-in">in ${esc(taken)}</span>` : ""}
        <span class="match-league">${esc(e.league_title)}</span></span>
      <span class="aside">${esc(fmtTime(e.commence_time))}</span></label>`;
  }
  list.innerHTML = html;
  renderMatchCount();
}

function renderMatchCount() {
  $("match-count").textContent = `${D.selectedEvents.size} selected`;
  $("btn-add-round").disabled = D.selectedEvents.size === 0;
  estimateSoon();
}

$("match-list").addEventListener("change", (e) => {
  const id = e.target.dataset.event;
  if (!id) return;
  const ev = D.events.find((x) => x.event_id === id);
  if (e.target.checked && ev) D.selectedEvents.set(id, { event_id: id, sport_key: ev.sport_key });
  else D.selectedEvents.delete(id);
  renderMatchCount();
});
for (const id of ["team-search", "date-from", "date-to"]) $(id).addEventListener("input", renderMatches);
$("btn-select-shown").onclick = () => {
  for (const e of visibleEvents()) if (!e.tracked_in) D.selectedEvents.set(e.event_id, { event_id: e.event_id, sport_key: e.sport_key });
  renderMatches();
};
$("btn-clear-matches").onclick = () => { D.selectedEvents.clear(); renderMatches(); };

// ---- bookmakers and markets -----------------------------------------------
async function loadBooks() {
  if (D.books.length) { renderBooks(); return; }
  try { D.books = (await api("/discovery/bookmakers")).bookmakers; } catch (e) {
    $("book-groups").innerHTML = `<div class="dim pad">Could not load bookmakers: ${esc(e.message)}</div>`;
    return;
  }
  const valid = new Set(D.books.filter((b) => !b.paid_only).map((b) => b.key));
  for (const k of [...D.selectedBooks]) if (!valid.has(k)) D.selectedBooks.delete(k);
  renderBooks();
}

const REGION_NAMES = { eu: "Europe", uk: "United Kingdom", us: "USA", us2: "USA (2)", us_ex: "US exchanges", au: "Australia", ca: "Canada", fr: "France", se: "Sweden", fi: "Finland" };
const REGION_ORDER = ["eu", "uk", "fr", "se", "fi", "us", "us2", "us_ex", "au", "ca"];

function renderBooks() {
  const q = $("book-search").value.trim().toLowerCase();
  const byRegion = new Map(REGION_ORDER.map((r) => [r, []]));
  for (const b of D.books) {
    if (q && !`${b.title} ${b.key}`.toLowerCase().includes(q)) continue;
    const r = b.regions[0];
    if (!byRegion.has(r)) byRegion.set(r, []);
    byRegion.get(r).push(b);
  }
  $("book-groups").innerHTML = [...byRegion.entries()].filter(([, bs]) => bs.length).map(([r, bs]) => {
    const ticked = bs.filter((b) => D.selectedBooks.has(b.key)).length;
    return `<details class="group" ${ticked || q ? "open" : ""}>
      <summary>${esc(REGION_NAMES[r] || r)}<span class="n">${bs.length}${ticked ? ` · ${ticked} ticked` : ""}</span></summary>
      ${bs.map((b) => `<label class="item ${b.paid_only ? "disabled" : ""}" title="${esc(b.note || "")}">
        <input type="checkbox" data-book="${esc(b.key)}" ${D.selectedBooks.has(b.key) ? "checked" : ""} ${b.paid_only ? "disabled" : ""} />
        <span class="txt">${esc(b.title)}${b.exchange ? '<span class="tag tag-x">exchange</span>' : ""}${b.paid_only ? '<span class="tag tag-paid">paid plan</span>' : ""}</span>
        <span class="aside">${b.live_check_epl ? `EPL ${esc(b.live_check_epl)}` : ""}</span></label>`).join("")}
    </details>`;
  }).join("") || `<div class="dim pad">No bookmaker matches "${esc(q)}".</div>`;
  const n = D.selectedBooks.size;
  const pill = $("book-count");
  pill.textContent = `${n} ticked · ${Math.max(1, Math.ceil(n / 10))} credit${n > 10 ? "s" : ""} per market`;
  pill.className = `pill ${n > 10 ? "pill-err" : "pill-dim"}`;
}

$("book-groups").addEventListener("change", (e) => {
  const key = e.target.dataset.book;
  if (!key) return;
  if (e.target.checked) D.selectedBooks.add(key); else D.selectedBooks.delete(key);
  store.set("so.books", [...D.selectedBooks]);
  renderBooks();
  estimateSoon();
});
$("book-search").addEventListener("input", renderBooks);
$("btn-books-default").onclick = () => {
  D.selectedBooks = new Set(D.config.default_bookmakers);
  store.set("so.books", [...D.selectedBooks]);
  renderBooks();
  estimateSoon();
};
document.querySelectorAll("input.mk").forEach((el) => el.addEventListener("change", () => {
  if (el.checked) D.markets.add(el.value); else D.markets.delete(el.value);
  estimateSoon();
}));
$("preset").addEventListener("change", estimateSoon);

// ---- live cost estimate ----------------------------------------------------
let estimateTimer = null;
function estimateSoon() { clearTimeout(estimateTimer); estimateTimer = setTimeout(runEstimate, 300); }

function selection() {
  return {
    matches: [...D.selectedEvents.values()],
    bookmakers: [...(D.selectedBooks || [])],
    markets: ["h2h", ...[...D.markets].filter((m) => m !== "h2h")],
    preset: $("preset").value,
  };
}

async function runEstimate() {
  const box = $("cost-box");
  const sel = selection();
  if (!sel.matches.length) { box.innerHTML = `<span class="dim">Pick matches to see what they cost.</span>`; return; }
  if (!sel.bookmakers.length) { box.innerHTML = `<span class="bad">Pick at least one bookmaker.</span>`; return; }
  const seq = ++D.estimateSeq;
  let est;
  try { est = await api("/discovery/estimate", { method: "POST", body: JSON.stringify(sel) }); } catch (e) {
    if (seq === D.estimateSeq) box.innerHTML = `<span class="bad">${esc(e.message)}</span>`;
    return;
  }
  if (seq !== D.estimateSeq) return; // a newer estimate is on its way
  const b = est.budget;
  const lines = [];
  lines.push(`<b>${est.matches}</b> match${est.matches === 1 ? "" : "es"} in <b>${est.leagues}</b> league${est.leagues === 1 ? "" : "s"}${est.already_tracked ? ` (${est.already_tracked} already tracked)` : ""}. ` +
    `Each refresh costs <span class="big">${est.per_refresh}</span> credit${est.per_refresh === 1 ? "" : "s"} ` +
    `(${est.cost_per_league_refresh} per league × ${est.leagues}).`);
  lines.push(`This schedule adds about <b>${est.this_month}</b> credits this month${est.next_month ? ` and ${est.next_month} next month` : ""}. ` +
    `All tracked rounds together: ~${est.plan_total_this_month} this month, ~${est.per_day_now}/day; ` +
    `<b>${b.auto_left}</b> credits left for automatic polling` +
    (est.days_of_budget_left == null ? "." : est.days_of_budget_left >= b.days_left
      ? ", enough for the rest of the month at this rate."
      : ` — <span class="warn">only ≈${est.days_of_budget_left} days at this rate, ${Math.round(b.days_left)} days left in the month</span>.`));
  if (!est.fits) lines.push(`<span class="warn">Over budget: the planner would skip ${est.dropped_calls_if_added} planned refresh(es) (${est.dropped_snapshots_if_added} snapshots), far-from-kickoff ones first.</span>`);
  if (est.bookmakers > 10) lines.push(`<span class="warn">More than 10 bookmakers: every refresh costs ${Math.ceil(est.bookmakers / 10)}× as much.</span>`);
  if (b.paused) lines.push(`<span class="bad">Paid calls are paused right now; added matches will not be polled until credits are available.</span>`);
  lines.push(`<span class="dim">Adding spends nothing now. Odds are bought later by the poller, inside the budget.</span>`);
  box.innerHTML = lines.join("<br />");
}

// ---- add to round ----------------------------------------------------------
$("btn-add-round").onclick = async () => {
  const status = $("add-status");
  const label = $("round-label").value.trim();
  if (!label) { status.className = "small err"; status.textContent = "Give the round a name first."; $("round-label").focus(); return; }
  const btn = $("btn-add-round");
  btn.disabled = true;
  try {
    const res = await api("/rounds/add", { method: "POST", body: JSON.stringify({ ...selection(), round_label: label }) });
    status.className = "small ok";
    status.textContent = `Added ${res.added} match${res.added === 1 ? "" : "es"} to "${res.round_label}" (0 credits spent).`;
    D.selectedEvents.clear();
    await Promise.all([loadRounds(), loadEvents(false)]);
  } catch (e) {
    status.className = "small err";
    status.textContent = e.message;
  } finally {
    btn.disabled = D.selectedEvents.size === 0;
  }
};

document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  for (const id of ["modal-discovery", "modal-credits"]) modal(id, false);
});

// ---------------------------------------------------------------------------
loadCredits();
loadRounds();
setInterval(loadCredits, 60000);
