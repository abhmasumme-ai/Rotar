/* ========= CONFIG & STATE ========= */
const SERVED = location.protocol === "http:" || location.protocol === "https:";
const API_BASE = SERVED ? location.origin : "http://127.0.0.1:5000";
const WS_URL = SERVED
  ? (location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws"
  : "ws://127.0.0.1:5000/ws";
const TELEGRAM_ID = "@abc_trc123";
const MAX_LOG = 200;
const MAX_MS = 1000;
const RECONNECT_MS = 3000;
const SETTINGS_KEY = "cricketPanelSettings";
const OVER_DELAY_MS = 0;
const INN_BREAK_DELAY_MS = 800;
const WICKET_TYPE_DELAY_MS = 0;
const FALLBACK_QUIET_MS = 45000;
const PIE_MIN_MS = 200;   // pie throttle

const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

/* Cached DOM refs (hot path) */
const EL = {
  score: $("score"), overTxt: $("overTxt"), team: $("team"),
  status: $("status"), ball: $("ball"),
  overNo: $("overNo"), overDots: $("overDots"),
  lgR: $("lgR"), lgB: $("lgB"),
  fixture: $("fixture"), terminal: $("terminal"),
  barClick: $("barClick"), barClickTxt: $("barClickTxt"),
  barOdds: $("barOdds"), barOddsTxt: $("barOddsTxt"),
};

const S = {
  started: false, ws: null, ping: null, reconnect: null,
  prevMs: null, label: "", matchKey: "", active: true, auto: false, idle: true,
  fmt: "", cur: null, isFreeHitNext: false, unk: new Set(),
  oversOverride: Number(new URLSearchParams(location.search || "").get("overs")) || 0
};

/* ========= Terminal ========= */
function log(msg, err) {
  const t = EL.terminal;
  if (!t) return;
  const li = document.createElement("li");
  const time = new Date().toLocaleTimeString();
  if (err) { li.className = "err"; li.textContent = `[${time}] ${msg}`; }
  else li.innerHTML = `[${time}] ${msg}`;
  const atBottom = t.scrollTop + t.clientHeight >= t.scrollHeight - 20;
  t.appendChild(li);
  while (t.children.length > MAX_LOG) t.firstChild.remove();
  if (atBottom) t.scrollTop = t.scrollHeight;
}
function matchLabel() { return S.label || EL.score?.parentElement?.textContent || "match"; }

/* ========= Bars ========= */
const BAR_MIN = 100, BAR_MAX = 5000;
function setBar(id, ms) {
  const el = id === "barClick" ? EL.barClick : EL.barOdds;
  const t = id === "barClick" ? EL.barClickTxt : EL.barOddsTxt;
  if (!el) return;
  const out = ms > BAR_MAX;
  const h = 20 + (Math.min(BAR_MAX, Math.max(BAR_MIN, ms)) - BAR_MIN) / (BAR_MAX - BAR_MIN) * 20;
  el.style.height = h + "px";
  if (t) {
    t.textContent = out ? "OUT TO LIMIT" : Math.round(ms) + "ms";
    t.style.color = out ? "var(--red)" : "";
  }
}

/* ========= Pie chart (throttled, last value always rendered) ========= */
const GREY = "#555";
let chart = null;

function setPie(vals, cols) {
  if (!chart) return;
  chart.data.datasets[0].data = vals;
  chart.data.datasets[0].backgroundColor = cols;
  chart.update("none");
}

let _pieData = { r: 0, b: 0, dirty: false };
let _pieTimer = 0;
let _pieLastMs = 0;

function pieOff() {
  _pieData = { r: 0, b: 0, dirty: false };
  if (_pieTimer) { clearTimeout(_pieTimer); _pieTimer = 0; }
  setPie([1, 1], [GREY, GREY]);
  if (EL.lgR) EL.lgR.textContent = "0ms";
  if (EL.lgB) EL.lgB.textContent = "0ms";
}

function _pieRenderNow() {
  _pieTimer = 0;
  if (!_pieData.dirty) return;
  _pieData.dirty = false;
  _pieLastMs = performance.now();
  if (!chart) return;
  if (document.visibilityState !== "visible") return;
  const r = Math.min(Math.max(0, _pieData.r), MAX_MS) || 1;
  const b = Math.min(Math.max(0, _pieData.b), MAX_MS) || 1;
  setPie([r, b], ["#ff2a2a", "#17f0e0"]);
}

function pieOnFast(r, b) {
  _pieData.r = Math.max(0, r);
  _pieData.b = Math.max(0, b);
  _pieData.dirty = true;
  if (EL.lgR) EL.lgR.textContent = Math.round(_pieData.r) + "ms";
  if (EL.lgB) EL.lgB.textContent = Math.round(_pieData.b) + "ms";
  const now = performance.now();
  const wait = Math.max(0, PIE_MIN_MS - (now - _pieLastMs));
  if (wait === 0) _pieRenderNow();
  else if (!_pieTimer) _pieTimer = setTimeout(_pieRenderNow, wait);
}

document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && _pieData.dirty) _pieRenderNow();
});

