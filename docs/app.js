/* FIB Hybrid Bot — frontend controller */
const $ = (id) => document.getElementById(id);
const fmt = (n, d = 4) => (n == null || isNaN(n)) ? "–" : Number(n).toLocaleString("en-US", { maximumFractionDigits: d });

/* ---------- theme (day / night) ---------- */
const themeBtn = $("theme-btn");
function applyTheme(t) {
  document.documentElement.setAttribute("data-theme", t);
  themeBtn.textContent = t === "dark" ? "🌙" : "☀️";
  localStorage.setItem("fib-theme", t);
}
applyTheme(localStorage.getItem("fib-theme") ||
  (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark"));
themeBtn.onclick = () => {
  applyTheme(document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark");
  if (lastSnap) renderSignals(lastSnap.signals || []); // redraw charts in new theme
};

/* ---------- manual scan ---------- */
$("scan-btn").onclick = async () => {
  $("scan-btn").textContent = "…";
  if (staticMode) {
    const snap = await fetchSnapshot();
    if (snap) render(snap);
  } else {
    try { await fetch("/api/scan", { method: "POST" }); } catch (e) {}
  }
  $("scan-btn").textContent = "↻ Scan";
};

/* ---------- "Saya Entry" button (event delegation) ---------- */
$("signals").addEventListener("click", (e) => {
  const btn = e.target.closest(".enter-btn");
  if (!btn || btn.classList.contains("taken")) return;
  const sym = btn.dataset.symbol;
  const sig = (lastSnap && lastSnap.signals || []).find(x => x.symbol === sym);
  if (!sig) return;
  addMyTrade(sig);
  renderMyTrades(lastSnap);
  renderSignals(lastSnap.signals || []);
});

/* ---------- backup / restore Trade Saya ---------- */
$("mt-export").onclick = async () => {
  const data = localStorage.getItem(MT_KEY) || "[]";
  try { await navigator.clipboard.writeText(data); alert("Data trade disalin ke clipboard. Simpan sebagai cadangan."); }
  catch (e) { prompt("Salin data cadangan ini:", data); }
};
$("mt-import").onclick = () => {
  const s = prompt("Tempel data cadangan Trade Saya:");
  if (!s) return;
  try { JSON.parse(s); localStorage.setItem(MT_KEY, s); renderMyTrades(lastSnap); if (lastSnap) renderSignals(lastSnap.signals || []); }
  catch (e) { alert("Data tidak valid."); }
};

/* ---------- websocket with polling fallback ---------- */
function setConn(state) {
  const dot = $("conn-dot"), txt = $("conn-text");
  dot.className = "dot " + (state === "live" ? "live" : state === "off" ? "off" : state === "static" ? "live" : "");
  txt.textContent = state === "live" ? "live" : state === "off" ? "terputus"
    : state === "static" ? "GitHub (tiap ~15m)" : "menghubungkan";
}
// Data sources tried in order: live server first, then a static JSON file
// (used when hosted on GitHub Pages, where there is no backend).
const SNAP_URLS = ["/api/snapshot", "data/snapshot.json"];
let staticMode = false;

async function fetchSnapshot() {
  for (const url of SNAP_URLS) {
    try {
      const r = await fetch(url, { cache: "no-store" });
      if (r.ok) { staticMode = url !== "/api/snapshot"; return await r.json(); }
    } catch (e) { /* try next */ }
  }
  return null;
}

let ws, pollTimer;
function connect() {
  try {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onopen = () => { setConn("live"); clearInterval(pollTimer); };
    ws.onmessage = (e) => render(JSON.parse(e.data));
    ws.onclose = () => { startPolling(); setTimeout(connect, 8000); };
    ws.onerror = () => ws.close();
  } catch (e) { startPolling(); }
}
function startPolling() {
  clearInterval(pollTimer);
  const tick = async () => {
    const snap = await fetchSnapshot();
    if (snap) { render(snap); setConn(staticMode ? "static" : "live"); }
    else setConn("off");
  };
  tick();
  pollTimer = setInterval(tick, staticMode ? 30000 : 5000);
}
connect();

/* ============ TRADE SAYA (personal tracker, disimpan di perangkat) ============ */
const MT_KEY = "fib-my-trades";
let myOpenSymbols = new Set();

const loadMT = () => { try { return JSON.parse(localStorage.getItem(MT_KEY)) || []; } catch (e) { return []; } };
const saveMT = (l) => localStorage.setItem(MT_KEY, JSON.stringify(l));

function addMyTrade(sig) {
  const p = sig.plan || {};
  if (!p.entry) return;
  const list = loadMT();
  if (list.some(t => t.symbol === sig.symbol && t.status === "OPEN")) return; // 1 posisi/simbol
  // masuk di HARGA SEKARANG; SL & TP2 ikut rencana bot, TP1 = +1R dari entry
  const cur = (lastSnap && lastSnap.prices && lastSnap.prices[sig.symbol]) ?? p.entry;
  const entry = cur;
  const risk = Math.abs(entry - p.sl) || 1e-9;
  const tp1 = sig.direction === "LONG" ? entry + risk : entry - risk;
  list.push({
    id: Date.now(), symbol: sig.symbol, direction: sig.direction,
    entry, sl: p.sl, tp1, tp2: p.tp2, rr: p.rr,
    confidence: sig.confidence, size: p.position_size,
    opened_ts: new Date().toISOString(), status: "OPEN", tp1_hit: false,
  });
  saveMT(list);
}

function _finalize(t, r, price) {
  t.status = "CLOSED";
  t.r = Math.round(r * 100) / 100;
  t.outcome = r > 0.05 ? "WIN" : r < -0.05 ? "LOSS" : "BE";
  t.exit_price = price;
  t.closed_ts = new Date().toISOString();
}

// Lacak & selesaikan trade memakai harga terbaru dari snapshot (tiap refresh).
function resolveMT(prices) {
  if (!prices) return;
  const list = loadMT();
  let changed = false;
  for (const t of list) {
    if (t.status !== "OPEN") continue;
    const cur = prices[t.symbol];
    if (cur == null) continue;
    // lewati jika setup tak konsisten (SL/TP di sisi yang salah dari entry)
    const valid = t.direction === "LONG" ? (t.sl < t.entry && t.entry < t.tp2)
                                         : (t.tp2 < t.entry && t.entry < t.sl);
    if (!valid) continue;
    const risk = Math.abs(t.entry - t.sl) || 1e-9;
    if (t.direction === "LONG") {
      const be = t.entry * 1.0015, stop = t.tp1_hit ? be : t.sl;
      if (cur <= stop) { _finalize(t, t.tp1_hit ? 0.5 + 0.5 * (be - t.entry) / risk : -1, stop); changed = true; continue; }
      if (!t.tp1_hit && cur >= t.tp1) { t.tp1_hit = true; changed = true; }
      if (cur >= t.tp2) { _finalize(t, 0.5 + 0.5 * (t.tp2 - t.entry) / risk, t.tp2); changed = true; }
    } else {
      const be = t.entry * 0.9985, stop = t.tp1_hit ? be : t.sl;
      if (cur >= stop) { _finalize(t, t.tp1_hit ? 0.5 + 0.5 * (t.entry - be) / risk : -1, stop); changed = true; continue; }
      if (!t.tp1_hit && cur <= t.tp1) { t.tp1_hit = true; changed = true; }
      if (cur <= t.tp2) { _finalize(t, 0.5 + 0.5 * (t.entry - t.tp2) / risk, t.tp2); changed = true; }
    }
  }
  if (changed) saveMT(list);
}

function closeMT(id) {
  const list = loadMT();
  const t = list.find(x => x.id === id);
  if (!t || t.status !== "OPEN") return;
  const cur = (lastSnap && lastSnap.prices && lastSnap.prices[t.symbol]) || t.entry;
  const risk = Math.abs(t.entry - t.sl) || 1e-9;
  const r = t.direction === "LONG" ? (cur - t.entry) / risk : (t.entry - cur) / risk;
  _finalize(t, r, cur);
  saveMT(list);
  renderMyTrades(lastSnap);
  if (lastSnap) renderSignals(lastSnap.signals || []);
}
window.closeMT = closeMT;

function renderMyTrades(snap) {
  resolveMT(snap && snap.prices);
  const list = loadMT();
  const open = list.filter(t => t.status === "OPEN");
  const closed = list.filter(t => t.status === "CLOSED").sort((a, b) => b.id - a.id);
  myOpenSymbols = new Set(open.map(t => t.symbol));
  const prices = (snap && snap.prices) || {};

  // ringkasan risiko pribadi
  const today = new Date().toDateString();
  const todayCount = list.filter(t => new Date(t.opened_ts).toDateString() === today).length;
  const wins = closed.filter(t => t.outcome === "WIN").length;
  const wr = closed.length ? Math.round(wins / closed.length * 100) : 0;
  const sumR = closed.reduce((a, t) => a + (t.r || 0), 0);
  const overLimit = todayCount > 3;
  $("my-risk").innerHTML = [
    ["Trade hari ini", `${todayCount} / 3${overLimit ? " ⚠️" : ""}`],
    ["Posisi terbuka", `${open.length}`],
    ["Menang / Kalah", `${wins} / ${closed.length - wins} (${wr}%)`],
    ["Total R (real.)", `${sumR >= 0 ? "+" : ""}${sumR.toFixed(2)}R`],
  ].map(([k, v]) => `<div class="row"><span class="muted">${k}</span><span class="v">${v}</span></div>`).join("");

  const openHTML = open.map(t => {
    const cur = prices[t.symbol];
    const risk = Math.abs(t.entry - t.sl) || 1e-9;
    let pnl = "–", cls = "";
    if (cur != null) {
      const r = t.direction === "LONG" ? (cur - t.entry) / risk : (t.entry - cur) / risk;
      cls = r >= 0 ? "o-win" : "o-loss";
      pnl = `${r >= 0 ? "+" : ""}${r.toFixed(2)}R`;
    }
    return `<div class="mt">
      <div class="mt-top">
        <span><span class="mt-sym">${t.symbol}</span> <span class="badge ${t.direction.toLowerCase()}">${t.direction}</span></span>
        <span class="${cls}">${pnl}${t.tp1_hit ? " · TP1✓" : ""}</span>
      </div>
      <div class="mt-row"><span>Entry ${fmt(t.entry)}</span><span>now ${cur != null ? fmt(cur) : "–"}</span></div>
      <div class="mt-row"><span class="lv-sl">SL ${fmt(t.sl)}</span><span class="lv-tp">TP2 ${fmt(t.tp2)}</span></div>
      <div class="mt-row"><span>${new Date(t.opened_ts).toLocaleString("id-ID", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })}</span>
        <button class="mt-close" onclick="closeMT(${t.id})">Tutup</button></div>
    </div>`;
  }).join("");

  const closedHTML = closed.slice(0, 8).map(t => {
    const oc = (t.outcome || "").toLowerCase();
    const cls = oc === "win" ? "o-win" : oc === "loss" ? "o-loss" : "o-be";
    return `<div class="mt">
      <div class="mt-top">
        <span><span class="mt-sym">${t.symbol}</span> <span class="badge ${t.direction.toLowerCase()}">${t.direction}</span></span>
        <span class="${cls}">${t.outcome} ${t.r >= 0 ? "+" : ""}${t.r}R</span>
      </div>
      <div class="mt-row"><span>Entry ${fmt(t.entry)} → ${fmt(t.exit_price)}</span>
        <span>${new Date(t.closed_ts).toLocaleDateString("id-ID")}</span></div>
    </div>`;
  }).join("");

  $("my-trades").innerHTML = (open.length || closed.length)
    ? (openHTML + closedHTML)
    : `<p class="mt-empty">Belum ada trade. Tekan "✅ Saya Entry" di kartu sinyal saat Anda mengambil posisi — trade dicatat & dilacak di sini (tersimpan di perangkat Anda).</p>`;
}

/* ---------- render ---------- */
let lastSnap = null;
function render(s) {
  lastSnap = s;
  renderMyTrades(s);
  renderRegime(s.regime);
  renderKpis(s.stats, s.risk);
  renderSignals(s.signals || []);
  renderMarket(s.regime);
  renderRisk(s.risk);
  renderLessons(s.lessons || [], s.blocked || []);
  renderJournal(s.recent_trades || []);
  if (s.last_scan) $("last-scan").textContent = "scan: " + new Date(s.last_scan).toLocaleTimeString("id-ID");
}

function renderRegime(r = {}) {
  const b = $("regime-badge");
  const reg = r.regime || "NEUTRAL";
  b.className = "regime " + reg.toLowerCase();
  const map = { BULL: "🐂 BULL · mesin LONG", BEAR: "🐻 BEAR · mesin SHORT", NEUTRAL: "◌ NETRAL" };
  b.textContent = map[reg] || reg;
}

function renderKpis(st = {}, risk = {}) {
  $("k-winrate").textContent = (st.win_rate ?? 0) + "%";
  $("k-pf").textContent = st.profit_factor ?? "–";
  $("k-resolved").textContent = st.resolved ?? 0;
  $("k-open").textContent = st.open ?? 0;
  $("k-today").textContent = `${risk.trades_today ?? 0}/${risk.max_trades ?? 3}`;
  const pnl = risk.pnl_today_pct ?? 0;
  const el = $("k-pnl");
  el.textContent = (pnl >= 0 ? "+" : "") + pnl + "%";
  el.style.color = pnl > 0 ? "var(--green)" : pnl < 0 ? "var(--red)" : "var(--text)";
}

function renderSignals(list) {
  const grid = $("signals");
  const shown = list.filter(x => x.state !== "WATCHING" || x.confidence >= 0.5).slice(0, 30);
  $("signals-empty").classList.toggle("hidden", shown.length > 0);
  grid.innerHTML = shown.map(cardHTML).join("");
  // draw candlestick charts after the cards are in the DOM
  requestAnimationFrame(() => shown.forEach(s => {
    const cv = document.getElementById("chart-" + s.symbol);
    if (cv && s.candles && s.candles.length) drawChart(cv, s);
  }));
}

/* ---------- candlestick chart (dependency-free, theme-aware) ---------- */
function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

function drawChart(cv, s) {
  const dpr = window.devicePixelRatio || 1;
  const W = cv.clientWidth || 260, H = cv.clientHeight || 130;
  cv.width = W * dpr; cv.height = H * dpr;
  const ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);

  const c = s.candles, p = s.plan || {}, fib = s.fib || {};
  const padR = 52, padT = 6, padB = 6;
  const plotW = W - padR, plotH = H - padT - padB;

  // price range spans candles + key levels so all lines are visible
  let lo = Infinity, hi = -Infinity;
  for (const k of c) { lo = Math.min(lo, k[3]); hi = Math.max(hi, k[2]); }
  [p.entry, p.sl, p.tp1, p.tp2, fib["0.5"], fib["0.618"]].forEach(v => {
    if (v != null) { lo = Math.min(lo, v); hi = Math.max(hi, v); }
  });
  const pad = (hi - lo) * 0.06 || 1; lo -= pad; hi += pad;
  const y = v => padT + plotH * (1 - (v - lo) / (hi - lo));
  const green = cssVar("--green"), red = cssVar("--red"), muted = cssVar("--muted");
  const accent = cssVar("--accent"), amber = cssVar("--amber");

  // golden-zone band
  if (fib["0.5"] != null && fib["0.618"] != null) {
    ctx.fillStyle = "rgba(245,158,11,0.13)";
    const y1 = y(fib["0.5"]), y2 = y(fib["0.618"]);
    ctx.fillRect(0, Math.min(y1, y2), plotW, Math.abs(y2 - y1));
  }

  // candles
  const n = c.length, cw = plotW / n, bw = Math.max(1.5, cw * 0.6);
  c.forEach((k, i) => {
    const [, o, h, l, cl] = k;
    const x = i * cw + cw / 2;
    const up = cl >= o;
    ctx.strokeStyle = up ? green : red;
    ctx.fillStyle = up ? green : red;
    ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(x, y(h)); ctx.lineTo(x, y(l)); ctx.stroke();
    const yo = y(o), yc = y(cl);
    ctx.fillRect(x - bw / 2, Math.min(yo, yc), bw, Math.max(1, Math.abs(yc - yo)));
  });

  // level lines + right-edge labels
  const lines = [
    [p.entry, accent, "Entry"], [p.sl, red, "SL"],
    [p.tp1, green, "TP1"], [p.tp2, green, "TP2"],
  ];
  ctx.font = "9px system-ui, sans-serif"; ctx.textBaseline = "middle";
  lines.forEach(([v, col, lbl]) => {
    if (v == null || v < lo || v > hi) return;
    const yy = y(v);
    ctx.strokeStyle = col; ctx.globalAlpha = 0.85; ctx.lineWidth = 1;
    ctx.setLineDash([4, 3]); ctx.beginPath();
    ctx.moveTo(0, yy); ctx.lineTo(plotW, yy); ctx.stroke();
    ctx.setLineDash([]); ctx.globalAlpha = 1;
    ctx.fillStyle = col; ctx.textAlign = "left";
    ctx.fillText(lbl, plotW + 3, yy);
  });
}

function cardHTML(s) {
  const dir = s.direction.toLowerCase();
  const stateCls = "state-" + s.state.toLowerCase();
  const p = s.plan || {};
  const conf = Math.round((s.confidence || 0) * 100);
  const gateCls = !s.allowed ? "blocked" : s.actionable ? "ok" : "";
  const checks = (s.checklist || []).concat(s.trigger || []);
  const taken = myOpenSymbols.has(s.symbol);
  return `
  <div class="card ${s.state.toLowerCase()}">
    <div class="card-top">
      <span class="sym">${s.symbol}</span>
      <div class="badges">
        <span class="badge ${dir}">${s.direction}</span>
        <span class="badge ${stateCls}">${s.state}</span>
      </div>
    </div>
    <div class="conf-wrap">
      <div class="conf-bar"><div class="conf-fill" style="width:${conf}%"></div></div>
      <div class="conf-row"><span>Keyakinan (belajar)</span><span>${conf}% · ${s.learn_reason || ""}</span></div>
    </div>
    <canvas class="chart" id="chart-${s.symbol}"></canvas>
    <div class="levels">
      <div><span class="lbl">Harga</span><span>${fmt(s.price)}</span></div>
      <div><span class="lbl">Retrace</span><span>${fmt(s.retrace_ratio, 3)}</span></div>
      <div><span class="lbl">Entry</span><span>${fmt(p.entry)}</span></div>
      <div><span class="lbl lv-sl">SL</span><span class="lv-sl">${fmt(p.sl)}</span></div>
      <div><span class="lbl lv-tp">TP1</span><span class="lv-tp">${fmt(p.tp1)}</span></div>
      <div><span class="lbl lv-tp">TP2</span><span class="lv-tp">${fmt(p.tp2)}</span></div>
      <div><span class="lbl">RR</span><span>${p.rr ?? "–"}${p.rr_ok ? " ✓" : ""}</span></div>
      <div><span class="lbl">Ukuran</span><span>${fmt(p.position_size, 4)}</span></div>
    </div>
    ${s.gate ? `<div class="gate ${gateCls}">${s.gate}</div>` : ""}
    <details class="checklist">
      <summary>Cek aturan (${checks.filter(c => c.ok).length}/${checks.length})</summary>
      ${checks.map(c => `<div class="chk ${c.ok ? "ok" : "no"}">
        <span class="mk">${c.ok ? "✔" : "✘"}</span><span>${c.rule}</span>
        ${c.detail ? `<span class="dt">${c.detail}</span>` : ""}</div>`).join("")}
    </details>
    ${p.entry ? `<button class="enter-btn ${taken ? "taken" : ""}" data-symbol="${s.symbol}" ${taken ? "disabled" : ""}>
      ${taken ? "✔ Sudah dientry" : "✅ Saya Entry (catat ke risiko)"}</button>` : ""}
  </div>`;
}

function renderMarket(r = {}) {
  const rows = [
    ["BTC EMA50 1D", r.btc_ema50_rising == null ? "–" : (r.btc_ema50_rising ? "naik ↑" : "turun ↓"), r.btc_ema50_rising],
    ["USDT.D", r.usdtd_value != null ? r.usdtd_value + "%" : (r.usdtd_ok ? "–" : "n/a"), null],
    ["USDT.D posisi", r.usdtd_pos != null ? Math.round(r.usdtd_pos * 100) + "% range" : "–", null],
    ["USDT.D arah", r.usdtd_rising == null ? "–" : (r.usdtd_rising ? "naik (risk-off)" : "turun (risk-on)"), r.usdtd_rising == null ? null : !r.usdtd_rising],
    ["Bias USDT.D", r.usdtd_bias || "–", null],
    ["Di resistance", r.usdtd_at_resistance ? "YA (short kuat)" : "tidak", null],
  ];
  $("market").innerHTML = rows.map(([k, v, up]) => {
    const cls = up === true ? "tag-up" : up === false ? "tag-dn" : "";
    return `<div class="row"><span class="muted">${k}</span><span class="v ${cls}">${v}</span></div>`;
  }).join("");
}

function renderRisk(r = {}) {
  const box = r.halted
    ? `<div class="halt">⛔ Entry baru DIHENTIKAN (circuit breaker)</div>`
    : `<div class="ok-box">✅ Entry baru diizinkan</div>`;
  $("risk").innerHTML = box + [
    ["Trade hari ini", `${r.trades_today ?? 0} / ${r.max_trades ?? 3}`],
    ["Stop-loss hari ini", `${r.stops_today ?? 0} / 2`],
    ["PnL hari ini", `${r.pnl_today_pct ?? 0}%`],
    ["Risiko / trade", "2%"],
  ].map(([k, v]) => `<div class="row"><span class="muted">${k}</span><span class="v">${v}</span></div>`).join("");
}

function renderLessons(lessons, blocked) {
  $("lesson-count").textContent = lessons.length;
  if (!lessons.length) {
    $("lessons").innerHTML = `<p class="muted small">Belum ada pelajaran. Bot akan mencatat pola menang/kalah
      dan otomatis menghindari pola yang berulang rugi.</p>`;
    return;
  }
  $("lessons").innerHTML = lessons.map(l => {
    const kind = (l.kind || "").toLowerCase();
    return `<div class="lesson ${kind}">${l.text}
      <div class="meta">win ${Math.round((l.win_rate || 0) * 100)}% · ${l.samples} sampel · ${new Date(l.ts).toLocaleDateString("id-ID")}</div>
    </div>`;
  }).join("");
}

function renderJournal(trades) {
  const tb = document.querySelector("#journal tbody");
  if (!trades.length) { tb.innerHTML = `<tr><td colspan="9" class="empty">Belum ada trade.</td></tr>`; return; }
  tb.innerHTML = trades.map(t => {
    const o = (t.outcome || "").toLowerCase();
    const ocls = o === "win" ? "o-win" : o === "loss" ? "o-loss" : o ? "o-be" : "";
    return `<tr>
      <td>${new Date(t.created_ts).toLocaleString("id-ID", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })}</td>
      <td>${t.symbol}</td>
      <td class="${t.direction === "LONG" ? "o-win" : "o-loss"}">${t.direction}</td>
      <td>${fmt(t.entry)}</td>
      <td>${t.exit_price ? fmt(t.exit_price) : "–"}</td>
      <td class="${ocls}">${t.outcome || "—"}</td>
      <td>${t.r_multiple != null ? t.r_multiple : "–"}</td>
      <td>${Math.round((t.confidence || 0) * 100)}%</td>
      <td>${t.status}</td>
    </tr>`;
  }).join("");
}
