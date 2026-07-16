/* SMC Bot — frontend controller */
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
  if (lastBacktest && !$("view-backtest").classList.contains("hidden")) renderBacktest(lastBacktest);
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
  renderJournal(lastSnap ? lastSnap.recent_trades : []);   // jurnal ikut terupdate
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

/* ============ NEWS ALERT (ForexFactory High-Impact, waktu WIB) ============ */
let newsEvents = [];
async function loadNews() {
  try {
    const r = await fetch("data/news.json", { cache: "no-store" });
    if (r.ok) { const d = await r.json(); newsEvents = d.events || []; }
  } catch (e) { /* offline: keep last */ }
  renderNewsAlert();
}
function fmtWIB(d) {
  return d.toLocaleString("id-ID", { weekday: "short", day: "2-digit", month: "short",
    hour: "2-digit", minute: "2-digit", timeZone: "Asia/Jakarta" }) + " WIB";
}
function _hm(mins) { const m = Math.abs(mins); return `${Math.floor(m / 60)}j ${m % 60}m`; }
function renderNewsAlert() {
  const el = $("news-alert");
  if (!el) return;
  const now = Date.now();
  const ALERT_MS = 4 * 3600e3;   // 4 jam sebelum
  const evs = (newsEvents || [])
    .map(e => ({ ...e, t: new Date(e.ts).getTime() }))
    .filter(e => e.t - now > -30 * 60e3)     // buang yang sudah lewat >30 menit
    .sort((a, b) => a.t - b.t);
  if (!evs.length) { el.className = "news-alert hidden"; el.innerHTML = ""; return; }
  const next = evs[0], diff = next.t - now, mins = Math.round(diff / 60000);
  const list = evs.slice(0, 5).map(e => {
    const dm = Math.round((e.t - now) / 60000);
    const soon = e.t - now <= ALERT_MS && e.t - now > 0;
    return `<li class="${soon ? "soon" : ""}"><span class="nt">${fmtWIB(new Date(e.t))}</span>
      <span class="nn">${e.country ? e.country + " · " : ""}${e.title}</span>
      <span class="nc">${e.t <= now ? "berlangsung" : "dalam " + _hm(dm)}</span></li>`;
  }).join("");
  let head, cls;
  if (diff <= 0) { cls = "danger"; head = `🔴 NEWS BERLANGSUNG: ${next.title} — jangan trading dulu`; }
  else if (diff <= ALERT_MS) { cls = "warn"; head = `⏰ ${_hm(mins)} lagi ada NEWS high-impact: <b>${next.title}</b> · ${fmtWIB(new Date(next.t))} — hindari entry`; }
  else { cls = "ok"; head = `✅ Aman. News high-impact berikutnya: ${next.title} · ${fmtWIB(new Date(next.t))} (${_hm(mins)} lagi)`; }
  el.className = "news-alert " + cls;
  el.innerHTML = `<div class="na-head">${head}</div><ul class="na-list">${list}</ul>
    <div class="na-src">📅 Sumber: ForexFactory · High Impact Expected · waktu WIB</div>`;
}
const _fy = $("foot-year"); if (_fy) _fy.textContent = new Date().getFullYear();
loadNews();
setInterval(loadNews, 10 * 60 * 1000);     // refresh feed tiap 10 menit
setInterval(renderNewsAlert, 30 * 1000);   // update hitung mundur tiap 30 detik

/* ============ TABS: Live / Backtest ============ */
let lastBacktest = null;
document.querySelectorAll(".tab").forEach(t => {
  t.onclick = () => {
    document.querySelectorAll(".tab").forEach(x => x.classList.remove("active"));
    t.classList.add("active");
    const v = t.dataset.view;
    $("view-live").classList.toggle("hidden", v !== "live");
    $("view-backtest").classList.toggle("hidden", v !== "backtest");
    $("view-macro").classList.toggle("hidden", v !== "macro");
    if (v === "backtest") loadBacktest();
    if (v === "macro") loadMacro();
  };
});

// Build a deep link to this repo's backtest workflow (works on GitHub Pages).
(function setupBacktestControls() {
  const run = $("bt-run");
  const host = location.hostname;           // e.g. juanfakhri.github.io
  const seg = location.pathname.split("/").filter(Boolean)[0]; // repo name
  if (run && host.endsWith("github.io") && seg) {
    const user = host.split(".")[0];
    run.href = `https://github.com/${user}/${seg}/actions/workflows/backtest.yml`;
  } else if (run) {
    run.href = "https://github.com";
    run.textContent = "▶ Jalankan backtest (buka GitHub Actions)";
  }
  const ref = $("bt-refresh");
  if (ref) ref.onclick = () => loadBacktest();
})();

async function loadBacktest() {
  let rep = null;
  for (const url of ["data/backtest.json", "/api/backtest"]) {
    try { const r = await fetch(url, { cache: "no-store" }); if (r.ok) { rep = await r.json(); break; } }
    catch (e) { /* try next */ }
  }
  if (rep) renderBacktest(rep);
}