/* ========= Rings ========= */
const RINGS = [
  {key:"0.5 over", label:"0.5 OVER", color:"#e5102a", n:5, hl:"background:#bdbdf0;color:#000"},
  {key:"0.5 under", label:"0.5 UNDER", color:"#bdbdf0", n:3, hl:"background:#ff2a2a"},
  {key:"wicket in fall", label:"WICKET", color:"#e5102a", n:3, hl:"background:#ff2a2a"},
  {key:"2 run over", label:"2/3 OVER", color:"#e0962c", n:5, hl:"background:#17f0e0;color:#000"},
  {key:"4 run over", label:"4/6 OVER", color:"#e020e0", n:3, hl:"background:#16f055;color:#000"},
  {key:"batsman under", label:"BATSMAN", color:"#1ee0f5", n:3, hl:"background:#2a2af0"}
];

function initRings() {
  const container = $("rings");
  if (!container) return;
  container.innerHTML = "";
  RINGS.forEach(r => {
    const b = document.createElement("button");
    b.type = "button"; b.className = "ring";
    b.innerHTML = `${r.label}<svg viewBox="0 0 110 110"><circle cx="55" cy="55" r="38" fill="none" stroke="${r.color}" stroke-width="16" stroke-dasharray="190 50" transform="rotate(-30 55 55)"/><text x="55" y="65" text-anchor="middle">${r.n}</text></svg>`;
    b.onclick = () => clickAction(r);
    container.appendChild(b);
  });
}
initRings();

async function postApi(path, body) {
  const t0 = performance.now();
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body)
  });
  if (!res.ok) throw new Error(res.status);
  return performance.now() - t0;
}

async function clickAction(r) {
  try {
    const ms = await postApi("/api/click", {action: r.key, telegram: TELEGRAM_ID, match: matchLabel()});
    setBar("barClick", ms);
    log(`clicked on <span class="hl" style="${r.hl}">${esc(r.key)}</span> on ${esc(matchLabel())}`);
  } catch (e) {
    log(`# ERROR click "${r.key}" failed`, true);
  }
}

/* ========= Fixtures ========= */
let _warnedEmpty = false;
async function loadFixtures(quiet) {
  try {
    const res = await fetch(`${API_BASE}/api/matches`);
    const result = await res.json();
    let matches = [];
    if (result.data && result.data.matches) matches = result.data.matches;
    else if (result.matches) matches = result.matches;
    else if (Array.isArray(result)) matches = result;
    else if (result.data && Array.isArray(result.data)) matches = result.data;

    if (matches.length > 0) {
      renderMatches(matches);
      if (!quiet) log(`Loaded ${matches.length} matches into fixtures.`);
    } else if (!_warnedEmpty) {
      _warnedEmpty = true;
      log("# Fixture khali: push/REST theke data asheni");
    }
  } catch (err) {
    log("# Error fetching matches fixture", true);
  }
}

function renderMatches(matches) {
  const tbody = EL.fixture;
  if (!tbody) return;
  const order = { started: 0, not_started: 1, completed: 2 };
  matches = [...matches].sort((a, b) => (order[a.status] ?? 3) - (order[b.status] ?? 3));

  let html = "";
  matches.forEach(m => {
    const matchId = String(m.key || m.id || "N/A");
    TEAMS[matchId] = { a: m.teams?.a?.name, b: m.teams?.b?.name };
    const title = m.title || m.name ||
      `${m.teams?.a?.name || "Team 1"} vs ${m.teams?.b?.name || "Team 2"}`;
    const format = String(m.format || m.type || "-").toUpperCase();
    const status = String(m.status || "not_started").toLowerCase();
    STATUS[matchId] = status;
    if (m.start_at) START[matchId] = m.start_at;
    const [stTxt, stCol] = statusView(status);

    html += `<tr style="cursor:pointer" data-key="${esc(matchId)}" data-title="${esc(title)}" data-type="${esc(format)}" data-status="${esc(status)}">
      <td><code>${esc(matchId)}</code></td>
      <td><strong>${esc(title)}</strong></td>
      <td>${esc(format)}</td>
      <td><span style="color:${stCol}">${esc(stTxt)}</span></td>
    </tr>`;
  });
  tbody.innerHTML = html;
  tbody.querySelectorAll("tr").forEach(tr =>
    tr.addEventListener("click", () => selectMatch(tr.dataset.key, tr.dataset.title, tr.dataset.type))
  );
}

