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
// Shared helpers
// ---------------------------------------------------------------------------
function askYesNo(title, text, yesLabel = "Yes") {
  return new Promise((resolve) => {
    const yes = $("confirm-yes"), no = $("confirm-no");
    $("confirm-title").textContent = title;
    $("confirm-text").textContent = text;
    yes.textContent = yesLabel;
    const done = (answer) => { modal("modal-confirm", false); yes.onclick = no.onclick = null; resolve(answer); };
    yes.onclick = () => done(true);
    no.onclick = () => done(false);
    modal("modal-confirm", true);
  });
}

function countdown(iso) {
  const ms = new Date(iso).getTime() - Date.now();
  if (ms <= 0) return { text: "kicked off", cls: "countdown-live" };
  const s = Math.floor(ms / 1000), d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  const text = d > 0 ? `${d}d ${h}h ${m}m` : h > 0 ? `${h}h ${m}m` : `${m}m ${s % 60}s`;
  return { text, cls: s <= 7200 ? "countdown-urgent" : "" };
}

function tickCountdowns() {
  document.querySelectorAll(".countdown[data-start]").forEach((el) => {
    const c = countdown(el.dataset.start);
    if (el.textContent !== c.text) el.textContent = c.text;
    const cls = `countdown ${c.cls}`.trim();
    if (el.className !== cls) el.className = cls;
  });
}
setInterval(tickCountdowns, 1000);

const pct = (v) => (v == null ? "—" : `${(v * 100).toFixed(1)}`);
const pp = (v) => (v == null ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(2)}`);
const ago = (iso) => {
  if (!iso) return "never";
  const m = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  return m < 60 ? `${m} min ago` : m < 2880 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} d ago`;
};

// ---------------------------------------------------------------------------
// Rounds and the match table
// ---------------------------------------------------------------------------
let rounds = [];
let currentRound = store.get("so.round", "");
let lastSig = null;
let lastResultSig = null;

async function loadRounds() {
  try { rounds = await api("/rounds"); } catch { rounds = []; }
  const sel = $("round-select");
  if (currentRound && !rounds.some((r) => r.round_label === currentRound)) currentRound = "";
  sel.innerHTML = `<option value="">All rounds</option>` +
    rounds.map((r) => `<option value="${esc(r.round_label)}">${esc(r.round_label)} (${esc(r.matches)})</option>`).join("");
  sel.value = currentRound;
  $("round-names").innerHTML = rounds.map((r) => `<option value="${esc(r.round_label)}"></option>`).join("");
  $("btn-refresh-round").disabled = !currentRound;
  for (const [id, path] of [["btn-download", "round.csv"], ["btn-download-polls", "round-polls.csv"]]) {
    const a = $(id);
    a.href = currentRound ? `${API}/export/${path}?round_label=${encodeURIComponent(currentRound)}` : "#";
    a.classList.toggle("disabled-link", !currentRound);
    a.setAttribute("aria-disabled", String(!currentRound));
  }
  const r = rounds.find((x) => x.round_label === currentRound);
  $("schedule-note").textContent = r
    ? `${r.bookmakers.length} bookmakers · ${r.markets.join(", ")} · ${r.schedule_preset} schedule. Snapshots are hours apart far from kickoff; that is set by the credit budget, not the API.`
    : "";
}

$("round-select").addEventListener("change", (e) => {
  currentRound = e.target.value;
  store.set("so.round", currentRound);
  lastSig = null;
  lastResultSig = null;
  loadRounds();
  loadTable();
});

function refCell(r) {
  const ref = r.reference;
  if (!ref) return `<span class="dim">no prices yet</span>`;
  const src = ref.source === "pinnacle" ? "Pinnacle" : `consensus of ${ref.books}`;
  return `<span class="hda"><b>${pct(ref.home)}</b> / ${pct(ref.draw)} / <b>${pct(ref.away)}</b></span><span class="sub">${esc(src)}</span>`;
}

const SIDE_NAME = { home: "Home", draw: "Draw", away: "Away" };

