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
themeBtn.onclick = () =>
  applyTheme(document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark");

/* ---------- manual scan ---------- */
$("scan-btn").onclick = async () => {
  $("scan-btn").textContent = "…";
  try { await fetch("/api/scan", { method: "POST" }); } catch (e) {}
  $("scan-btn").textContent = "↻ Scan";
};

/* ---------- websocket with polling fallback ---------- */
function setConn(state) {
  const dot = $("conn-dot"), txt = $("conn-text");
  dot.className = "dot " + (state === "live" ? "live" : state === "off" ? "off" : "");
  txt.textContent = state === "live" ? "live" : state === "off" ? "terputus" : "menghubungkan";
}
let ws, pollTimer;
function connect() {
  try {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    ws = new WebSocket(`${proto}://${location.host}/ws`);
    ws.onopen = () => { setConn("live"); clearInterval(pollTimer); };
    ws.onmessage = (e) => render(JSON.parse(e.data));
    ws.onclose = () => { setConn("off"); startPolling(); setTimeout(connect, 4000); };
    ws.onerror = () => ws.close();
  } catch (e) { startPolling(); }
}
function startPolling() {
  clearInterval(pollTimer);
  pollTimer = setInterval(async () => {
    try { const r = await fetch("/api/snapshot"); render(await r.json()); setConn("live"); }
    catch (e) { setConn("off"); }
  }, 5000);
}
connect();

/* ---------- render ---------- */
function render(s) {
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
}

function cardHTML(s) {
  const dir = s.direction.toLowerCase();
  const stateCls = "state-" + s.state.toLowerCase();
  const p = s.plan || {};
  const conf = Math.round((s.confidence || 0) * 100);
  const gateCls = !s.allowed ? "blocked" : s.actionable ? "ok" : "";
  const checks = (s.checklist || []).concat(s.trigger || []);
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