function selectMatch(key, title, type) {
  S.matchKey = key;
  S.label = title;
  S.fmt = type || "";
  const st0 = ms(key);
  st0.dots = []; st0.dotsOver = null; st0.order = null; st0.seen.clear();
  st0.objTs = 0; st0.lastCommId = null; st0.evAt = 0;
  S.isFreeHitNext = false;
  if ($("matchKey")) $("matchKey").value = key;
  if ($("type")) $("type").value = type || "";
  if ($("matchTeams")) $("matchTeams").textContent = title;
  log(`Selected match: ${esc(title)}`);
  resetBall();
  startAll();
  setActive(true);
  resetViewport();
  showStatus(key);
  sendWatch();
  const st = ms(key);
  if (st.has) renderLive(key);
  fetchBaseline(key);
}

/* ========= Admin form ========= */
const FORM_FIELDS = ["site", "match", "over05", "under05", "over23", "b46", "bat", "wk"];

function setFormLocked(locked) {
  FORM_FIELDS.forEach(id => { if ($(id)) $(id).disabled = locked; });
  const sub = document.querySelector("#adminForm .submit");
  if (sub) sub.disabled = locked;
}

function saveSettings(data) {
  try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(data)); } catch (e) {}
}

function loadSettings() {
  try {
    const raw = localStorage.getItem(SETTINGS_KEY);
    if (!raw) return;
    const data = JSON.parse(raw);
    FORM_FIELDS.forEach(id => { if ($(id) && data[id] !== undefined) $(id).value = data[id]; });
    setFormLocked(true);
  } catch (e) {}
}

$("adminForm")?.addEventListener("submit", e => {
  e.preventDefault();
  let ok = true;
  FORM_FIELDS.forEach(id => {
    const el = $(id);
    if (!el) return;
    const empty = !el.value.trim();
    el.classList.toggle("need", empty);
    if (empty) ok = false;
  });
  if (!ok) { log("# Please fill all required fields.", true); return; }
  const data = {};
  FORM_FIELDS.forEach(id => { data[id] = $(id).value.trim(); });
  saveSettings(data);
  setFormLocked(true);
  log("Settings saved.");
});

document.querySelector('[data-edit="adminForm"]')?.addEventListener("click", () => {
  setFormLocked(false);
  log("# Edit mode enabled.");
});

FORM_FIELDS.forEach(id => $(id)?.addEventListener("input", () => $(id).classList.remove("need")));

function setActive(on) {
  const b = $("activeBtn");
  if (!b) return;
  S.active = !!on;
  b.classList.toggle("on", !!on);
  b.textContent = on ? "ACTIVE MATCH" : "NO ACTIVE MATCH";
}

/* ========= WebSocket ========= */
function sendWatch() {
  if (S.ws && S.ws.readyState === WebSocket.OPEN) S.ws.send("watch:" + (S.matchKey || ""));
}

function startLiveListener() {
  if (S.ws) { S.ws.onclose = null; S.ws.close(); }
  clearInterval(S.ping);
  clearTimeout(S.reconnect);

  S.ws = new WebSocket(WS_URL);

  S.ws.onopen = () => {
    log("# WebSocket connected.");
    clearInterval(S.ping);
    S.ping = setInterval(() => {
      if (S.ws && S.ws.readyState === WebSocket.OPEN) S.ws.send("ping");
    }, 30000);
    sendWatch();
  };

  S.ws.onmessage = (event) => {
    if (event.data === "pong") return;
    try {
      const m = JSON.parse(event.data);
      if (S.matchKey && m.id && String(m.id) !== S.matchKey) return;
      let delay = 10;
      if (m._server_ts) delay = Math.max(1, Math.round(Date.now() - m._server_ts));

      updateViewport(m);          // ball event → সাথে সাথে DOM
      setBar("barOdds", delay);
      if (typeof m._lag_ms === "number") pieOnFast(m._lag_ms, delay);
    } catch (err) {
      log("# Data parse error", true);
    }
  };

  S.ws.onerror = () => log("# WebSocket Error", true);

  S.ws.onclose = () => {
    clearInterval(S.ping);
    log("# WebSocket Disconnected");
    if (S.started) S.reconnect = setTimeout(startLiveListener, RECONNECT_MS);
  };
}

function stopLiveListener() {
  clearInterval(S.ping);
  clearTimeout(S.reconnect);
  if (S.ws) {
    S.ws.onclose = null;
    S.ws.close();
    S.ws = null;
    log("# Live Listener Stopped.");
  }
}

/* ========= Per-match state ========= */
const TEAMS = {};
const MS = {};
function ms(key) {
  return (MS[key] ||= {
    runs: 0, wk: 0, ov: "0.0", bat: null,
    seen: new Set(), has: false, lastBallAt: 0, evAt: 0, objTs: 0, lastCommId: null,
    phase: "idle", cur: null, timers: [], overNo: 0, dots: [], dotsOver: null, order: null,
    toss: "", result: "", review: "", sum: [], brk: "",
    day: null, session: null, dayLine: "", rawState: "", maxOv: 0
  });
}