function renderBacktest(rep) {
  lastBacktest = rep;
  const s = rep.summary || {};
  const has = (s.trades || 0) > 0;
  $("bt-empty").classList.toggle("hidden", has);
  $("bt-winrate").textContent = (s.win_rate ?? 0) + "%";
  $("bt-pf").textContent = s.profit_factor ?? "–";
  $("bt-trades").textContent = s.trades ?? 0;
  $("bt-exp").textContent = (s.expectancy_r >= 0 ? "+" : "") + (s.expectancy_r ?? 0) + "R";
  $("bt-totalr").textContent = (s.total_r >= 0 ? "+" : "") + (s.total_r ?? 0) + "R";
  $("bt-dd").textContent = (s.max_drawdown_r ?? 0) + "R";
  const p = rep.params || {};
  $("bt-meta").textContent = `Strategi: ${(p.strategy || "smc").toUpperCase()}`
    + (p.score_th != null ? ` (Skor Setup ≥ ${p.score_th})` : "")
    + ` · ${p.lookback_days || "?"} hari · ${p.symbols || "?"} simbol · trigger ${p.ltf || "1h"}`
    + (rep.generated_ts ? ` · dibuat ${new Date(rep.generated_ts).toLocaleString("id-ID")}` : "")
    + (p.demo ? " · (DEMO)" : "");

  drawEquity($("bt-equity"), s.equity_curve || []);

  const sym = s.per_symbol || {};
  document.querySelector("#bt-symbols tbody").innerHTML = Object.keys(sym).length
    ? Object.entries(sym).map(([k, v]) => `<tr><td>${k}</td><td>${v.n}</td>
        <td class="${v.win_rate >= 50 ? "o-win" : "o-loss"}">${v.win_rate}%</td>
        <td class="${v.total_r >= 0 ? "o-win" : "o-loss"}">${v.total_r >= 0 ? "+" : ""}${v.total_r}R</td></tr>`).join("")
    : `<tr><td colspan="4" class="empty">–</td></tr>`;

  const lessons = (rep.learned && rep.learned.lessons) || [];
  $("bt-lesson-count").textContent = lessons.length;
  $("bt-lessons").innerHTML = lessons.length
    ? lessons.map(l => `<div class="lesson ${(l.kind || "").toLowerCase()}">${l.text}
        <div class="meta">win ${Math.round((l.win_rate || 0) * 100)}% · ${l.samples} sampel</div></div>`).join("")
    : `<p class="muted small">Belum ada pelajaran (butuh ≥5 sampel per pola).</p>`;

  renderOptimization(rep.params);
  renderWalkforward(rep.walkforward);
  renderBtMetrics(s);

  const tr = rep.recent_trades || [];
  document.querySelector("#bt-journal tbody").innerHTML = tr.length
    ? tr.map(t => {
        const oc = (t.outcome || "").toLowerCase();
        const cls = oc === "win" ? "o-win" : oc === "loss" ? "o-loss" : "o-be";
        return `<tr>
          <td>${new Date(t.entry_ts).toLocaleDateString("id-ID", { day: "2-digit", month: "short" })}</td>
          <td>${new Date(t.exit_ts).toLocaleDateString("id-ID", { day: "2-digit", month: "short" })}</td>
          <td>${t.symbol}</td>
          <td class="${t.direction === "LONG" ? "o-win" : "o-loss"}">${t.direction}</td>
          <td>${fmt(t.entry)}</td><td>${fmt(t.exit_price)}</td><td>${t.rr}</td>
          <td class="${cls}">${t.outcome}</td><td class="${cls}">${t.r >= 0 ? "+" : ""}${t.r}</td>
        </tr>`;
      }).join("")
    : `<tr><td colspan="9" class="empty">Belum ada trade backtest.</td></tr>`;
}

function renderOptimization(p) {
  const el = $("bt-opt");
  if (!el) return;
  p = p || {};
  el.innerHTML = `
    <div class="opt-box on">
      ✅ <b>Strategi SMC + Skor Setup</b> — entry hanya saat konfluensi lolos ambang.
      <div class="opt-params">
        <span class="opt-chip">Skor Setup ≥ ${p.score_th ?? 60}</span>
        <span class="opt-chip">Trigger 1H</span>
        <span class="opt-chip">SL swing ± ATR</span>
        <span class="opt-chip">TP 1R/2R/3R</span>
      </div>
    </div>`;
}