function steamChips(steam) {
  if (!steam || !steam.length) return "";
  return `<span class="steam-chips">${steam.map((x) => `<span class="steam-chip ${x.confirmed ? "confirmed" : ""}"
      title="${x.confirmed ? "Confirmed: steam in 2+ bookmakers at overlapping times" : "Steam in one bookmaker only"}">
      ${esc(SIDE_NAME[x.side])} ${esc(x.biggest_pp.toFixed(1))}pp · ${esc(x.books)} bk${x.confirmed ? " ✓" : ""}</span>`).join("")}</span>`;
}

function deltaCell(r) {
  const d = r.delta_pp;
  const one = (v) => `<span class="${v > 0.005 ? "up" : v < -0.005 ? "down" : ""}">${pp(v)}</span>`;
  const move = d ? `<span class="hda">${one(d.home)} / ${one(d.draw)} / ${one(d.away)}</span>` : `<span class="dim">—</span>`;
  return move + steamChips(r.steam);
}

function rowCells(r) {
  const next = r.next_snapshot;
  return [
    esc(new Date(r.kickoff).toLocaleString([], { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })),
    null, // countdown
    `${esc(r.home_team)} v ${esc(r.away_team)}${r.is_monitored ? "" : ' <span class="tag-off">not tracked</span>'}`,
    esc(r.league),
    refCell(r),
    deltaCell(r),
    `${esc(r.books)} books · ${esc(r.snapshots)} rows<span class="sub">last ${esc(ago(r.last_snapshot))}</span>`,
    next ? `${esc(next.label)}<span class="sub">${esc(fmtWhen(next.at))}</span>` : `<span class="dim">${r.is_monitored ? "—" : "stopped"}</span>`,
  ];
}

const scoreVal = (v) => (v == null ? "" : String(v));

function buildRow(r) {
  const cells = rowCells(r).map((html, i) => i === 1
    ? `<td class="c-cd"><span class="countdown" data-start="${esc(r.kickoff)}"></span></td>`
    : `<td class="c${i}" data-h="">${html}</td>`).join("");
  return `<tr data-id="${r.id}">${cells}<td class="result-cell">
      <input type="number" class="result-input" data-side="home" value="${scoreVal(r.home_score)}" placeholder="H" min="0" max="99" aria-label="Home goals" />
      <span class="dim">-</span>
      <input type="number" class="result-input" data-side="away" value="${scoreVal(r.away_score)}" placeholder="A" min="0" max="99" aria-label="Away goals" />
      <button type="button" class="btn btn-ghost result-save" title="Save result" aria-label="Save result">✓</button>
    </td></tr>`;
}

function patchRow(tr, r) {
  rowCells(r).forEach((html, i) => {
    if (i === 1) return;
    const td = tr.children[i];
    if (td.dataset.h !== html) { td.innerHTML = html; td.dataset.h = html; }
  });
  for (const [side, v] of [["home", r.home_score], ["away", r.away_score]]) {
    const input = tr.querySelector(`.result-input[data-side="${side}"]`);
    if (input.dataset.dirty === "1" || document.activeElement === input) continue; // never wipe typing
    if (input.value !== scoreVal(v)) input.value = scoreVal(v);
  }
}

function renderTable(rows) {
  const body = $("match-body");
  if (!rows.length) {
    body.innerHTML = `<tr><td colspan="9" class="dim empty-row">No matches here yet. Open Discovery to pick some.</td></tr>`;
    return;
  }
  const existing = [...body.querySelectorAll("tr[data-id]")];
  const same = existing.length === rows.length && existing.every((tr, i) => Number(tr.dataset.id) === rows[i].id);
  if (!same) {
    const unsaved = {};
    for (const inp of body.querySelectorAll('.result-input[data-dirty="1"]')) {
      const id = inp.closest("tr").dataset.id;
      (unsaved[id] ||= {})[inp.dataset.side] = inp.value;
    }
    body.innerHTML = rows.map(buildRow).join("");
    for (const [id, sides] of Object.entries(unsaved)) {
      const tr = body.querySelector(`tr[data-id="${id}"]`);
      if (!tr) continue;
      for (const [side, value] of Object.entries(sides)) {
        const inp = tr.querySelector(`.result-input[data-side="${side}"]`);
        inp.value = value;
        inp.dataset.dirty = "1";
      }
    }
  }
  const trs = body.querySelectorAll("tr[data-id]");
  rows.forEach((r, i) => patchRow(trs[i], r));
  tickCountdowns();
}