const BREAKS = [
  { re: /innings break/, label: "INNINGS BREAK" },
  { re: /lunch/,         label: "LUNCH BREAK" },
  { re: /\btea\b/,       label: "TEA BREAK" },
  { re: /dinner/,        label: "DINNER BREAK" },
  { re: /drink/,         label: "DRINKS BREAK" },
  { re: /stump/,         label: null },
  { re: /time ?out|strategic/, label: "TIME OUT", skipTest: true },
  { re: /\brain\b/,      label: "RAIN DELAY" },
  { re: /bad ?light/,    label: "BAD LIGHT" },
  { re: /delay|interrupt|suspend/, label: "PLAY DELAYED" }
];

const isTest = () => /test|first[\s-]?class/i.test(S.fmt || "");

function breakLabel(txt, st) {
  for (const b of BREAKS) {
    if (!b.re.test(txt)) continue;
    if (b.skipTest && isTest()) continue;
    return b.label ?? (st.day !== null ? `Day ${st.day}: Stumps` : "STUMPS");
  }
  return "";
}

function applyPlayState(m, key) {
  const st = ms(key);
  const dayN = Number(m.day);
  if (dayN > 0) st.day = dayN;
  const sesN = Number(m.session);
  if (sesN > 0) st.session = sesN;
  if (st.day !== null) st.dayLine = `Day ${st.day}${st.session === null ? "" : ` · Session ${st.session}`}`;

  const sig = String(m.game || "");
  if (!sig) return;
  if (sig !== st.rawState) { st.rawState = sig; log(`# play state: ${esc(sig)}`); }
  const brk = breakLabel(sig.toLowerCase().replace(/[_-]+/g, " "), st);
  st.brk = (brk && Date.now() - st.lastBallAt < 10000) ? "" : brk;
}

function normOv(ov) {
  const [o, b] = String(ov).split(".");
  const oi = parseInt(o, 10) || 0, bi = parseInt(b, 10) || 0;
  return bi >= 6 ? `${oi + 1}.0` : `${oi}.${bi}`;
}
const fmtOvers = ov => normOv(ov);
const ovVal = ov => {
  const [o, b] = String(ov).split(".");
  return (parseInt(o, 10) || 0) * 6 + (parseInt(b, 10) || 0);
};

function showStatus(key) {
  if (key !== S.matchKey) return;
  const st = ms(key);
  const el = EL.status;
  if (el) {
    const lines = [];
    if (st.toss && (st.has || STATUS[key] === "not_started")) lines.push(`<div>${esc(st.toss)}</div>`);
    if (STATUS[key] === "not_started" && START[key]) lines.push(`<div class="sub">Starts: ${esc(fmtStart(START[key]))}</div>`);
    if (STATUS[key] === "completed") (st.sum || []).forEach(t => lines.push(`<div class="sub">${esc(t)}</div>`));
    if (isTest() && st.dayLine && !st.result) lines.push(`<div class="sub">${esc(st.dayLine)}</div>`);
    el.innerHTML = lines.join("");
  }
  renderBall(key);
}

function renderLive(key) {
  const st = ms(key);
  if (!st.has) return;
  if (EL.score) EL.score.textContent = `${st.runs}-${st.wk}`;
  if (EL.overTxt) EL.overTxt.textContent = `${st.ov} OVER`;
  const nm = st.bat && TEAMS[key]?.[st.bat];
  if (nm && EL.team) EL.team.textContent = nm;
}

/* ========= Ball box =========
   phases: idle → wait ("Ball....") → result → over / inn → wait ...
*/
function paintBall(info) {
  const el = EL.ball;
  if (!el) return;
  const k = info.idle ? "idle|" : `${info.kind || ""}|${info.text}`;
  if (el.dataset.k === k) return;
  el.dataset.k = k;
  S.cur = info;
  if (info.idle) {
    el.className = "ball idle";
    el.innerHTML = `<span>Ball<i>.</i><i>.</i><i>.</i><i>.</i></span>`;
  } else {
    el.className = `ball ${info.kind || ""}`;
    el.innerHTML = `<span>${esc(info.text)}</span>`;
  }
}

function maxOvers() {
  if (S.oversOverride > 0) return S.oversOverride;
  const mo = S.matchKey ? ms(S.matchKey).maxOv : 0;
  if (mo > 0) return mo;
  if (/T10/i.test(S.fmt)) return 10;
  if (/T20/i.test(S.fmt)) return 20;
  if (/ONE[\s_-]?DAY|ODI/i.test(S.fmt)) return 50;
  return 0;
}
function inningsEnded(st, prov) {
  const wk = prov ? prov.wk : st.wk;
  const ov = prov ? prov.ov : st.ov;
  if (wk >= 10) return true;
  const mo = maxOvers();
  return mo > 0 && ovVal(ov) >= mo * 6;
}
function breakApplies(st) {
  if (isTest()) return st.order == null || st.order < 4;
  return st.order == null || st.order === 1;
}