function renderWalkforward(wf) {
  const el = $("bt-wf");
  if (!el) return;
  if (!wf || !wf.test_all) { el.innerHTML = `<p class="muted small">Belum ada data.</p>`; return; }
  const a = wf.test_all, f = wf.test_filtered;
  const profit = (f.profit_factor >= 1 && f.total_r > 0);
  const cell = (v, good) => `<span class="${good ? "o-win" : "o-loss"}">${v}</span>`;
  el.innerHTML = `
    <div class="opt-box ${profit ? "on" : "off"}">
      ${profit ? "✅ <b>Profit di data uji (out-of-sample)!</b>" : "⚠️ Belum profit di data uji — perlu perbaikan lanjutan."}
      &nbsp;Bot menahan diri: dari ${wf.test_n} sinyal, hanya <b>${wf.kept}</b> yang dieksekusi.
    </div>
    <div class="table-wrap"><table style="margin-top:10px">
      <thead><tr><th></th><th>Semua trade</th><th>Setelah filter bot</th></tr></thead>
      <tbody>
        <tr><td class="muted">Jumlah trade</td><td>${a.trades}</td><td>${f.trades}</td></tr>
        <tr><td class="muted">Win rate</td><td>${a.win_rate}%</td><td>${cell(f.win_rate + "%", f.win_rate >= a.win_rate)}</td></tr>
        <tr><td class="muted">Profit Factor</td><td>${a.profit_factor}</td><td>${cell(f.profit_factor, f.profit_factor >= 1)}</td></tr>
        <tr><td class="muted">Expectancy</td><td>${a.expectancy_r}R</td><td>${cell(f.expectancy_r + "R", f.expectancy_r > 0)}</td></tr>
        <tr><td class="muted">Total R</td><td>${a.total_r}R</td><td>${cell(f.total_r + "R", f.total_r > 0)}</td></tr>
      </tbody>
    </table></div>`;
}

function renderBtMetrics(s) {
  const L = s.long || {}, S = s.short || {};
  $("m-long").textContent = (L.n ? `${L.win_rate}%` : "–") + (L.n ? ` (${L.n})` : "");
  $("m-short").textContent = (S.n ? `${S.win_rate}%` : "–") + (S.n ? ` (${S.n})` : "");
  $("m-dur").textContent = s.avg_duration_bars ? `${s.avg_duration_bars} bar 4H` : "–";
  const hist = s.r_histogram || [];
  const max = Math.max(1, ...hist.map(h => h.count));
  $("bt-hist").innerHTML = hist.map(h =>
    `<div class="hist-row"><span class="muted">${h.label}</span>
      <span class="hist-bar" style="width:${Math.round(h.count / max * 100)}%"></span>
      <span>${h.count}</span></div>`).join("");
}

function drawEquity(cv, curve) {
  const dpr = window.devicePixelRatio || 1;
  const W = cv.clientWidth || 600, H = cv.clientHeight || 240;
  cv.width = W * dpr; cv.height = H * dpr;
  const ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  if (!curve.length) return;
  const padL = 44, padB = 22, padT = 12, padR = 12;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const rs = curve.map(p => p.r);
  let lo = Math.min(0, ...rs), hi = Math.max(0, ...rs);
  if (hi === lo) hi = lo + 1;
  const x = i => padL + plotW * (i / Math.max(1, curve.length - 1));
  const y = v => padT + plotH * (1 - (v - lo) / (hi - lo));
  const muted = cssVar("--muted"), accent = cssVar("--accent"), green = cssVar("--green"), red = cssVar("--red");
  // zero line
  ctx.strokeStyle = muted; ctx.globalAlpha = 0.4; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(padL, y(0)); ctx.lineTo(W - padR, y(0)); ctx.stroke();
  ctx.globalAlpha = 1;
  // axis labels
  ctx.fillStyle = muted; ctx.font = "10px system-ui"; ctx.textAlign = "right"; ctx.textBaseline = "middle";
  ctx.fillText(hi.toFixed(0) + "R", padL - 5, y(hi));
  ctx.fillText(lo.toFixed(0) + "R", padL - 5, y(lo));
  // area + line
  const end = rs[rs.length - 1];
  const col = end >= 0 ? green : red;
  ctx.beginPath();
  curve.forEach((p, i) => { const px = x(i), py = y(p.r); i ? ctx.lineTo(px, py) : ctx.moveTo(px, py); });
  ctx.strokeStyle = col; ctx.lineWidth = 2; ctx.stroke();
  ctx.lineTo(x(curve.length - 1), y(0)); ctx.lineTo(x(0), y(0)); ctx.closePath();
  ctx.fillStyle = col; ctx.globalAlpha = 0.12; ctx.fill(); ctx.globalAlpha = 1;
}

/* ============ MAKRO: screening berita + backtest 3 tahun ============ */
(function setupMacroControls() {
  const run = $("mc-run");
  const host = location.hostname, seg = location.pathname.split("/").filter(Boolean)[0];
  if (run && host.endsWith("github.io") && seg) {
    run.href = `https://github.com/${host.split(".")[0]}/${seg}/actions/workflows/news_backtest.yml`;
  } else if (run) { run.href = "https://github.com"; run.textContent = "▶ Buka GitHub Actions"; }
  const ref = $("mc-refresh"); if (ref) ref.onclick = () => loadMacro();
})();