async function loadTable() {
  const qs = currentRound ? `?round_label=${encodeURIComponent(currentRound)}` : "";
  let data;
  try { data = await api(`/matches${qs}`); } catch { return; }
  const sig = `${currentRound}|${JSON.stringify(data.matches)}`;
  if (sig === lastSig) return; // nothing changed: touch nothing
  lastSig = sig;
  renderTable(data.matches);
  const resultSig = data.matches.map((m) => `${m.id}:${m.home_score}:${m.away_score}:${(m.steam || []).length}`).join(",");
  if (resultSig !== lastResultSig) { lastResultSig = resultSig; loadScorecard(); }
  if (openMatchId && data.matches.some((m) => m.id === openMatchId)) refreshDrilldown();
}

$("match-body").addEventListener("input", (e) => {
  if (e.target.matches(".result-input")) e.target.dataset.dirty = "1";
});
$("match-body").addEventListener("click", async (e) => {
  const tr = e.target.closest("tr[data-id]");
  if (!tr) return;
  const btn = e.target.closest(".result-save");
  if (btn) {
    const h = tr.querySelector('.result-input[data-side="home"]'), a = tr.querySelector('.result-input[data-side="away"]');
    const body = { home_score: h.value === "" ? null : Number(h.value), away_score: a.value === "" ? null : Number(a.value) };
    btn.disabled = true;
    try {
      await api(`/matches/${tr.dataset.id}/result`, { method: "PUT", body: JSON.stringify(body) });
      delete h.dataset.dirty; delete a.dataset.dirty;
      btn.classList.add("saved");
      setTimeout(() => btn.classList.remove("saved"), 1200);
      lastSig = null;
      await loadTable();
    } catch (err) { alert(err.message); } finally { btn.disabled = false; }
    return;
  }
  if (e.target.closest(".result-cell")) return;
  openDrilldown(Number(tr.dataset.id));
});

// ---- scorecard ----------------------------------------------------------------
const scPct = (right, n) => (n ? `${Math.round((right / n) * 100)}%` : "—");

function scCells(t) {
  if (!t || !t.settled) return `<div class="sc-empty dim">No final scores entered for this round yet.</div>`;
  const e = t.engine, st = t.steam, c = t.converged;
  return `
    <div class="sc-cell sc-engine"><h4>Engine favourite</h4><span class="sc-value">${e.right}/${e.picks}</span><span class="sc-pct">${scPct(e.right, e.picks)}</span>
      <div class="sc-sub">${e.no_pick} without a pick (level opening prices)</div></div>
    <div class="sc-cell sc-steam"><h4>Confirmed steam</h4><span class="sc-value">${st.right}/${st.flagged}</span><span class="sc-pct">${scPct(st.right, st.flagged)}</span>
      <div class="sc-sub">${st.flagged ? `covers ${st.avg_sides} side(s) on average; blind picks of as many sides: ~${st.chance_right} right` : "no match with confirmed steam"}</div></div>
    <div class="sc-cell sc-conv"><h4>Both agree</h4><span class="sc-value">${c.right}/${c.count}</span><span class="sc-pct">${scPct(c.right, c.count)}</span>
      <div class="sc-sub">engine favourite was also a confirmed steam side</div></div>
    <div class="sc-cell"><h4>Results in</h4><span class="sc-value">${t.settled}</span><div class="sc-sub">matches with a final score</div></div>`;
}

