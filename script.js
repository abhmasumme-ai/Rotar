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
const OVER_DELAY_MS = 0;      // 0 = OVER COMPLETE / INNINGS BREAK ekdom shathe (score + ball box + chips ek tick e). Last ball result dekhate chaile 800-2000 dao
const INN_BREAK_DELAY_MS = 800;      // 1st innings shesh (over complete hole): OVER COMPLETE -> (ei delay) -> INNINGS BREAK
const WICKET_TYPE_DELAY_MS = 2000;   // WICKET blink -> dismissal type (CAUGHT / BOWLED ...)
const POLL_MS = 15000;      

const $ = id => document.getElementById(id);
const esc = s => String(s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

const S = {
  started: false,
  ws: null,
  ping: null,
  reconnect: null,
  prevMs: null,
  label: "",
  matchKey: "",
  active: true,
  auto: false,
  idle: true,
  fmt: "",     
  poll: null,  
  cur: null,
  isFreeHitNext: false,
  // rain-e over kome gele: panel URL e ?overs=15 dile 15 over e INNINGS BREAK dekhabe
  oversOverride: Number(new URLSearchParams((typeof location !== "undefined" && location.search) || "").get("overs")) || 0
};

/* ========= Terminal ========= */
const term = $("terminal");
function log(msg, err) {
  if (!term) return;
  const li = document.createElement("li");
  const time = new Date().toLocaleTimeString();
  if (err) { li.className = "err"; li.textContent = `[${time}] ${msg}`; }
  else li.innerHTML = `[${time}] ${msg}`;
  const atBottom = term.scrollTop + term.clientHeight >= term.scrollHeight - 20;
  term.appendChild(li);
  while (term.children.length > MAX_LOG) term.firstChild.remove();
  if (atBottom) term.scrollTop = term.scrollHeight;
}
function matchLabel() { return S.label || $("matchTeams")?.textContent || "match"; }

/* ========= Bars & Latency Chart ========= */
const BAR_MIN = 100, BAR_MAX = 5000;
function setBar(id, ms) {
  const el = $(id);
  if (!el) return;
  const out = ms > BAR_MAX;
  const h = 20 + (Math.min(BAR_MAX, Math.max(BAR_MIN, ms)) - BAR_MIN) / (BAR_MAX - BAR_MIN) * 20;
  el.style.height = h + "px";
  const t = $(id + "Txt");
  if (t) {
    t.textContent = out ? "OUT TO LIMIT" : Math.round(ms) + "ms";
    t.style.color = out ? "var(--red)" : "";
  }
}

const GREY = "#555";
let chart = null;

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
  setInterval(() => loadFixtures(true), 60000);   // fixture status (Live/Upcoming/Finished) refresh
  setBallIdle();
});

function setPie(vals, cols) {
  if (!chart) return;
  chart.data.datasets[0].data = vals;
  chart.data.datasets[0].backgroundColor = cols;
  chart.update();
}

function pieOff() {
  setPie([1, 1], [GREY, GREY]);
  if ($("lgR")) $("lgR").textContent = "0ms";
  if ($("lgB")) $("lgB").textContent = "0ms";
}

function pieOn(r, b) {
  r = Math.max(0, r);
  b = Math.max(0, b);
  setPie([Math.min(r, MAX_MS) || 1, Math.min(b, MAX_MS) || 1], ["#ff2a2a", "#17f0e0"]);
  if ($("lgR")) $("lgR").textContent = Math.round(r) + "ms";
  if ($("lgB")) $("lgB").textContent = Math.round(b) + "ms";
}

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
    }
  } catch (err) {
    log("# Error fetching matches fixture", true);
  }
}