const _pct = v => (v >= 0 ? "+" : "") + (v ?? 0) + "%";
function _verdictBadge(bias, verdict) {
  const cls = bias === "RISK_ON" ? "bagus" : bias === "RISK_OFF" ? "buruk" : "netral";
  const txt = verdict ? verdict.toUpperCase() : (cls === "bagus" ? "BAGUS" : cls === "buruk" ? "BURUK" : "NETRAL");
  return `<span class="verdict ${cls}">${txt}</span>`;
}

async function loadMacro() {
  // 1) live screen from news.json (already enriched by the scan)
  try {
    const r = await fetch("data/news.json", { cache: "no-store" });
    if (r.ok) renderMacroNow(await r.json());
  } catch (e) { /* offline */ }
  // 2) 3-year backtest report
  let rep = null;
  for (const url of ["data/news_backtest.json", "/api/news_backtest"]) {
    try { const x = await fetch(url, { cache: "no-store" }); if (x.ok) { rep = await x.json(); break; } }
    catch (e) { /* next */ }
  }
  if (rep) renderMacroBacktest(rep);
}

function renderMacroNow(d) {
  const s = d.screen || {};
  const now = $("mc-now");
  if (now) {
    const has = s.n_events > 0;
    now.innerHTML = has
      ? `<div class="mc-head">${_verdictBadge(s.bias, s.verdict)}
           <span class="mc-net">skor makro ${s.net_score >= 0 ? "+" : ""}${s.net_score}</span>
           <span class="muted small">${s.n_events} rilis ke depan</span></div>
         ${s.driver ? `<p class="muted small">Penggerak utama: <b>${s.driver}</b> — ${s.driver_reason || ""}</p>` : ""}`
      : `<p class="muted">Tidak ada rilis high-impact terdekat untuk dinilai.</p>`;
  }
  const evs = (d.events || []);
  const tb = document.querySelector("#mc-events tbody");
  $("mc-events-empty").classList.toggle("hidden", evs.length > 0);
  if (tb) tb.innerHTML = evs.map(e => {
    const pf = (e.previous || "–") + " → " + (e.forecast || "–");
    const when = e.ts ? fmtWIB(new Date(e.ts)) : "–";
    return `<tr><td class="small">${when}</td>
      <td>${e.country ? e.country + " · " : ""}${e.title}<div class="muted xsmall">${e.reason || ""}</div></td>
      <td class="small">${pf}</td>
      <td>${_verdictBadge(e.bias, e.verdict)}</td></tr>`;
  }).join("");
}

function _retCell(v) {
  if (v == null) return `<td class="small">–</td>`;
  const p = (v * 100);
  const cls = p >= 0 ? "up" : "down";
  return `<td class="small ${cls}">${p >= 0 ? "+" : ""}${p.toFixed(2)}%</td>`;
}

function renderMacroBacktest(rep) {
  const p = rep.params || {};
  const has = (p.n_events || 0) > 0;
  $("mc-bt-empty").style.display = has ? "none" : "";
  $("mc-bt-kpis").style.display = has ? "" : "none";
  $("mc-meta").textContent = `${p.years || 3} tahun · ${p.n_events || 0} rilis diuji`
    + ` (RISK_ON ${p.n_risk_on || 0} / RISK_OFF ${p.n_risk_off || 0})`
    + ` · sumber: ${p.source || "?"}`
    + (rep.generated_ts ? ` · dibuat ${new Date(rep.generated_ts).toLocaleString("id-ID")}` : "")
    + (p.demo ? " · (DEMO)" : "");

  const bh = rep.by_horizon || {};
  const h3 = bh["3d"] || {};
  $("mc-hit").textContent = (h3.all_hit?.hit_rate ?? 0) + "%";
  $("mc-n").textContent = h3.all_hit?.n ?? 0;

  // horizon breakdown: RISK_ON vs RISK_OFF average forward return
  $("mc-horizon").innerHTML = ["1d", "3d", "7d"].map(h => {
    const g = bh[h]; if (!g) return "";
    const on = g.risk_on || {}, off = g.risk_off || {};
    return `<div class="mc-hz">
      <span class="mc-hz-lab">${h.replace("d", " hari")}</span>
      <span class="mc-hz-on">RISK_ON ${_pct(on.avg_ret)} <span class="muted xsmall">(${on.n||0}, win ${on.win_rate||0}%)</span></span>
      <span class="mc-hz-off">RISK_OFF ${_pct(off.avg_ret)} <span class="muted xsmall">(${off.n||0}, win ${off.win_rate||0}%)</span></span>
    </div>`;
  }).join("");

  // per-type accuracy
  const types = rep.by_type || {};
  document.querySelector("#mc-types tbody").innerHTML = Object.keys(types).length
    ? Object.entries(types).sort((a, b) => (b[1].hit_3d - a[1].hit_3d)).map(([t, v]) =>
        `<tr><td>${t}</td><td>${v.n}</td><td>${v.hit_3d}%</td>
         <td class="${v.avg_ret_3d >= 0 ? "up" : "down"}">${_pct(v.avg_ret_3d)}</td></tr>`).join("")
    : `<tr><td colspan="4" class="muted small">–</td></tr>`;

  // strategy vs buy&hold equity curve
  const st = rep.strategy || {};
  $("mc-strat-note").innerHTML = `Long BTC ${st.hold_days || 3} hari tiap rilis <b>RISK_ON</b>, flat setelah RISK_OFF`
    + ` · di pasar ${st.exposure_pct || 0}% waktu · <b>strategi ${_pct(st.strat_return_pct)}</b>`
    + ` vs buy&hold ${_pct(st.buyhold_return_pct)}. Uji konsep, bukan sinyal live.`;
  drawDualCurve($("mc-equity"), st.curve || []);

  // recent events journal
  document.querySelector("#mc-journal tbody").innerHTML = (rep.recent_events || []).map(e => {
    const d = e.ts ? new Date(e.ts).toLocaleDateString("id-ID", { day: "2-digit", month: "short", year: "2-digit" }) : "–";
    const pa = (e.previous ?? "–") + " → " + (e.actual ?? "–");
    return `<tr><td class="small">${d}</td><td>${e.title}</td><td class="small">${pa}</td>
      <td>${_verdictBadge(e.bias, e.verdict)}</td>
      ${_retCell(e.fwd_1d)}${_retCell(e.fwd_3d)}${_retCell(e.fwd_7d)}</tr>`;
  }).join("");
}