async function loadScorecard() {
  let d;
  try { d = await api(`/scorecard${currentRound ? `?round_label=${encodeURIComponent(currentRound)}` : ""}`); } catch { return; }
  $("sc-round").textContent = currentRound ? currentRound : "all rounds";
  $("sc-cells").innerHTML = scCells(currentRound ? d.round : d.all);
  const row = (label, t, cls = "") => `<tr class="${cls}"><td>${esc(label)}</td><td>${t.settled}</td>
    <td>${t.engine.right}/${t.engine.picks}</td><td>${t.steam.right}/${t.steam.flagged} <span class="dim">(chance ~${t.steam.chance_right})</span></td>
    <td>${t.converged.right}/${t.converged.count}</td></tr>`;
  $("sc-body").innerHTML = d.rounds.length
    ? d.rounds.map((r) => row(r.round_label, r, r.round_label === currentRound ? "sc-current" : "")).join("") + row("All rounds", d.all, "sc-total")
    : `<tr><td colspan="5" class="dim">No results yet.</td></tr>`;
}

// ---- poller status and manual refresh --------------------------------------
async function loadPoller() {
  try {
    const p = await api("/poller");
    const pill = $("poller-status");
    const next = p.plan && p.plan.next;
    pill.textContent = !p.enabled ? "poller off" : !p.leader ? "poller: standby" : next ? `next poll ${fmtWhen(next.at)}` : "poller: idle";
    pill.title = p.plan ? `${next ? `Next: ${next.sport} (${next.labels.join(", ")}), ${next.cost} credit(s). ` : ""}Plan this month: ${p.plan.calls_this_month} calls, ${p.plan.cost_this_month} credits` +
      (p.plan.dropped_calls ? `; ${p.plan.dropped_calls} planned refreshes skipped to fit the budget` : "") : "";
  } catch { /* ignore */ }
}

$("btn-refresh-round").onclick = async () => {
  if (!currentRound) return;
  let q;
  try { q = await api(`/rounds/refresh-quote?round_label=${encodeURIComponent(currentRound)}`); } catch (e) { alert(e.message); return; }
  if (!q.cost) { alert("Nothing to refresh: no tracked match before kickoff in this round."); return; }
  const b = q.budget;
  const ok = await askYesNo(`Refresh "${currentRound}" now?`,
    `This makes ${q.leagues.length} paid call(s): ${q.leagues.map((l) => `${l.sport} (${l.matches} matches, ${l.cost} cr)`).join(", ")}.\n` +
    `Total: ${q.cost} credit(s). Used this month: ${b.used} of ${b.limit}; manual refreshes stop at ${b.pause_line}.`, `Spend ${q.cost} credit(s)`);
  if (!ok) return;
  try {
    const res = await api("/rounds/refresh", { method: "POST", body: JSON.stringify({ round_label: currentRound, expected_cost: q.cost }) });
    const refused = res.results.filter((r) => !r.allowed);
    if (refused.length) alert(`Not done: ${refused.map((r) => `${r.sport}: ${r.reason}`).join("; ")}`);
  } catch (e) { alert(e.message); }
  lastSig = null;
  await Promise.all([loadTable(), loadCredits(), loadPoller()]);
};

// ---------------------------------------------------------------------------
// Drill-down
// ---------------------------------------------------------------------------
const BOOK_COLORS = ["#2dffa0", "#38d6ff", "#9b7bff", "#ffd93d", "#ff9f1a", "#ff6b9d", "#7ee787", "#79c0ff", "#d2a8ff", "#ffa657", "#56d4dd", "#f778ba"];
let openMatchId = null, ddData = null, ddSide = "home", charts = [];
const bookTitle = (k) => (D.books.find((b) => b.key === k) || {}).title || k;

let chartObservers = [];
function destroyCharts() {
  chartObservers.forEach((o) => o.disconnect()); // stop resize callbacks before the charts go
  chartObservers = [];
  charts.forEach((c) => c.remove());
  charts = [];
}