function ballView(key) {
  const st = ms(key);
  if (st.result) return { text: st.result, kind: "msg" };
  if (st.review) return { text: st.review, kind: "review" };
  if (st.brk) return { text: st.brk, kind: "brk" };
  switch (st.phase) {
    case "wait":    return { idle: true };
    case "freehit": return { text: "FREE HIT", kind: "freehit" };
    case "result":  if (st.cur) return st.cur; break;
    case "over":    return { text: `${st.overNo} OVER COMPLETE`, kind: "overdone" };
    case "inn":     return { text: "INNINGS BREAK", kind: "brk" };
  }
  if (STATUS[key] === "not_started" && !st.has) return { text: "UPCOMING", kind: "msg" };
  if (STATUS[key] === "completed") return { text: "MATCH FINISHED", kind: "msg" };
  if (!st.has && st.toss) return { text: st.toss, kind: "msg" };
  return { idle: true };
}

function renderBall(key) {
  if (!S.matchKey || key !== S.matchKey) return;
  paintBall(ballView(key));
  renderDots(key);
}

function setBallIdle() {
  if (!S.matchKey) { paintBall({ text: "NULL", kind: "msg" }); renderDots(null); return; }
  renderBall(S.matchKey);
}

function clearBallTimers(st) { (st.timers || []).forEach(clearTimeout); st.timers = []; }
function later(st, delay, fn) { st.timers.push(setTimeout(fn, delay)); }

function resetBall() {
  const st = S.matchKey ? ms(S.matchKey) : null;
  if (st) { clearBallTimers(st); st.phase = "idle"; }
  setBallIdle();
}

/* ========= Over dots ========= */
function dotEntry(info, legal) {
  let t = info.text, c = "r";
  switch (info.kind) {
    case "wicket":  t = "W"; c = "w"; break;
    case "six":     t = "6"; c = "s"; break;
    case "four":    t = "4"; c = "f"; break;
    case "noball":  t = info.text.replace("NO BALL", "NB").replace(/\s*\+/, ""); c = "x"; break;
    case "wide":    t = info.text.replace("WIDE", "Wd").replace(/\s*\+/, ""); c = "x"; break;
    case "dot":     t = "0"; c = "d"; break;
  }
  return { t, c, legal };
}

function ordinal(n) {
  const v = n % 100;
  if (v >= 11 && v <= 13) return n + "th";
  return n + (["th", "st", "nd", "rd"][n % 10] || "th");
}
function curOverNo(st) {
  if (st.dotsOver != null) return Number(st.dotsOver) + 1;
  return Math.floor(ovVal(st.ov) / 6) + 1;
}

function renderDots(key) {
  const no = EL.overNo;
  if (no) {
    const t = key && isLiveKey(key) ? `${ordinal(curOverNo(ms(key)))} OVER` : "-- OVER";
    if (no.textContent !== t) no.textContent = t;
  }
  const el = EL.overDots;
  if (!el) return;
  const dots = key && isLiveKey(key) ? ms(key).dots : [];
  const legal = dots.filter(x => x.legal).length;
  let h = dots.map(x => `<span class="od ${x.c}">${esc(x.t)}</span>`).join("");
  for (let i = legal; i < 6; i++) h += `<span class="od e">&middot;</span>`;
  if (el.dataset.h !== h) { el.dataset.h = h; el.innerHTML = h; }
}

/* ========= Result display ========= */
function showResult(key, info, overDone, overNo, prov) {
  const st = ms(key);
  clearBallTimers(st);
  st.phase = "result";
  st.cur = info;

  let t = 0;
  if (info.kind === "wicket" && info.sub) {
    if (WICKET_TYPE_DELAY_MS > 0) {
      t += WICKET_TYPE_DELAY_MS;
      later(st, t, () => { st.cur = { text: info.sub, kind: "wickettype" }; renderBall(key); });
    } else {
      st.cur = { text: info.sub, kind: "wickettype" };
    }
  }
  const innEnd = inningsEnded(st, prov) && breakApplies(st);
  if (overDone || innEnd) {
    st.overNo = overNo || curOverNo(st);
    const at = OVER_DELAY_MS > 0 ? t + OVER_DELAY_MS : 0;
    const go = (phase, when) => {
      if (when > 0) later(st, when, () => { st.phase = phase; renderBall(key); });
      else st.phase = phase;
    };
    if (overDone) go("over", at);
    if (innEnd) go("inn", overDone ? at + INN_BREAK_DELAY_MS : at);
  }
  renderBall(key);
}

function wicketLabel(t) {
  const s = t ? String(t).replace(/[_-]+/g, " ").trim().toUpperCase() : "";
  if (s === "RUNOUT") return "RUN OUT";
  if (s === "HITWICKET") return "HIT WICKET";
  return s;
}