function renderMatches(matches) {
  const tbody = $("fixture");
  if (!tbody) return;
  const order = { started: 0, not_started: 1, completed: 2 };
  matches = [...matches].sort((a, b) => (order[a.status] ?? 3) - (order[b.status] ?? 3));

  let html = "";
  matches.forEach(m => {
    const matchId = m.key || m.id || "N/A";
    TEAMS[matchId] = { a: m.teams?.a?.name, b: m.teams?.b?.name };
    const title = m.title || m.name || `${m.team1?.name || "Team 1"} vs ${m.team2?.name || "Team 2"}`;
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
  const st0 = ms(key); st0.dots = []; st0.dotsOver = null; st0.order = null; st0.bfOver = null; st0.lastDetOv = null;
  S.isFreeHitNext = false;
  if ($("matchKey")) $("matchKey").value = key;
  if ($("type")) $("type").value = type || "";
  if ($("matchTeams")) $("matchTeams").textContent = title;
  log(`Selected match: ${esc(title)}`);
  resetBall();
  startAll();        
  setActive(true);
  resetViewport();   // ager match er score/team/status muche felo
  showStatus(key);
  const st = ms(key);
  if (st.has) renderLive(key);
  fetchBaseline(key);
  if (isLiveKey(key)) backfillOver(key, null);   // mid-over start: ager ball gulo chips e anbe
}

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
  };

  S.ws.onmessage = (event) => {
    if (event.data === "pong") return;
    try {
      const payload = JSON.parse(event.data);
      // onno match er push latency chart e dhukte debo na
      const pk = payload.data?.matchkey;
      if (S.matchKey && pk && pk !== S.matchKey) return;
      let delay = 10;
      if (payload._server_ts) delay = Math.max(1, Math.round(Date.now() - payload._server_ts));

      updateViewport(payload);          // age viewport (score + ball box + chips), tarpor latency chart
      setBar("barOdds", delay);
      if (typeof payload._lag_ms === "number") pieOn(payload._lag_ms, delay);
    } catch (err) {
      log("# Data parse error", true);
    }
  };

  S.ws.onerror = () => log("# WebSocket Error", true);

  S.ws.onclose = () => {
    clearInterval(S.ping);
    log("# WebSocket Disconnected");
    if (S.started) {
      S.reconnect = setTimeout(startLiveListener, RECONNECT_MS);
    }
  };
}

function stopLiveListener() {
  clearInterval(S.ping);
  clearTimeout(S.reconnect);
  if (S.ws) {
    S.ws.onclose = null;
    S.ws.close();
    S.ws = null;
    log("# Webhook Listener Stopped.");
  }
}

const TEAMS = {};
const MS = {};
function ms(key) {
  return (MS[key] ||= {
    runs: 0, wk: 0, ov: "0.0", innings: null, bat: null,
    seen: new Set(), labels: {}, has: false, matchAt: 0, lastBallAt: 0,
    phase: "idle", cur: null, timers: [], overNo: 0, dots: [], dotsOver: null, order: null, bfOver: null, bfBusy: false,
    toss: "", result: "", review: "", sum: [],
    brk: "",        
    day: null, session: null, dayLine: "",   
    rawState: ""    
  });
}

const STATE_PATHS = ["play_status", "play.play_status", "play.status", "play.live.status", "play.live.play_status", "live.status", "live.play_status", "status_note"];
const DAY_PATHS = ["play.day_number", "play.live.day_number", "play.day", "day_number", "live.day_number", "live.day"];
const SESSION_PATHS = ["play.session_number", "play.live.session_number", "play.session", "session_number", "live.session_number", "live.session"];

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

const OVERS_LIMIT_PATHS = ["overs_limit", "limited_overs", "match_overs", "max_overs", "overs", "rules.overs", "play.overs_limit"];
function applyOversLimit(obj, key) {
  const st = ms(key);
  const v = pickFirst(obj, OVERS_LIMIT_PATHS);
  const n = Array.isArray(v) ? Number(v[0]) : Number(v);
  if (n >= 1 && n <= 100 && n !== st.maxOv) {
    st.maxOv = n;
    log(`# overs limit (match data): ${n}`);
  }
}

const getPath = (o, p) => p.split(".").reduce((a, k) => (a == null ? a : a[k]), o);
const pickFirst = (o, paths) => {
  for (const p of paths) {
    const v = getPath(o, p);
    if (v !== undefined && v !== null && v !== "") return v;
  }
  return null;
};
const isTest = () => /test/i.test(S.fmt || "");