function makeChart(id) {
  const el = $(id);
  el.innerHTML = "";
  if (typeof LightweightCharts === "undefined") { el.innerHTML = `<div class="dim pad">Charts library did not load (offline?).</div>`; return null; }
  const chart = LightweightCharts.createChart(el, {
    width: el.clientWidth, height: el.clientHeight,
    layout: { background: { color: "#070e1b" }, textColor: "#a9c4e8", fontFamily: "monospace" },
    grid: { vertLines: { color: "#12223a" }, horzLines: { color: "#12223a" } },
    rightPriceScale: { borderColor: "#1c3354" }, timeScale: { borderColor: "#1c3354", timeVisible: true },
  });
  const obs = new ResizeObserver(() => { if (el.clientWidth) chart.applyOptions({ width: el.clientWidth }); });
  obs.observe(el);
  chartObservers.push(obs);
  charts.push(chart);
  return chart;
}

function seriesByBook(rows, value) {
  const out = new Map();
  for (const r of rows) {
    const v = value(r);
    if (v == null || !isFinite(v)) continue;
    if (!out.has(r.bookmaker)) out.set(r.bookmaker, []);
    const t = Math.floor(new Date(r.captured_at).getTime() / 1000);
    const arr = out.get(r.bookmaker);
    if (arr.length && arr[arr.length - 1].time === t) arr[arr.length - 1].value = v; else arr.push({ time: t, value: v });
  }
  return out;
}

function drawLines(chartId, legendId, rows, value, fmt) {
  const chart = makeChart(chartId);
  const series = seriesByBook(rows, value);
  const books = [...series.keys()].sort((a, b) => (a === "pinnacle" ? -1 : b === "pinnacle" ? 1 : a.localeCompare(b)));
  const legend = [];
  books.forEach((bk, i) => {
    const color = BOOK_COLORS[i % BOOK_COLORS.length];
    legend.push(`<span class="swatch"><span class="dot" style="background:${color}"></span>${esc(bookTitle(bk))}</span>`);
    if (chart) chart.addLineSeries({ color, lineWidth: bk === "pinnacle" ? 3 : 2, pointMarkersVisible: true, lineType: 1,
      priceFormat: { type: "custom", formatter: fmt } }).setData(series.get(bk));
  });
  $(legendId).innerHTML = legend.join("") || `<span class="dim">No snapshots yet.</span>`;
  if (chart) chart.timeScale().fitContent();
}