function setWicketType(key, label) {
  const st = ms(key);
  label = wicketLabel(label);
  if (!label) return;
  if (st.phase === "result" && st.cur && (st.cur.kind === "wicket" || st.cur.kind === "wickettype")) {
    clearBallTimers(st);
    st.cur = { text: label, kind: "wickettype" };
    renderBall(key);
  }
}

/* ========= Ball event handler — FAST PATH ========= */
const DISMISS_RE = /^(bowled|lbw|caught|c ?& ?b|caught behind|caught and bowled|run ?out|stumped?|hit ?wicket|retired|handled|obstruct|timed ?out|hit the ball twice)/i;
const IGNORE_RE  = /^(over|misfield|new batter|toss update|dropped|overthrow)/i;

function resultFromEvent(low, ev) {
  if (/^(dot|0)( ball)?$/.test(low)) return { legal: true, info: { text: "0", kind: "dot" } };
  const num = /^(\d+)$/.exec(low);
  if (num) {
    const n = Number(num[1]);
    if (n === 4) return { legal: true, info: { text: "4", kind: "four" } };
    if (n === 6) return { legal: true, info: { text: "6", kind: "six" } };
    return { legal: true, info: { text: String(n), kind: "run" } };
  }
  if (/^four/.test(low)) return { legal: true, info: { text: "4", kind: "four" } };
  if (/^six/.test(low))  return { legal: true, info: { text: "6", kind: "six" } };
  if (/^(wicket|out)$/.test(low)) return { legal: true, info: { text: "WICKET", kind: "wicket" } };
  if (/^wide/.test(low)) return { legal: false, info: { text: "WIDE", kind: "wide" } };
  if (/^no[\s_-]?ball|^nb\b/.test(low)) return { legal: false, info: { text: "NO BALL", kind: "noball" } };
  if (/^leg[\s_-]?bye/.test(low) || /^bye/.test(low)) return { legal: true, info: { text: ev.toUpperCase(), kind: "run" } };
  return null;
}

function handleChalu(key, e, st) {
  const inn = Number(e.inn) || 0;
  if (inn > 0 && st.order != null && inn !== st.order) newInnings(st);
  if (inn > 0) st.order = inn;

  const ovIdx = parseInt(e.over, 10);
  if (!isNaN(ovIdx) && (st.dotsOver == null || ovIdx > st.dotsOver)) {
    st.dots = [];
    st.dotsOver = ovIdx;
  }
  clearBallTimers(st);
  st.brk = ""; st.review = "";
  st.lastBallAt = Date.now();
  st.phase = S.isFreeHitNext ? "freehit" : "wait";    // ← "Ball...." আসে এখান থেকে
  renderBall(key);
}

function handleEvent(key, e) {
  if (!S.matchKey || key !== S.matchKey) return;
  const st = ms(key);
  const ev = String(e.ev || "").trim();
  const low = ev.toLowerCase();
  st.evAt = Date.now();

  // "Ball Chalu" / "Ball Start" → idle "Ball...."
  if (/^ball (chalu|start)/.test(low)) return handleChalu(key, e, st);

  // dismissal type (LBW, Bowled...) → WICKET replace
  if (DISMISS_RE.test(low)) { setWicketType(key, ev); return; }

  // dot/1/4/6/Wicket/wide/nb/bye → result
  const r = resultFromEvent(low, ev);
  if (r) {
    const info = r.info;
    const rawOv = e.over != null ? String(e.over) : "";
    const ballNo = parseInt(rawOv.split(".")[1], 10);
    const ovIdx = parseInt(rawOv, 10);
    const overDone = r.legal && ballNo === 6;
    const overNo = isNaN(ovIdx) ? 0 : ovIdx + 1;

    let prov = null;
    if (r.legal && rawOv) {
      const evOv = normOv(rawOv);
      if (ovVal(evOv) > ovVal(st.ov)) prov = { ov: evOv, wk: st.wk + (info.kind === "wicket" ? 1 : 0) };
    }

    if (!isTest()) {
      if (info.kind === "noball") S.isFreeHitNext = true;
      else if (r.legal) S.isFreeHitNext = false;
    }

    if (r.legal && !isNaN(ovIdx) && !isNaN(ballNo) && ovIdx === st.dotsOver
        && st.dots.filter(x => x.legal).length < ballNo) {
      st.dots.push({ id: `p${ovIdx}.${ballNo}`,
                     ord: (st.dots.length ? st.dots[st.dots.length - 1].ord || 0 : 0) + 1,
                     ...dotEntry(info, true) });
    }

    st.lastBallAt = Date.now();
    st.brk = ""; st.review = "";
    showResult(key, info, overDone, overNo, prov);
    return;
  }

  if (IGNORE_RE.test(low)) return;

  if (/review|drs/.test(low)) {
    st.review = /bat/.test(low) ? "BATTER REVIEW" : /bowl|field/.test(low) ? "BOWLER REVIEW" : "REVIEW";
    log(`# review: ${esc(ev)}`);
    renderBall(key);
    return;
  }

  const bl = breakLabel(low.replace(/[_-]+/g, " "), st);
  if (bl) { st.brk = bl; log(`# break: ${esc(ev)}`); renderBall(key); return; }

  const uk = `${ev}|${e.code}`;
  if (!S.unk.has(uk)) {
    S.unk.add(uk);
    log(`# unknown ball_event: "${esc(ev)}" (code ${esc(e.code)}) over ${esc(e.over)}`, true);
  }
}