function drawDualCurve(cv, curve) {
  if (!cv) return;
  const dpr = window.devicePixelRatio || 1;
  const W = cv.clientWidth || 600, H = cv.clientHeight || 240;
  cv.width = W * dpr; cv.height = H * dpr;
  const ctx = cv.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, W, H);
  if (!curve.length) return;
  const padL = 48, padB = 22, padT = 12, padR = 12;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const all = curve.flatMap(p => [p.strat, p.bh]);
  let lo = Math.min(0, ...all), hi = Math.max(0, ...all);
  if (hi === lo) hi = lo + 1;
  const x = i => padL + plotW * (i / Math.max(1, curve.length - 1));
  const y = v => padT + plotH * (1 - (v - lo) / (hi - lo));
  const muted = cssVar("--muted"), accent = cssVar("--accent"), green = cssVar("--green");
  ctx.strokeStyle = muted; ctx.globalAlpha = 0.4; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(padL, y(0)); ctx.lineTo(W - padR, y(0)); ctx.stroke();
  ctx.globalAlpha = 1;
  ctx.fillStyle = muted; ctx.font = "10px system-ui"; ctx.textAlign = "right"; ctx.textBaseline = "middle";
  ctx.fillText(hi.toFixed(0) + "%", padL - 5, y(hi));
  ctx.fillText(lo.toFixed(0) + "%", padL - 5, y(lo));
  const line = (key, col, w) => {
    ctx.beginPath();
    curve.forEach((p, i) => { const px = x(i), py = y(p[key]); i ? ctx.lineTo(px, py) : ctx.moveTo(px, py); });
    ctx.strokeStyle = col; ctx.lineWidth = w; ctx.stroke();
  };
  line("bh", muted, 1.5);          // buy & hold (reference)
  line("strat", green, 2);         // screening strategy
  // legend
  ctx.textAlign = "left"; ctx.font = "11px system-ui";
  ctx.fillStyle = green; ctx.fillText("■ strategi screening", padL + 4, padT + 6);
  ctx.fillStyle = muted; ctx.fillText("■ buy & hold", padL + 140, padT + 6);
}