function renderDrilldown() {
  const d = ddData, m = d.match;
  $("dd-title").textContent = `${m.home_team} v ${m.away_team}`;
  $("dd-league").textContent = m.league_title || m.sport_key;
  const cd = $("dd-countdown");
  cd.dataset.start = m.commence_time;
  $("dd-monitor").textContent = m.is_monitored ? "Stop tracking" : "Resume tracking";
  $("dd-monitor").disabled = !m.is_monitored && new Date(m.commence_time) <= new Date();

  const h2h = d.snapshots.filter((s) => s.market === "h2h");
  const latest = new Map();
  for (const s of h2h) latest.set(s.bookmaker, s);
  const g = d.analysis.gaps;
  const gapText = g.median_minutes == null ? "Only one snapshot so far."
    : `Snapshots are ${g.min_minutes}–${g.max_minutes} min apart (median ${g.median_minutes} min). Each step holds a price until the next snapshot; nothing is known about the price in between.`;
  $("dd-gap-note").textContent = gapText;

  const card = (title, value, sub) => `<div class="card"><h4>${esc(title)}</h4><div class="value">${value}</div><div class="sub">${sub}</div></div>`;
  $("dd-cards").innerHTML = [
    card("Kickoff", esc(fmtWhen(m.commence_time)), esc(m.round_label || "")),
    card("Bookmakers priced", esc(latest.size), `${esc(d.settings ? d.settings.bookmakers.length : 0)} requested`),
    card("Snapshots of this match", esc(g.polls), `${esc(d.snapshots.length)} stored rows (only price changes are stored)`),
    card("Paid polls of this league", esc(d.polls.length), d.polls.length ? `last ${esc(ago(d.polls[d.polls.length - 1].t))}` : "none recorded"),
  ].join("");

  const a = d.analysis, cmp = a.comparison;
  const ref = cmp.reference;
  $("dd-latest-note").textContent = ref
    ? `Reference: ${ref.source === "pinnacle" ? "Pinnacle" : `consensus (median of ${ref.books} books)`}. Best price per outcome is highlighted. Off = fair % minus the reference, in pp.`
    : "";
  const cell = (b, side) => `<td class="${b.is_best[side] ? "best" : ""}">${esc(b.prices[side] ?? "—")}</td>`;
  const off = (b) => {
    const v = b.off_market_pp;
    const one = (x) => `<span class="${Math.abs(x) >= 2 ? "far" : ""}">${pp(x)}</span>`;
    return `${one(v.home)} / ${one(v.draw)} / ${one(v.away)}`;
  };
  $("dd-latest").innerHTML = cmp.books.length ? `<table class="book-table cmp"><thead><tr><th>Bookmaker</th><th>Home</th><th>Draw</th><th>Away</th><th>Fair H/D/A %</th><th>Margin</th><th>Off (pp)</th></tr></thead><tbody>` +
    cmp.books.map((b) => `<tr><td>${esc(bookTitle(b.bookmaker))}${b.bookmaker === "pinnacle" ? ' <span class="tag-ref">ref</span>' : ""}</td>
      ${cell(b, "home")}${cell(b, "draw")}${cell(b, "away")}
      <td>${pct(b.fair.home)} / ${pct(b.fair.draw)} / ${pct(b.fair.away)}</td><td>${b.margin_pct == null ? "—" : b.margin_pct.toFixed(2) + "%"}</td><td>${off(b)}</td></tr>`).join("") +
    `</tbody></table><p class="dim small">Spread between books (max − min fair %): home ${cmp.spread_pp.home} pp, draw ${cmp.spread_pp.draw} pp, away ${cmp.spread_pp.away} pp.</p>`
    : `<div class="dim pad">No prices stored yet. The first snapshot is taken by the poller shortly after a match is added.</div>`;

  const gp = a.gaps;
  $("dd-steam-note").textContent = (gp.median_minutes == null
    ? `Only ${gp.polls} snapshot(s) so far: steam needs a series. `
    : `Snapshots for this match are ${gp.min_minutes}–${gp.max_minutes} min apart (median ${gp.median_minutes} min). `) +
    `A steam reading looks at the last ${gp.steam_window_minutes / 60} h; any window the snapshots cannot resolve is skipped, not estimated. ` +
    "So these labels only describe how prices moved between snapshots, not how fast. The predecessor tool's backtest found steam gives no reliable edge even with 15-minute data.";
  const sides = a.steam.sides;
  $("dd-steam").innerHTML = sides.length ? `<ul class="steam-list">${sides.map((x) => `<li>
      <span class="steam-side">${esc(SIDE_NAME[x.side])}</span>
      ${x.confirmed ? `<span class="badge-ok">confirmed in ${esc(x.confirmed_books.length)} books</span>` : `<span class="badge-dim">1 book only</span>`}
      <span>biggest ${esc(x.biggest_pp.toFixed(2))} pp at ${esc(bookTitle(x.biggest_book))}, ${esc(x.timing || "")} (${esc(fmtWhen(x.from))} → ${esc(fmtTime(x.to))})</span>
      <span class="dim">books: ${x.books.map((b) => esc(bookTitle(b))).join(", ")}</span></li>`).join("")}</ul>`
    : `<div class="dim">No steam move in any bookmaker${gp.polls < 3 ? " (too few snapshots yet)" : ""}.</div>`;
  const eng = a.engine;
  const engCard = card("Engine favourite", eng.favorite ? esc(SIDE_NAME[eng.favorite]) : "no pick",
    eng.favorite ? `${esc(eng.source === "pinnacle" ? "Pinnacle" : "consensus")} opening ${esc(eng.favorite_prob.toFixed(1))}%${eng.lead_change ? ` · lead change ${esc(eng.lead_change_gap_pp.toFixed(1))} pp` : ""}` : "level opening prices");
  $("dd-cards").insertAdjacentHTML("beforeend", engCard);

  const margin = (r) => ((1 / r.home_price + (r.draw_price ? 1 / r.draw_price : 0) + 1 / r.away_price - 1) * 100);
  document.querySelectorAll("#dd-side .seg-btn").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.side === ddSide)));
  destroyCharts();
  // Charts use the series rebuilt at every poll (stored rows are changes only).
  const polled = Object.entries(a.series).flatMap(([bookmaker, rows]) => rows.map((x) => ({
    bookmaker, captured_at: x[0], fair_home: x[1], fair_draw: x[2], fair_away: x[3],
    home_price: x[4], draw_price: x[5], away_price: x[6] })));
  drawLines("chart-fair", "legend-fair", polled, (r) => r[`fair_${ddSide}`] * 100, (v) => `${v.toFixed(1)}%`);
  drawLines("chart-margin", "legend-margin", polled, margin, (v) => `${v.toFixed(2)}%`);

  $("dd-plan").innerHTML = d.plan.length
    ? `<ul class="plan-list">${d.plan.map((p) => `<li class="${p.dropped ? "dropped" : ""}"><b>${esc(p.label)}</b> ${esc(fmtWhen(p.at))}${p.dropped ? " — skipped to fit the credit budget" : p.this_month ? "" : " — next month's budget"}</li>`).join("")}</ul>`
    : `<span class="dim">${m.is_monitored ? "No more snapshots planned." : "Not tracked: no snapshots planned."}</span>`;
  tickCountdowns();
}