/* ========= Match object handler (score/team/commentary) ========= */
function newInnings(st) {
  st.wk = 0; st.runs = 0; st.ov = "0.0";
  st.seen.clear(); st.dots = []; st.dotsOver = null; st.lastCommId = null;
  S.isFreeHitNext = false;
}

function infoFromComm(e) {
  if (e.w) return { text: "WICKET", kind: "wicket", sub: wicketLabel(e.dis) };
  if (e.nb > 0) return { text: e.bat > 0 ? `NO BALL +${e.bat}` : "NO BALL", kind: "noball" };
  if (e.wd > 0) { const wr = Math.max(0, e.run - 1); return { text: wr > 0 ? `WIDE +${wr}` : "WIDE", kind: "wide" }; }
  if (e.six)  return { text: "6", kind: "six" };
  if (e.four) return { text: "4", kind: "four" };
  if (e.by > 0) return { text: `${e.by}B`, kind: "run" };
  if (e.lb > 0) return { text: `${e.lb}LB`, kind: "run" };
  return { text: String(e.run), kind: e.run === 0 ? "dot" : "run" };
}
const commLegal = e => !(e.wd > 0 || e.nb > 0);

function applyComm(key, m) {
  const st = ms(key);
  const list = (m.comm || []).slice().sort((a, b) => Number(a.id) - Number(b.id));
  if (!list.length) return;

  const maxOv = Math.max(...list.map(e => e.ov));
  if (st.dotsOver == null || maxOv >= st.dotsOver) {
    st.dots = list.filter(e => e.ov === maxOv)
      .map(e => ({ id: e.id, ord: e.x || 0, ...dotEntry(infoFromComm(e), commLegal(e)) }));
    st.dotsOver = maxOv;
  }

  const newest = list[list.length - 1];
  const isNewBall = newest.id !== st.lastCommId;
  const first = st.lastCommId == null;
  st.lastCommId = newest.id;

  if (isNewBall && !isTest()) {
    if (newest.nb > 0) S.isFreeHitNext = true;
    else if (newest.wd === 0) S.isFreeHitNext = false;
  }
  if (newest.w && newest.dis) setWicketType(key, newest.dis);

  if (isNewBall && !first && (!st.evAt || Date.now() - st.evAt > FALLBACK_QUIET_MS)) {
    const info = infoFromComm(newest);
    st.lastBallAt = Date.now();
    st.brk = "";
    showResult(key, info, commLegal(newest) && newest.b === 6, newest.ov + 1, null);
  }
}