function applyPlayState(obj, key) {
  const st = ms(key);

  const day = pickFirst(obj, DAY_PATHS);
  const ses = pickFirst(obj, SESSION_PATHS);
  if (day !== null) st.day = day;
  if (ses !== null) st.session = ses;
  if (st.day !== null) {
    const sesTxt = st.session === null ? "" : (/^\d+$/.test(String(st.session)) ? ` · Session ${st.session}` : ` · ${st.session}`);
    st.dayLine = `Day ${st.day}${sesTxt}`;
  }

  const raw = STATE_PATHS.map(p => getPath(obj, p)).filter(v => typeof v === "string" && v);
  if (!raw.length) return;
  const sig = raw.join(" | ");
  if (sig !== st.rawState) {
    st.rawState = sig;
    log(`# play state: ${esc(sig)}`);
  }
  const txt = raw.join(" ").toLowerCase().replace(/[_-]+/g, " ");
  let brk = "";
  for (const b of BREAKS) {
    if (!b.re.test(txt)) continue;
    if (b.skipTest && isTest()) continue;
    brk = b.label ?? (st.day !== null ? `Day ${st.day}: Stumps` : "STUMPS");
    break;
  }
  st.brk = (brk && Date.now() - st.lastBallAt < 10000) ? "" : brk;
}

function deepGet(obj, key, depth = 0) {
  if (!obj || typeof obj !== "object" || depth > 6) return undefined;
  if (Object.prototype.hasOwnProperty.call(obj, key) && obj[key] != null) return obj[key];
  for (const k of Object.keys(obj)) {
    const v = deepGet(obj[k], key, depth + 1);
    if (v !== undefined) return v;
  }
  return undefined;
}

function normOv(ov) {
  const [o, b] = String(ov).split(".");
  const oi = parseInt(o, 10) || 0, bi = parseInt(b, 10) || 0;
  return bi >= 6 ? `${oi + 1}.0` : `${oi}.${bi}`;
}
const fmtOvers = ov => normOv(Array.isArray(ov) ? `${ov[0]}.${ov[1] ?? 0}` : ov);
const ovVal = ov => {
  const [o, b] = String(ov).split(".");
  return (parseInt(o, 10) || 0) * 6 + (parseInt(b, 10) || 0);
};

function parseDisplayScore(s) {
  const m = /(\d+)\s*\/\s*(\d+)\s+in\s+([\d.]+)\s+overs?/i.exec(String(s || ""));
  return m ? { runs: Number(m[1]), wk: Number(m[2]), ov: normOv(m[3]), raw: m[3] } : null;
}

function tossText(toss, key) {
  if (!toss) return "";
  if (typeof toss === "string") return toss;
  if (typeof toss !== "object") return "";
  const direct = toss.str || toss.msg || toss.text;
  if (direct) return String(direct);
  const w = toss.winner;
  const name = (TEAMS[key] || {})[w] || w;
  const el = toss.elected || toss.decision || toss.choice;
  if (name) return `${name} won the toss${el ? " and elected to " + String(el).replace(/_/g, " ") : ""}`;
  return "";
}

function showStatus(key) {
  if (key !== S.matchKey) return;
  const st = ms(key);
  const el = $("status");
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
  if ($("score")) $("score").textContent = `${st.runs}-${st.wk}`;
  if ($("overTxt")) $("overTxt").textContent = `${st.ov} OVER`;
  const nm = st.bat && TEAMS[key]?.[st.bat];
  if (nm && $("team")) $("team").textContent = nm;
}

/* ========= Ball box =========
   phases (per match): idle -> wait ("Ball....") -> result (stays) -> over / inn (after delay) -> wait ...
   priority shown in #ball: match result > break/time-out/drinks > phase
*/
function paintBall(info) {
  const el = $("ball");
  if (!el) return;
  const k = info.idle ? "idle|" : `${info.kind || ""}|${info.text}`;
  if (el.dataset.k === k) return;      // same thing already on screen: don't restart animation
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
  if (S.oversOverride > 0) return S.oversOverride;                  // ?overs=15
  const mo = S.matchKey ? ms(S.matchKey).maxOv : 0;
  if (mo > 0) return mo;                                           // match data theke (reduced overs)
  if (/T10/i.test(S.fmt)) return 10;
  if (/T20/i.test(S.fmt)) return 20;
  if (/ONE[\s_-]?DAY|ODI/i.test(S.fmt)) return 50;
  return 0;
}
function inningsEnded(st) {
  if (st.wk >= 10) return true;
  const mo = maxOvers();
  return mo > 0 && ovVal(st.ov) >= mo * 6;
}
function breakApplies(st) {
  // limited overs: only after 1st innings. Test: after innings 1-3.
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
  if (!S.matchKey) {
    paintBall({ text: "NULL", kind: "msg" });
    renderDots(null);
    return;
  }
  renderBall(S.matchKey);
}