async function refreshDrilldown() {
  if (!openMatchId) return;
  try { ddData = await api(`/matches/${openMatchId}`); } catch { return; }
  renderDrilldown();
}

async function openDrilldown(id) {
  openMatchId = id;
  $("drilldown").classList.remove("hidden");
  document.body.classList.add("dd-open");
  if (!D.books.length) { try { D.books = (await api("/discovery/bookmakers")).bookmakers; } catch { /* titles fall back to keys */ } }
  await refreshDrilldown();
}

function closeDrilldown() {
  openMatchId = null;
  destroyCharts();
  $("drilldown").classList.add("hidden");
  document.body.classList.remove("dd-open");
}

$("dd-close").onclick = closeDrilldown;
$("dd-side").addEventListener("click", (e) => {
  const side = e.target.dataset.side;
  if (side && ddData) { ddSide = side; renderDrilldown(); }
});
$("dd-monitor").onclick = async () => {
  if (!ddData) return;
  const on = !ddData.match.is_monitored;
  if (!on && !(await askYesNo("Stop tracking this match?", "No more snapshots will be bought for it. You can resume before kickoff.", "Stop tracking"))) return;
  try { await api(`/matches/${openMatchId}/monitor?on=${on}`, { method: "PUT" }); } catch (e) { alert(e.message); }
  lastSig = null;
  await Promise.all([refreshDrilldown(), loadTable()]);
};

// ---------------------------------------------------------------------------
// Storage
// ---------------------------------------------------------------------------
const fmtBytes = (n) => (n >= 1e9 ? `${(n / 1e9).toFixed(2)} GB` : n >= 1e6 ? `${(n / 1e6).toFixed(1)} MB` : `${Math.round(n / 1e3)} kB`);

async function refreshStoragePill() {
  try {
    const u = await api("/storage/status");
    const pill = $("storage-pill");
    pill.textContent = `DB ${u.used_pct.toFixed(1)}%`;
    pill.className = `pill storage-pill storage-${u.level}`;
    pill.title = `${fmtBytes(u.used_bytes)} of ${fmtBytes(u.limit_bytes)}${u.collection_paused ? " - collection paused" : ""}`;
  } catch { /* ignore */ }
}