/* ============ TRADE SAYA (personal tracker, disimpan di perangkat) ============ */
const MT_KEY = "fib-my-trades";
const MY_DAILY_LIMIT = 10;   // batas jurnal "Trade Saya" (manual, bukan cap strategi bot=5)
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
  if (changed) {
    saveMT(list);
    list.filter(t => t.status === "CLOSED" && !t._notified).forEach(t => {
      t._notified = true;
      notify(`${t.symbol} ${t.outcome} ${t.r >= 0 ? "+" : ""}${t.r}R`,
             `Trade ${t.direction} Anda ditutup di ${fmt(t.exit_price)}`);
    });
    saveMT(list);
  }
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
  renderJournal(lastSnap ? lastSnap.recent_trades : []);   // jurnal ikut terupdate
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
  const overLimit = todayCount > MY_DAILY_LIMIT;
  $("my-risk").innerHTML = [
    ["Trade hari ini", `${todayCount} / ${MY_DAILY_LIMIT}${overLimit ? " ⚠️" : ""}`],
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
        <span>${new Date(t.closed_ts).toLocaleDateString("id-ID")}
          <button class="mt-del" title="Hapus" onclick="deleteMT(${t.id})">🗑</button></span></div>
    </div>`;
  }).join("");

  $("my-trades").innerHTML = (open.length || closed.length)
    ? (openHTML + closedHTML)
    : `<p class="mt-empty">Belum ada trade. Tekan "✅ Saya Entry" di kartu sinyal saat Anda mengambil posisi — trade dicatat & dilacak di sini (tersimpan di perangkat Anda).</p>`;

  // kurva ekuitas pribadi (kumulatif R dari trade yang selesai)
  const eqCv = $("my-equity");
  const cl = closed.slice().sort((a, b) => new Date(a.closed_ts) - new Date(b.closed_ts));
  if (cl.length >= 2) {
    eqCv.classList.remove("hidden");
    let cum = 0;
    const curve = cl.map(t => ({ ts: t.closed_ts, r: (cum += (t.r || 0)) }));
    drawEquity(eqCv, curve);
  } else {
    eqCv.classList.add("hidden");
  }
}

/* ---------- render ---------- */
let lastSnap = null;
function render(s) {
  lastSnap = s;
  renderMyTrades(s);
  renderRegime(s.regime);
  renderKpis(s.stats, s.risk);
  renderSignals(s.signals || []);
  checkSignalNotifs(s.signals || []);
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

// KPI atas default ke trade milik pengguna ("saya"), bukan statistik bot.
// Migrasi sekali agar pengguna lama (default lama "bot") ikut pindah ke "saya".
if (!localStorage.getItem("fib-kpi-v2")) {
  localStorage.setItem("fib-kpi", "saya");
  localStorage.setItem("fib-kpi-v2", "1");
}
let kpiMode = localStorage.getItem("fib-kpi") || "saya";
function renderKpis(st = {}, risk = {}) {
  const el = $("k-pnl");
  if (kpiMode === "saya") {
    const m = computeMyStats();
    $("kl-pnl").textContent = "Total R";
    $("k-winrate").textContent = m.win_rate + "%";
    $("k-pf").textContent = m.pf || 0;
    $("k-resolved").textContent = m.resolved;
    $("k-open").textContent = m.open;
    $("k-today").textContent = `${m.today}/${MY_DAILY_LIMIT}`;
    el.textContent = (m.total_r >= 0 ? "+" : "") + m.total_r + "R";
    el.style.color = m.total_r > 0 ? "var(--green)" : m.total_r < 0 ? "var(--red)" : "var(--text)";
  } else {
    $("kl-pnl").textContent = "PnL hari ini";
    $("k-winrate").textContent = (st.win_rate ?? 0) + "%";
    $("k-pf").textContent = st.profit_factor ?? "–";
    $("k-resolved").textContent = st.resolved ?? 0;
    $("k-open").textContent = st.open ?? 0;
    $("k-today").textContent = `${risk.trades_today ?? 0}/${risk.max_trades ?? 5}`;
    const pnl = risk.pnl_today_pct ?? 0;
    el.textContent = (pnl >= 0 ? "+" : "") + pnl + "%";
    el.style.color = pnl > 0 ? "var(--green)" : pnl < 0 ? "var(--red)" : "var(--text)";
  }
}

function computeMyStats() {
  const list = loadMT();
  const open = list.filter(t => t.status === "OPEN");
  const closed = list.filter(t => t.status === "CLOSED");
  const wins = closed.filter(t => t.outcome === "WIN").length;
  const losses = closed.filter(t => t.outcome === "LOSS").length;
  const gw = closed.filter(t => t.r > 0).reduce((a, t) => a + t.r, 0);
  const gl = -closed.filter(t => t.r < 0).reduce((a, t) => a + t.r, 0);
  const pf = gl > 0 ? gw / gl : gw;
  const today = new Date().toDateString();
  const todayCount = list.filter(t => new Date(t.opened_ts).toDateString() === today).length;
  const totalR = closed.reduce((a, t) => a + (t.r || 0), 0);
  return {
    win_rate: closed.length ? Math.round(wins / closed.length * 100) : 0,
    pf: Math.round(pf * 100) / 100, resolved: closed.length, open: open.length,
    today: todayCount, total_r: Math.round(totalR * 100) / 100, wins, losses,
  };
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
        ${s.score != null ? `<span class="badge score" title="Skor Setup — konfluensi setup (0-95), entry ≥60">Skor ${s.score}</span>` : ""}
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
      <div><span class="lbl lv-tp">TP2 ${p.tp_source === "likuiditas" ? "💧" : ""}</span><span class="lv-tp">${fmt(p.tp2)}</span></div>
      ${p.tp3 != null ? `<div><span class="lbl lv-tp">TP3</span><span class="lv-tp">${fmt(p.tp3)}</span></div>` : ""}
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
  const dirCls = (d) => d === "NAIK" ? true : d === "TURUN" ? false : null;
  const altCls = r.alt_prediction === "NAIK" ? true : r.alt_prediction === "TURUN" ? false : null;
  const usdtdPosTxt = r.usdtd_pos != null
    ? Math.round(r.usdtd_pos * 100) + "%" + (r.usdtd_at_support ? " (support)" : r.usdtd_at_resistance ? " (resistance)" : "")
    : "–";
  const rows = [
    ["USDT.D", r.usdtd_value != null ? r.usdtd_value + "%" : (r.usdtd_ok ? "–" : "n/a"), null],
    ["USDT.D posisi", usdtdPosTxt, r.usdtd_at_support ? true : r.usdtd_at_resistance ? false : null],
    ["USDT.D arah", r.usdtd_target || (r.usdtd_rising == null ? "–" : (r.usdtd_rising ? "menuju resistance" : "menuju support")),
      r.alt_bias === "LONG" ? true : r.alt_bias === "SHORT" ? false : null],
    [`🎯 Keputusan (${r.decider || "USDT.D"})`, r.alt_bias === "LONG" ? "LONG (alt naik)" : r.alt_bias === "SHORT" ? "SHORT (alt turun)" : "NETRAL",
      r.alt_bias === "LONG" ? true : r.alt_bias === "SHORT" ? false : null],
    [r.usdtd_consolidating ? "— matriks BTC.D (aktif) —" : "— info —", "arah BTC & dominance", null],
    ["Arah BTC", r.btc_dir ? r.btc_dir.toLowerCase() : "–", dirCls(r.btc_dir)],
    ["BTC.D", r.btcd_value != null ? `${r.btcd_value}% (${(r.btcd_dir || "").toLowerCase()})` : "–", null],
    ["Prediksi ALT (matriks)", r.alt_prediction || "–", altCls],
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
    ["Trade hari ini", `${r.trades_today ?? 0} / ${r.max_trades ?? 5}`],
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

function buildJournalRows(botTrades) {
  const mine = loadMT().map(t => ({
    id: t.id, source: "Saya", created_ts: t.opened_ts, symbol: t.symbol, direction: t.direction,
    entry: t.entry, exit_price: t.exit_price, outcome: t.outcome, r_multiple: t.r,
    confidence: t.confidence, status: t.status === "CLOSED" ? "RESOLVED" : "OPEN",
  }));
  const bot = (botTrades || []).map(t => ({ ...t, source: "Bot" }));
  return [...mine, ...bot].sort((a, b) => new Date(b.created_ts) - new Date(a.created_ts));
}

function applyJournalFilter(rows) {
  const sym = ($("f-symbol").value || "").toUpperCase();
  const src = $("f-source").value;
  const out = $("f-outcome").value;
  return rows.filter(t => {
    if (sym && !t.symbol.includes(sym)) return false;
    if (src && t.source !== src) return false;
    if (out === "OPEN" && t.status !== "OPEN") return false;
    if ((out === "WIN" || out === "LOSS") && t.outcome !== out) return false;
    return true;
  });
}

let lastBotTrades = [];
function renderJournal(botTrades) {
  lastBotTrades = botTrades || lastBotTrades;
  const tb = document.querySelector("#journal tbody");
  const all = applyJournalFilter(buildJournalRows(lastBotTrades)).slice(0, 80);
  if (!all.length) { tb.innerHTML = `<tr><td colspan="11" class="empty">Belum ada trade.</td></tr>`; return; }
  tb.innerHTML = all.map(t => {
    const o = (t.outcome || "").toLowerCase();
    const ocls = o === "win" ? "o-win" : o === "loss" ? "o-loss" : o ? "o-be" : "";
    const srcCls = t.source === "Saya" ? "src-me" : "src-bot";
    const del = t.source === "Saya" ? `<button class="row-del" title="Hapus" onclick="deleteMT(${t.id})">🗑</button>` : "";
    return `<tr>
      <td>${new Date(t.created_ts).toLocaleString("id-ID", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })}</td>
      <td><span class="src-tag ${srcCls}">${t.source}</span></td>
      <td>${t.symbol}</td>
      <td class="${t.direction === "LONG" ? "o-win" : "o-loss"}">${t.direction}</td>
      <td>${fmt(t.entry)}</td>
      <td>${t.exit_price ? fmt(t.exit_price) : "–"}</td>
      <td class="${ocls}">${t.outcome || "—"}</td>
      <td>${t.r_multiple != null ? t.r_multiple : "–"}</td>
      <td>${Math.round((t.confidence || 0) * 100)}%</td>
      <td>${t.status}</td>
      <td>${del}</td>
    </tr>`;
  }).join("");
}

/* ============ COMPLETENESS FEATURES ============ */

// Re-render everything that depends on local (personal) data.
function refreshAll() {
  renderMyTrades(lastSnap);
  renderJournal(lastSnap ? lastSnap.recent_trades : lastBotTrades);
  renderKpis(lastSnap ? lastSnap.stats : {}, lastSnap ? lastSnap.risk : {});
  if (lastSnap) renderSignals(lastSnap.signals || []);
}

// ---- delete / clear personal trades ----
function deleteMT(id) {
  saveMT(loadMT().filter(t => t.id !== id));
  refreshAll();
}
window.deleteMT = deleteMT;

$("mt-clear").onclick = () => {
  const closed = loadMT().filter(t => t.status === "CLOSED").length;
  if (!closed) { alert("Tidak ada trade selesai untuk dihapus."); return; }
  if (!confirm(`Hapus ${closed} trade yang sudah selesai?`)) return;
  saveMT(loadMT().filter(t => t.status === "OPEN"));
  refreshAll();
};

// ---- KPI Bot/Saya toggle ----
document.querySelectorAll("#kpi-toggle button").forEach(b => {
  if (b.dataset.src === kpiMode) b.classList.add("active"); else b.classList.remove("active");
  b.onclick = () => {
    kpiMode = b.dataset.src;
    localStorage.setItem("fib-kpi", kpiMode);
    document.querySelectorAll("#kpi-toggle button").forEach(x => x.classList.toggle("active", x === b));
    renderKpis(lastSnap ? lastSnap.stats : {}, lastSnap ? lastSnap.risk : {});
  };
});

// ---- journal filters ----
["f-symbol", "f-source", "f-outcome"].forEach(id => {
  const ev = id === "f-symbol" ? "input" : "change";
  $(id).addEventListener(ev, () => renderJournal(lastBotTrades));
});

// ---- CSV export ----
$("csv-btn").onclick = () => {
  const rows = applyJournalFilter(buildJournalRows(lastBotTrades));
  const head = ["Waktu", "Sumber", "Simbol", "Arah", "Entry", "Exit", "Hasil", "R", "Conf", "Status"];
  const lines = [head.join(",")].concat(rows.map(t => [
    new Date(t.created_ts).toISOString(), t.source, t.symbol, t.direction,
    t.entry, t.exit_price ?? "", t.outcome ?? "", t.r_multiple ?? "",
    Math.round((t.confidence || 0) * 100) + "%", t.status,
  ].join(",")));
  const blob = new Blob([lines.join("\n")], { type: "text/csv" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `jurnal-fib-${new Date().toISOString().slice(0, 10)}.csv`;
  a.click();
  URL.revokeObjectURL(a.href);
};

// ---- browser notifications ----
let notifOn = localStorage.getItem("fib-notif") === "1";
function updateNotifBtn() {
  const b = $("notif-btn");
  b.textContent = notifOn ? "🔔" : "🔕";
  b.title = notifOn ? "Notifikasi aktif" : "Notifikasi mati";
}
updateNotifBtn();
$("notif-btn").onclick = async () => {
  if (!("Notification" in window)) { alert("Browser tidak mendukung notifikasi."); return; }
  if (Notification.permission !== "granted") {
    const p = await Notification.requestPermission();
    if (p !== "granted") { notifOn = false; localStorage.setItem("fib-notif", "0"); updateNotifBtn(); return; }
  }
  notifOn = !notifOn;
  localStorage.setItem("fib-notif", notifOn ? "1" : "0");
  updateNotifBtn();
  if (notifOn) notify("Notifikasi aktif", "Anda akan diberi tahu saat ada sinyal ENTRY & saat trade Anda kena TP/SL.");
};
function notify(title, body) {
  if (!notifOn || !("Notification" in window) || Notification.permission !== "granted") return;
  try { new Notification(title, { body, icon: "icon-192.png" }); } catch (e) { /* ignore */ }
}
const _entryState = {};
function checkSignalNotifs(signals) {
  signals.forEach(s => {
    const prev = _entryState[s.symbol];
    if (s.state === "ENTRY" && s.actionable && prev !== "ENTRY") {
      const p = s.plan || {};
      notify(`🎯 ENTRY ${s.direction} ${s.symbol}`,
             `Entry ${fmt(s.price)} · SL ${fmt(p.sl)} · TP2 ${fmt(p.tp2)} · RR ${p.rr}`);
    }
    _entryState[s.symbol] = s.state;
  });
}

// ---- PWA: install prompt + service worker ----
let deferredPrompt = null;
window.addEventListener("beforeinstallprompt", (e) => {
  e.preventDefault();
  deferredPrompt = e;
  $("install-btn").classList.remove("hidden");
});
$("install-btn").onclick = async () => {
  if (!deferredPrompt) return;
  deferredPrompt.prompt();
  await deferredPrompt.userChoice;
  deferredPrompt = null;
  $("install-btn").classList.add("hidden");
};
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => navigator.serviceWorker.register("sw.js").catch(() => {}));
}