function applySummary(key, m) {
  const st = ms(key);
  const parts = [];
  let last = null;
  ["a", "b"].forEach(side => {
    const t = m[side];
    if (!t || !t.sc) return;
    parts.push(`${t.name || side.toUpperCase()} ${String(t.sc).replace(/^\*/, "")}`);
    const re = /(\d+)\s*\/\s*(\d+)\s*\(\s*([\d.]+)\s*ov/gi;
    let mm, lm = null;
    while ((mm = re.exec(t.sc))) lm = mm;
    if (lm) last = { side, runs: Number(lm[1]), wk: Number(lm[2]), ov: fmtOvers(lm[3]) };
  });
  st.sum = parts;
  if (last && m.status !== "started" && !(m.inn > 0 && m.score)) {
    st.runs = last.runs; st.wk = last.wk; st.ov = last.ov; st.bat = last.side; st.has = true;
  }
}

function handleMatch(key, m) {
  if (!key || (m.id && String(m.id) !== key)) return;
  const st = ms(key);
  if (m.ts && st.objTs && m.ts < st.objTs) return;
  if (m.ts) st.objTs = m.ts;

  if (m.a || m.b) TEAMS[key] = { a: m.a?.name || TEAMS[key]?.a, b: m.b?.name || TEAMS[key]?.b };
  if (m.fmt && !S.fmt) { S.fmt = m.fmt; if ($("type")) $("type").value = m.fmt; }
  if (m.start) START[key] = m.start;
  if (m.status) setMatchStatus(key, m.status);

  const mo = Number(m.max) || 0;
  if (mo > 0 && mo !== st.maxOv) { st.maxOv = mo; log(`# overs limit: ${mo}`); }

  if (m.toss) st.toss = String(m.toss);
  if (m.result) {
    if (m.result !== st.result && key === S.matchKey) log(`# result: ${esc(m.result)}`);
    st.result = String(m.result);
  }
  applySummary(key, m);
  applyPlayState(m, key);

  const inn = Number(m.inn) || 0;
  if (inn > 0 && m.score) {
    if (st.order != null && inn !== st.order) newInnings(st);
    st.order = inn;
    st.runs = m.score.runs;
    st.wk = m.score.wk;
    st.ov = fmtOvers(m.score.ov);
    if (m.bat) st.bat = m.bat;
    st.has = true;
  }

  applyComm(key, m);

  if (m.lw && m.lw.dis && st.phase === "result" && st.cur && st.cur.kind === "wicket"
      && Number(m.lw.sc) === st.runs) {
    setWicketType(key, m.lw.dis);
  }

  showStatus(key);
  renderLive(key);
}

/* ========= Match status ========= */
const STATUS = {};
const START = {};
const STATUS_VIEW = {
  started:     ["LIVE",     "var(--green)"],
  not_started: ["UPCOMING", "var(--yellow)"],
  completed:   ["FINISHED", "var(--text-muted)"],
  cancelled:   ["CANCELLED", "var(--red)"]
};
const statusView = s => STATUS_VIEW[s] || [String(s || "-").toUpperCase(), "var(--cyan)"];
const isLiveKey = key => !STATUS[key] || STATUS[key] === "started";

function fmtStart(v) {
  const n = Number(v);
  if (!n) return String(v);
  const d = new Date(n > 1e11 ? n : n * 1000);
  return isNaN(d) ? String(v) : d.toLocaleString();
}

function setMatchStatus(key, s) {
  s = String(s || "").toLowerCase();
  if (!STATUS_VIEW[s] || STATUS[key] === s) return;
  STATUS[key] = s;
  const [txt, col] = STATUS_VIEW[s];
  document.querySelectorAll("#fixture tr").forEach(tr => {
    if (tr.dataset.key !== key) return;
    tr.dataset.status = s;
    const sp = tr.querySelector("td:last-child span");
    if (sp) { sp.textContent = txt; sp.style.color = col; }
  });
  if (key === S.matchKey) log(`# status: ${esc(txt)}`);
}

function resetViewport() {
  if (EL.score) EL.score.textContent = "0-0";
  if (EL.overTxt) EL.overTxt.textContent = "0.0 OVER";
  if (EL.team) EL.team.textContent = "--";
  if (EL.status) EL.status.innerHTML = "";
}

async function fetchBaseline(key) {
  if (!S.matchKey || key !== S.matchKey) return;
  try {
    const res = await fetch(`${API_BASE}/api/match/${encodeURIComponent(key)}`);
    if (!res.ok) return;
    const json = await res.json();
    if (json.data && key === S.matchKey) handleMatch(key, json.data);
  } catch (e) {}
}

let _hinted = false;
function updateViewport(m) {
  if (!S.matchKey) {
    if (!_hinted) { log("# Select a match from Fixture."); _hinted = true; }
    if (EL.status) EL.status.textContent = "Select a match from Fixture";
    setBallIdle();
    return;
  }
  if (m.id && String(m.id) !== S.matchKey) return;

  if (m.t === "ev") handleEvent(S.matchKey, m);        // ← ball event: সাথে সাথে
  else if (m.t === "match") handleMatch(S.matchKey, m); // match object: score/comm
}

function startAll() {
  if (S.started) return;
  S.started = true;
  startLiveListener();
  setActive(!!S.matchKey);
  log("# Live listener started.");
}

function stopAll() {
  S.started = false;
  stopLiveListener();
  pieOff();
  S.prevMs = null;
  setActive(false);
  resetBall();
  log("# Live listener stopped.");
}

$("bStop")?.addEventListener("click", stopAll);
$("bRestart")?.addEventListener("click", () => { stopAll(); setTimeout(startAll, 300); });
$("bAuto")?.addEventListener("click", () => {
  S.auto = !S.auto;
  $("bAuto").style.background = S.auto ? "var(--cyan)" : "";
  $("bAuto").style.color = S.auto ? "#000" : "";
  if (S.auto && !S.started) startAll();
});

window.addEventListener("DOMContentLoaded", () => {
  const canvasEl = $("latChart");
  if (canvasEl && window.Chart) {
    chart = new Chart(canvasEl, {
      type: "pie",
      data: {
        labels: ["Latency A", "Latency B"],
        datasets: [{ data: [1, 1], backgroundColor: [GREY, GREY], borderColor: "#fff", borderWidth: 1 }]
      },
      options: {
        responsive: false,
        animation: false,
        plugins: { legend: { display: false }, tooltip: { enabled: false } }
      }
    });
  }
  loadSettings();
  loadFixtures();
  setInterval(() => loadFixtures(true), 60000);
  setBallIdle();
});