async function loadStorage() {
  const s = await api("/storage");
  const u = s.usage;
  const fill = $("storage-bar-fill");
  fill.style.width = `${Math.min(100, u.used_pct)}%`;
  fill.className = `storage-bar-fill storage-${u.level}`;
  $("storage-summary").textContent = `${fmtBytes(u.used_bytes)} of ${fmtBytes(u.limit_bytes)} (${u.used_pct}%). ` +
    (u.collection_paused ? "Collection is PAUSED until space is freed. " : "") +
    (s.pending_delete_matches ? `${s.pending_delete_matches} deleted matches are still being cleared.` : "");
  $("storage-table-body").innerHTML = s.rounds.length ? s.rounds.map((r) => `<tr>
      <td>${esc(r.round_label)}</td><td>${esc(r.matches)}</td><td>${esc(r.snapshots)}</td><td>${esc(fmtBytes(r.est_bytes))}</td>
      <td>${r.downloaded_at ? esc(fmtWhen(r.downloaded_at)) : '<span class="warn-text">never</span>'}</td>
      <td><button class="btn btn-danger btn-sm" data-del="${esc(r.round_label)}" ${r.active ? 'disabled title="Stop tracking its matches first"' : ""}>Delete</button></td>
    </tr>`).join("") : `<tr><td colspan="6" class="dim">No rounds.</td></tr>`;
}

$("storage-pill").onclick = async () => { modal("modal-storage", true); $("storage-message").textContent = ""; await loadStorage(); };
$("btn-storage-close").onclick = () => modal("modal-storage", false);
$("storage-table-body").addEventListener("click", async (e) => {
  const label = e.target.dataset.del;
  if (!label) return;
  if (!(await askYesNo(`Delete round "${label}"?`, "Its matches and every snapshot are removed for good.", "Delete"))) return;
  const del = (extra) => fetch(`${API}/rounds?round_label=${encodeURIComponent(label)}${extra}`, { method: "DELETE" });
  let res = await del("");
  if (res.status === 409 && (await res.clone().json()).detail.startsWith("never_downloaded")) {
    const sure = await askYesNo("This round was never downloaded",
      "There is no CSV copy of this round anywhere. Deleting it loses its data for good. Delete anyway?", "Delete without a copy");
    if (!sure) return;
    res = await del("&allow_undownloaded=true");
  }
  const body = await res.json();
  $("storage-message").textContent = res.ok ? `Deleted "${label}" (${body.deleted_matches} matches).` : body.detail;
  await Promise.all([loadStorage(), loadRounds(), refreshStoragePill()]);
  lastSig = null;
  loadTable();
});
$("btn-reclaim").onclick = async () => {
  $("storage-message").textContent = "Reclaiming…";
  try {
    const r = await api("/storage/reclaim", { method: "POST" });
    $("storage-message").textContent = `Done: ${fmtBytes(r.before_bytes)} → ${fmtBytes(r.after_bytes)}.` + (r.skipped.length ? ` Skipped (not enough room): ${r.skipped.join(", ")}.` : "");
  } catch (e) { $("storage-message").textContent = e.message; }
  await Promise.all([loadStorage(), refreshStoragePill()]);
};

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
    currentRound = res.round_label;
    store.set("so.round", currentRound);
    lastSig = null;
    await Promise.all([loadRounds(), loadEvents(false)]);
    loadTable();
  } catch (e) {
    status.className = "small err";
    status.textContent = e.message;
  } finally {
    btn.disabled = D.selectedEvents.size === 0;
  }
};

document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (!$("modal-confirm").classList.contains("hidden")) return; // the confirm box answers itself
  if (openMatchId && document.querySelectorAll(".modal:not(.hidden)").length === 0) { closeDrilldown(); return; }
  for (const id of ["modal-discovery", "modal-credits", "modal-storage"]) modal(id, false);
});

// ---------------------------------------------------------------------------
loadCredits();
loadRounds().then(loadTable);
loadPoller();
refreshStoragePill();
setInterval(loadTable, 20000);
setInterval(loadPoller, 30000);
setInterval(loadCredits, 60000);
setInterval(refreshStoragePill, 120000);