function clearBallTimers(st) { (st.timers || []).forEach(clearTimeout); st.timers = []; }
function later(st, ms, fn) { st.timers.push(setTimeout(fn, ms)); }

function resetBall() {
  const st = S.matchKey ? ms(S.matchKey) : null;
  if (st) { clearBallTimers(st); st.phase = "idle"; }
  setBallIdle();
}

/* ========= Over dots (1 0 4 W 1 .) ========= */
function dotEntry(d, info) {
  const bt = String(d.ball_type || "normal").toLowerCase();
  const legal = !/wide|no_?ball/.test(bt);
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

/* ========= Backfill: current over er ager ball gulo (mid-over start) ========= */
async function backfillOver(key, wantOv) {
  const st = ms(key);
  if (st.bfBusy) return;
  st.bfBusy = true;
  try {
    const res = await fetch(`${API_BASE}/api/ball-by-ball/${encodeURIComponent(key)}`);
    const json = await res.json();

    // response er moddhe jekhanei ball object (overs[] + batsman/team_score) ache, shob tule ana
    const all = [];
    const walk = (o, depth = 0) => {
      if (!o || typeof o !== "object" || depth > 10) return;
      if (Array.isArray(o.overs) && o.overs.length >= 2 && (o.batsman || o.team_score)) { all.push(o); return; }
      Object.values(o).forEach(v => walk(v, depth + 1));
    };
    walk(json.data || json);

    let balls = all.filter(x => !st.innings || !x.innings || x.innings === st.innings);
    if (!balls.length) { log("# backfill: ball data paoa jayni (ball-by-ball format check korun)", true); return; }

    const target = wantOv != null ? wantOv : Math.max(...balls.map(x => Number(x.overs[0]) || 0));
    balls = balls.filter(x => Number(x.overs[0]) === target);
    if (balls.length > 1 && Number(balls[0].overs[1]) > Number(balls[balls.length - 1].overs[1])) balls.reverse();   // newest-first hole ulte
    balls = balls.map((x, i) => ({ x, i })).sort((a, b) => (Number(a.x.overs[1]) - Number(b.x.overs[1])) || (a.i - b.i)).map(o => o.x);

    // already onno over e chole gele (new over shuru) kichu korbo na
    if (st.dotsOver != null && st.dotsOver > target) return;

    const made = [];
    const seenIds = new Set();
    balls.forEach(x => {
      const id = String(x.key ?? x.ball_key ?? "");
      if (id && seenIds.has(id)) return;
      if (id) seenIds.add(id);
      made.push({ id, ord: Number(x.overs[1]) || 0, ...dotEntry(x, ballInfo(x)) });
    });

    const hasIds = made.every(e => e.id);
    let merged;
    if (hasIds) {
      // server er list ke base dhore, live (push) entry diye replace; server e na thaka live entry sheshe
      const liveMap = new Map(st.dots.filter(e => e.id).map(e => [e.id, e]));
      merged = made.map(e => liveMap.get(e.id) || e);
      st.dots.forEach(e => { if (!e.id || !made.some(m => m.id === e.id)) merged.push(e); });
    } else if (!st.dots.length) {
      merged = made;
    } else {
      return;   // id chhara safe merge shombhob na
    }

    st.dots = merged;
    st.dotsOver = target;
    log(`# backfill: ${made.length} ball loaded for over ${target + 1}`);
    renderDots(key);
  } catch (e) {
    log("# backfill failed", true);
  } finally {
    st.bfBusy = false;
  }
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
  const no = $("overNo");
  if (no) {
    const t = key && isLiveKey(key) ? `${ordinal(curOverNo(ms(key)))} OVER` : "-- OVER";
    if (no.textContent !== t) no.textContent = t;
  }
  const el = $("overDots");
  if (!el) return;
  const dots = key && isLiveKey(key) ? ms(key).dots : [];
  const legal = dots.filter(x => x.legal).length;
  let h = dots.map(x => `<span class="od ${x.c}">${esc(x.t)}</span>`).join("");
  for (let i = legal; i < 6; i++) h += `<span class="od e">&middot;</span>`;
  if (el.dataset.h !== h) { el.dataset.h = h; el.innerHTML = h; }
}

/* ========= Ball events ========= */
function showResult(key, info, overDone, overNo) {
  const st = ms(key);
  clearBallTimers(st);
  st.phase = "result";
  st.cur = info;

  let t = 0;
  // wicket: blink "WICKET", then (delay) show dismissal type in white until next ball's 1st push
  if (info.kind === "wicket" && info.sub) {
    if (WICKET_TYPE_DELAY_MS > 0) {
      t += WICKET_TYPE_DELAY_MS;
      later(st, t, () => { st.cur = { text: info.sub, kind: "wickettype" }; renderBall(key); });
    } else {
      st.cur = { text: info.sub, kind: "wickettype" };
    }
  }
  const innEnd = inningsEnded(st) && breakApplies(st);   // 1st innings shesh (over limit ba 10 wicket)
  if (overDone || innEnd) {
    st.overNo = overNo || curOverNo(st);
    const at = OVER_DELAY_MS > 0 ? t + OVER_DELAY_MS : 0;
    const go = (phase, when) => {
      if (when > 0) later(st, when, () => { st.phase = phase; renderBall(key); });
      else st.phase = phase;                             // ekhoni
    };
    if (overDone) go("over", at);                        // over shesh hole OVER COMPLETE
    // innings shesh: over shesh hole 800ms pore INNINGS BREAK; all-out mid-over (14.3) hole sorasori
    if (innEnd) go("inn", overDone ? at + INN_BREAK_DELAY_MS : at);
  }
  renderBall(key);              // score (renderLive) er shathe ek-i tick e: ball box + chips
}

function wicketLabel(t) {
  return t ? String(t).replace(/[_-]+/g, " ").trim().toUpperCase() : "";
}

function ballInfo(d) {
  const bt = String(d.ball_type || "normal").toLowerCase();

  if (d.team_score?.is_wicket || d.bowler?.is_wicket || d.wicket) {
    return { text: "WICKET", kind: "wicket", sub: wicketLabel(d.wicket?.wicket_type || d.wicket?.type) };
  }

  if (/no_?ball/.test(bt)) {
    const br = Number(d.batsman?.runs) || 0;
    return { text: br > 0 ? `NO BALL +${br}` : "NO BALL", kind: "noball" };
  }

  if (d.batsman?.is_six) return { text: "6", kind: "six" };
  if (d.batsman?.is_four) return { text: "4", kind: "four" };

  if (/wide/.test(bt)) {
    const wr = (Number(d.team_score?.runs) || 1) - 1;   // 1 ta penalty baad
    return { text: wr > 0 ? `WIDE +${wr}` : "WIDE", kind: "wide" };
  }

  const r = d.team_score?.runs ?? d.batsman?.runs ?? 0;
  return { text: String(r), kind: r === 0 ? "dot" : "run" };
}

// 1st push (match-related-update): only ball_key/over/batting_order -> "Ball....." until details arrive
function handleFirstPush(key, b) {
  if (!S.matchKey || key !== S.matchKey) return;
  const st = ms(key);
  const id = String(b.ball_key ?? "");
  if (id && st.seen.has(id)) return;            // details of this ball already came: not a new ball

  if (b.batting_order != null && st.order != null && b.batting_order !== st.order) {
    st.dots = []; st.dotsOver = null; S.isFreeHitNext = false;   // new innings
  }
  if (b.batting_order != null) st.order = b.batting_order;
  // new over: previous over already has 6 legal balls -> start fresh chips, over index +1
  // (b.over er meaning (0/1 based) nirbhorjoggo na, tai use kora hoyni)
  if (st.dots.filter(x => x.legal).length >= 6) {
    st.dots = [];
    if (st.dotsOver != null) st.dotsOver += 1;
  }

  clearBallTimers(st);
  st.brk = "";
  st.lastBallAt = Date.now();
  st.phase = S.isFreeHitNext ? "freehit" : "wait";
  renderBall(key);
}

function handleDelete(key, b) {
  if (!S.matchKey || key !== S.matchKey) return;
  const st = ms(key);
  const id = String(b.ball_key ?? b.detail?.key ?? "");
  if (!id) return;
  st.seen.delete(id);
  delete st.labels[id];
  const i = st.dots.findIndex(x => x.id === id);
  if (i >= 0) { st.dots.splice(i, 1); renderDots(key); }
}

// chips kom thakle (ball push miss/late) 1.2s pore ball-by-ball theke bhore dey, max 4 bar
function scheduleChipCheck(key, ovIdx, need, tries = 0) {
  const st = ms(key);
  const tag = `${st.order}:${ovIdx}:${need}:${tries}`;
  if (st.chkTag === tag) return;
  st.chkTag = tag;
  setTimeout(async () => {
    if (key !== S.matchKey || st.dotsOver !== ovIdx) return;                 // match/over bodle geche
    if (st.dots.filter(x => x.legal).length >= need) return;                 // thik ache
    if (tries >= 3) return;
    await backfillOver(key, ovIdx);
    scheduleChipCheck(key, ovIdx, need, tries + 1);
  }, tries === 0 ? 1200 : 2500);
}

// 2nd push (match-detail-update): ball result
function handleBall(key, b) {
  if (!S.matchKey || key !== S.matchKey) return;

  const d = b.detail;
  if (!d || typeof d !== "object") return;

  const st = ms(key);

  if (d.innings && st.innings && d.innings !== st.innings) {
    st.wk = 0; st.runs = 0; st.ov = "0.0"; st.seen.clear(); st.labels = {};
    st.dots = []; st.dotsOver = null; st.lastDetOv = null;
    S.isFreeHitNext = false;
  }
  if (d.innings) st.innings = d.innings;
  if (d.batting_team) st.bat = d.batting_team;
  if (b.batting_order != null) st.order = b.batting_order;

  const id = String(b.ball_key ?? d.key ?? "");
  const isNew = !!id && !st.seen.has(id);
  if (isNew) st.seen.add(id);

  const ds = parseDisplayScore(d.display_score);
  const rawOv = Array.isArray(d.overs) ? `${d.overs[0]}.${d.overs[1] ?? 0}` : (ds ? ds.raw : null);
  const ballOv = rawOv ? normOv(rawOv) : null;
  const ovIdx = Array.isArray(d.overs) && d.overs[0] != null ? Number(d.overs[0]) : null;
  // purono over er ball (correction/re-send) current over e dhukbe na
  const chipOk = ovIdx == null || st.dotsOver == null || ovIdx >= st.dotsOver;
  // score/over (blue): kokhono pichhabe na
  const scoreLatest = (!ballOv || ovVal(ballOv) >= ovVal(st.ov)) && chipOk;
  // ball box: ei detail push er moddhe shobcheye notun ball hole tabei dekhabe
  const ballV = ballOv ? ovVal(ballOv) : null;
  const boxLatest = chipOk && (ballV == null || st.lastDetOv == null || ballV >= st.lastDetOv);
  if (ballV != null && (st.lastDetOv == null || ballV > st.lastDetOv)) st.lastDetOv = ballV;

  if (scoreLatest) {
    if (ds) {
      st.runs = ds.runs; st.wk = ds.wk; st.ov = ds.ov; st.has = true;
    } else if (ballOv) {
      st.ov = ballOv;
    }
  }

  const bt = String(d.ball_type || "").toLowerCase();
  const legal = !/wide|no_?ball/i.test(bt);
  const overDone = !!rawOv && legal && parseInt(rawOv.split(".")[1], 10) === 6;
  const overNo = rawOv ? (parseInt(rawOv, 10) || 0) + 1 : 0;

  const info = ballInfo(d);
  const lab = info.text + "|" + (info.sub || "");
  const prevLabel = id ? st.labels[id] : undefined;
  if (id) st.labels[id] = lab;
  st.lastBallAt = Date.now();
  st.brk = "";

  // free hit: next delivery after a no-ball (carries over a wide)
  if (isNew && !isTest()) {
    if (/no_?ball/.test(bt)) S.isFreeHitNext = true;
    else if (legal && S.isFreeHitNext) S.isFreeHitNext = false;
  }

  // this over's dots (late/out-of-order ball o chip e dhukbe)
  if (chipOk) {
    if (ovIdx != null) {
      if (st.dotsOver == null || ovIdx > st.dotsOver) st.dots = [];   // new over
      st.dotsOver = ovIdx;
    } else if (isNew && st.dots.filter(x => x.legal).length >= 6) {
      st.dots = [];                                                   // overs array nai: 6 ball hole new over
    }
    const entry = { id, ord: Number(Array.isArray(d.overs) ? d.overs[1] : 0) || 0, ...dotEntry(d, info) };
    const di = id ? st.dots.findIndex(x => x.id === id) : -1;
    if (di >= 0) st.dots[di] = entry;
    else if (isNew || !id) st.dots.push(entry);
    st.dots.sort((a, b) => (a.ord || 0) - (b.ord || 0));   // late ball hole-o thik jayga e boshbe

    // over er majkhane start korle: ager ball gulo missing -> server theke anbe (over pro ekbar)
    const bf = `${st.order}:${ovIdx}`;
    if (ovIdx != null && Array.isArray(d.overs) && st.bfOver !== bf
        && st.dots.filter(x => x.legal).length < (Number(d.overs[1]) || 0)) {
      st.bfOver = bf;
      backfillOver(key, ovIdx);
    }
    // kono ball miss hole (blue 14.3 kintu chip 2 ta): ektu opekkha kore server theke ene bharbe
    if (ovIdx != null && Array.isArray(d.overs)) scheduleChipCheck(key, ovIdx, Number(d.overs[1]) || 0);
  }

  renderLive(key);

  if (boxLatest && (!id || isNew || prevLabel !== lab)) {
    showResult(key, info, overDone && (!id || isNew), overNo);
  } else {
    renderBall(key);
  }
}

/* ========= Match status (Live / Upcoming / Finished) ========= */
const STATUS = {};   // matchKey -> started | not_started | completed
const START = {};    // matchKey -> start time
const STATUS_VIEW = {
  started:     ["LIVE",     "var(--green)"],
  not_started: ["UPCOMING", "var(--yellow)"],
  completed:   ["FINISHED", "var(--text-muted)"]
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

function applyStatusInfo(obj, key) {
  const st = ms(key);
  if (typeof obj.status === "string") setMatchStatus(key, obj.status);
  if (obj.start_at) START[key] = obj.start_at;

  const inn = deepGet(obj, "innings");
  if (!inn || typeof inn !== "object" || Array.isArray(inn)) return;
  const orderRaw = deepGet(obj, "innings_order");
  const order = Array.isArray(orderRaw) ? orderRaw : Object.keys(inn);
  const parts = [];
  let last = null;
  order.forEach(k => {
    const sc = inn[k]?.score;
    if (!sc || sc.runs === undefined) return;
    const side = String(k).split("_")[0];
    const nm = (TEAMS[key] || {})[side] || side.toUpperCase();
    const ov = fmtOvers(sc.overs ?? sc.over ?? "0.0");
    parts.push(`${nm} ${sc.runs}-${sc.wickets ?? 0} (${ov})`);
    last = { side, sc, ov };
  });
  st.sum = parts;
  // finished / upcoming match: last innings er score viewport e dekhao (live match live data diye cholbe)
  if (last && STATUS[key] !== "started") {
    st.runs = last.sc.runs; st.wk = last.sc.wickets ?? 0; st.ov = last.ov;
    st.bat = last.side; st.has = true;
  }
}

function resetViewport() {
  if ($("score")) $("score").textContent = "0-0";
  if ($("overTxt")) $("overTxt").textContent = "0.0 OVER";
  if ($("team")) $("team").textContent = "--";
  if ($("status")) $("status").innerHTML = "";
}

function reviewText(rv, key) {
  if (!rv || typeof rv !== "object") return "";
  const low = v => String(v ?? "").toLowerCase().replace(/[_-]+/g, " ");
  const status = low(rv.status ?? rv.state ?? rv.result);
  // review shesh hoye gele clear
  if (/over|done|complete|decid|uphold|struck|overturn|stay|finish|end/.test(status)) return "";

  const type = low(rv.type ?? rv.kind ?? rv.review_type ?? rv.reason);
  if (/no ?ball/.test(type)) return "NO BALL CHECK";
  if (/leg ?bye|\blb\b/.test(type)) return "LB CHECK";

  const by = low(rv.by ?? rv.team ?? rv.requested_by ?? rv.taken_by ?? rv.requester);
  const st = ms(key);
  if (/bat/.test(by) || (by && by.replace(/\s/g, "") === String(st.bat || "").toLowerCase())) return "BATTER REVIEW";
  if (/bowl|field/.test(by)) return "BOWLER REVIEW";
  return "REVIEW";
}

function applyMatchObject(obj, key, full) {
  const st = ms(key);
  const prevBrk = st.brk, prevRes = st.result;

  const rv = deepGet(obj, "review");
  if (full || rv !== undefined) {
    const t = reviewText(rv, key);
    if (t !== st.review) log(`# review: ${esc(t || "cleared")}`);
    st.review = t;
  }
  applyStatusInfo(obj, key);
  applyOversLimit(obj, key);

  const res = deepGet(obj, "result");
  const resMsg = res && typeof res === "object" ? (res.msg || deepGet(obj, "msgs")?.completed || "") : "";
  if (resMsg) {
    if (resMsg !== st.result && key === S.matchKey) log(`# result: ${esc(resMsg)}`);
    st.result = String(resMsg);
  }
  const tossStr = tossText(deepGet(obj, "toss"), key);
  if (tossStr) st.toss = tossStr;
  applyPlayState(obj, key);
  showStatus(key);


  const live = deepGet(obj, "live");
  const score = live && typeof live === "object" ? live.score : null;
  if (!score || score.runs === undefined) return false;

  // ball push chalche: score/over shudhu ball push theke (nahole blue age egie jay, chips pichiye thake)
  if (st.has && st.lastBallAt && Date.now() - st.lastBallAt < 20000) return false;

  st.runs = score.runs;
  st.wk = score.wickets ?? st.wk;
  st.ov = fmtOvers(score.overs ?? score.over ?? st.ov);
  if (typeof live.innings === "string") { st.innings = live.innings; st.bat = live.innings.split("_")[0]; }
  st.has = true;
  return true;
}

let _lastBase = 0;
async function fetchBaseline(key) {
  if (!S.matchKey || key !== S.matchKey) return;
  _lastBase = Date.now();
  try {
    const res = await fetch(`${API_BASE}/api/match/${encodeURIComponent(key)}`);
    const json = await res.json();
    const obj = json.data || json;
    applyMatchObject(obj, key, true);
    if (key === S.matchKey) renderLive(key);
  } catch (e) {}
}

let _hinted = false;
function updateViewport(payload) {
  const data = payload.data || payload;
  const key = data.matchkey || data.match?.key || data.match_key || data.key || null;
  const kind = data.match_push_kind || "-";

  if (!S.matchKey) {
    if (!_hinted) { log("# Select a match from Fixture."); _hinted = true; }
    if ($("status")) $("status").textContent = "Select a match from Fixture";
    setBallIdle();
    return;
  }
  if (key && key !== S.matchKey) return;

  if (data.ball) {
    const b = data.ball;
    if (b.is_delete) handleDelete(S.matchKey, b);
    else if (b.detail) handleBall(S.matchKey, b);
    else handleFirstPush(S.matchKey, b);
    return;
  }

  const needBase = ["Toss", "Play", "Result", "Reward", "Review", "Squad"].includes(kind)
    && (kind === "Review" || Date.now() - _lastBase > 3000);

  if (!data.matchkey) {
    const st = ms(S.matchKey);
    const had = st.has, pr = st.runs, pw = st.wk;
    if (applyMatchObject(data, S.matchKey)) {
      st.matchAt = Date.now();
      renderLive(S.matchKey);
      if (needBase) fetchBaseline(S.matchKey);
      if (had && Date.now() - st.lastBallAt > 10000) {
        const dr = st.runs - pr, dw = st.wk - pw;
        if (dr >= 0 && dw >= 0 && (dr > 0 || dw > 0)) {
          const info = dw > 0 ? { text: "WICKET", kind: "wicket" }
            : dr === 6 ? { text: "6", kind: "six" }
            : dr === 4 ? { text: "4", kind: "four" }
            : { text: String(dr), kind: "run" };
          showResult(S.matchKey, info, false, 0);
        }
      }
      return;
    }
  }

  if (needBase) {
    fetchBaseline(S.matchKey);
  }
}

function startPoll() {
  clearInterval(S.poll);
  S.poll = setInterval(() => {
    if (!S.started || !S.matchKey) return;
    if (ms(S.matchKey).result) return;
    fetchBaseline(S.matchKey);
  }, POLL_MS);
}

function startAll() {
  if (S.started) return;
  S.started = true;
  startLiveListener();
  startPoll();
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

$("bRestart")?.addEventListener("click", () => {
  stopAll();
  setTimeout(startAll, 300);
});

$("bAuto")?.addEventListener("click", () => {
  S.auto = !S.auto;
  $("bAuto").style.background = S.auto ? "var(--cyan)" : "";
  $("bAuto").style.color = S.auto ? "#000" : "";
  if (S.auto && !S.started) startAll();
});