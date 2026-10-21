const P = "/api/paper";
let TOKEN = null;

async function api(path, method = "GET", body = null) {
  const opts = { method, headers: { "Content-Type": "application/json" } };
  if (method !== "GET") {
    if (!TOKEN) TOKEN = (await (await fetch(P + "/token")).json()).token;
    opts.headers["X-Paper-Token"] = TOKEN;
  }
  if (body) opts.body = JSON.stringify(body);
  const res = await fetch(P + path, opts);
  if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || res.statusText);
  return res.json();
}

// Token names, reasons and research text come from public APIs / LLMs: always escape.
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const usd = (v, d = 2) => (v === null || v === undefined ? "–" : `$${Number(v).toFixed(d)}`);
const pnl = (v) => {
  if (v === null || v === undefined) return "–";
  const n = Number(v);
  return `<span class="${n >= 0 ? "green" : "red"}">${n >= 0 ? "+" : "−"}$${Math.abs(n).toFixed(2)}</span>`;
};
const pct = (v) => (v === null || v === undefined ? "–" : `${(v * 100).toFixed(2)}%`);
const time = (iso) => (iso ? new Date(iso).toLocaleString() : "–");
const short = (m) => (m ? `${m.slice(0, 4)}…${m.slice(-4)}` : "");
const ago = (iso) => {
  const s = Math.max(0, (Date.now() - new Date(iso)) / 1000);
  return s < 60 ? `${Math.round(s)}s` : s < 3600 ? `${Math.round(s / 60)} min` : `${Math.round(s / 3600)} h`;
};

function table(id, head, rows) {
  document.getElementById(id).innerHTML =
    `<thead><tr>${head.map((h) => `<th>${h}</th>`).join("")}</tr></thead>` +
    `<tbody>${rows.length ? rows.join("") : `<tr><td colspan="${head.length}" class="muted">Ainda nada</td></tr>`}</tbody>`;
}

// ─── controls: every button shows "working…", then a visible result ───
let lastStatus = null, lastLive = null;
let toastTimer;
function toast(msg, err = false) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.className = `toast${err ? " err" : ""}`;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), err ? 7000 : 3500);
}
async function run(btn, busyLabel, fn, okMsg) {
  const label = btn.textContent;
  btn.disabled = true;
  btn.classList.add("busy");
  btn.textContent = busyLabel;
  try {
    const res = await fn();
    toast(typeof okMsg === "function" ? okMsg(res) : okMsg);
    btn.classList.add("done");
    setTimeout(() => btn.classList.remove("done"), 1500);
  } catch (e) {
    toast(`✖ ${e.message}`, true);
    btn.textContent = label;
  } finally {
    btn.disabled = false;
    btn.classList.remove("busy");
    safe(refreshLive); safe(refreshLiveTrading);
  }
}
function toggleRun(btn) {
  const on = lastStatus?.running;
  run(btn, on ? "A parar…" : "A iniciar…", () => api(on ? "/stop" : "/start", "POST"),
      on ? "⏸ Bot parado — não abre novas posições" : "✓ Bot a correr 24/7 (um ciclo a cada 5 min)");
}
function scanNow(btn) {
  run(btn, "A pedir análise…", () => api("/cycle", "POST"),
      (r) => (r.started ? "🔍 Análise iniciada — veja a barra roxa e a sala dos agents" : "Já está uma análise a correr"));
}
function kill(btn) {
  const active = lastStatus?.kill_switch?.active;
  if (!confirm(active ? "Rearmar o kill switch e permitir novas entradas?" : "Ativar o kill switch? Bloqueia novas entradas; as saídas continuam.")) return;
  run(btn, active ? "A rearmar…" : "A ativar…",
      () => api(active ? "/kill-switch/reset" : "/kill-switch", "POST", { reason: "dashboard" }),
      active ? "✓ Kill switch rearmado" : "🛑 Kill switch ativo — sem novas entradas");
}
function setMode(mode, btn) {
  run(btn, mode === "llm" ? "A ligar LLM…" : "A ligar regras…", async () => {
    await api("/decision-mode", "POST", { mode, target: "paper" });
    await api("/decision-mode", "POST", { mode, target: "live" });
  }, mode === "llm" ? "🧠 O LLM passa a decidir compras e vendas (o Risk mantém os limites)" : "✓ Decisões por regras determinísticas");
}

// ─── real trading (Hyperliquid) ───
function toggleArm(btn) {
  const armed = lastLive?.armed;
  if (!armed) {
    const net = lastLive?.network === "mainnet" ? "MAINNET COM DINHEIRO REAL" : "testnet (dinheiro de teste)";
    if (!confirm(`Ativar trading automático em ${net}?\nLimites: capital ${usd(lastLive?.limits?.max_capital_usd, 0)}, posição ${usd(lastLive?.limits?.max_position_usd, 0)}, 1x, stop obrigatório na exchange.`)) return;
  }
  run(btn, armed ? "A desativar…" : "A verificar conta e ativar…", () => api(armed ? "/live/disarm" : "/live/arm", "POST"),
      armed ? "⏸ Trading real desativado (stops na exchange mantêm-se)" : (r) => `✓ Trading automático ATIVO em ${r.network} — saldo ${usd(r.balance)}`);
}
function flatten(btn) {
  if (!confirm("Fechar TODAS as posições reais a mercado e desativar o trading automático?")) return;
  run(btn, "A fechar posições…", () => api("/live/flatten", "POST"),
      (r) => (r.errors.length ? `Fechadas: ${r.closed.join(", ") || "nenhuma"} · erros: ${r.errors.join("; ")}` : `✓ Fechadas: ${r.closed.join(", ") || "nenhuma posição aberta"}`));
}
function testOrder(btn) {
  run(btn, "A enviar ordem de teste…", () => api("/live/test-order", "POST"),
      (r) => (r.position ? `✓ Ordem de teste executada: ${r.position.symbol} com stop e alvo na exchange` : `Sem ordem: ${(r.cycle?.error || "ver o feed")}`));
}
async function refreshLiveTrading() {
  const s = await api("/live/status");
  lastLive = s;
  const net = document.getElementById("net-pill");
  net.textContent = s.network.toUpperCase();
  net.className = `pill ${s.network}`;
  const arm = document.getElementById("arm-pill");
  arm.textContent = s.kill_switch.active ? "KILL SWITCH" : s.armed ? "ATIVO" : "DESATIVADO";
  arm.className = `pill ${s.armed && !s.kill_switch.active ? "armed" : ""}`;
  const ready = !s.config.problems.length;
  const btn = document.getElementById("btn-arm");
  if (!btn.classList.contains("busy")) btn.textContent = s.armed ? "Desativar trading automático" : "Ativar trading automático";
  btn.disabled = !ready && !s.armed;
  document.getElementById("btn-test").hidden = !(s.armed && s.network === "testnet");
  document.getElementById("live-bal").textContent = s.exchange_balance !== undefined ? usd(s.exchange_balance) : "–";
  document.getElementById("live-pos").textContent = s.positions.length ? s.positions.map((p) => p.symbol).join(", ") : "0";
  document.getElementById("live-pnl").innerHTML = pnl(s.trades.reduce((x, t) => x + t.pnl_usd, 0));
  const L = s.limits;
  document.getElementById("live-limits").innerHTML = `capital ${usd(L.max_capital_usd, 0)} · posição ${usd(L.max_position_usd, 0)} · ${L.max_positions} de cada vez<br>1x só compra · perda diária ${pct(L.max_daily_loss_pct)} · stop na exchange`;
  document.getElementById("live-setup").innerHTML = ready ? (s.exchange_error ? `<span class="red">Hyperliquid: ${esc(s.exchange_error)}</span>` : "")
    : `<b>Para ativar falta:</b><ul class="check">${s.config.problems.map((p) => `<li>⬜ ${esc(p)}</li>`).join("")}</ul>
       Passos: MetaMask → depositar USDC na Hyperliquid → criar API wallet em More → API → pôr <code>HL_ACCOUNT_ADDRESS</code> e <code>HL_API_WALLET_KEY</code> no <code>.env</code> → reiniciar.`;
  document.getElementById("live-feed").innerHTML = s.decisions.slice(0, 8).map((d) => `
    <div class="fi"><span class="w">${ago(d.ts)}</span><span><span class="k-${esc(d.kind)}">${KIND[d.kind] || esc(d.kind)}</span>
    <b>${esc(d.symbol || "")}</b> — ${esc(d.reason)}</span></div>`).join("");
}

// ─── live agent room ───
const AGENTS = [
  ["scout", "🔭", "Scout", "Procura mercados com volume e liquidez"],
  ["llm", "🧠", "LLM trader", "Decide comprar ou vender (modo LLM)"],
  ["safety", "🛡️", "Safety", "Anti-rug: rejeita o que não consegue verificar"],
  ["mean_reversion", "🎯", "Mean reversion", "Compra quedas exageradas"],
  ["momentum", "🚀", "Momentum", "Compra rompimentos com volume"],
  ["research", "📚", "Research", "Métricas, notícias e riscos do token"],
  ["debate", "⚖️", "Debate LLM", "Bull vs bear — só pode vetar"],
  ["risk", "🧮", "Risk", "Limites e tamanho — palavra final"],
  ["executor", "⚙️", "Executor", "Executa no broker paper"],
];

function renderRoom(agents, s) {
  const byName = Object.fromEntries(agents.map((a) => [a.name, a]));
  const shown = AGENTS.filter(([n]) => byName[n]);       // only agents that exist in this build
  const live = s.live || {};
  const cycleStart = s.heartbeat ? new Date(s.heartbeat.ts).getTime() : 0;
  const cols = window.innerWidth <= 900 ? 0 : 4;
  const DOWNSTREAM = new Set(["research", "debate", "risk", "executor"]);
  // snake layout: a reversed, incomplete last row needs spacers on its left so it lines up under the row above
  let spacers = "";
  if (cols) {
    const lastRow = Math.floor((shown.length - 1) / cols), missing = (lastRow + 1) * cols - shown.length;
    if (lastRow % 2 === 1) for (let j = 0; j < missing; j++) spacers += `<div style="order:${lastRow * cols + j}"></div>`;
  }
  document.getElementById("stations").innerHTML = spacers + shown.map(([n, icon, label, role], i) => {
    const a = byName[n] || {};
    const acted = a.last_run && new Date(a.last_run).getTime() >= cycleStart - 1000;
    const working = live.running && live.agent === n && Date.now() - new Date(live.ts) < 30000;
    const cls = working ? "working" : !acted ? "idle" : a.last_ok === false ? "blocked" : "ok";
    const stateTxt = working ? '<span class="typing">a trabalhar</span>' : !acted ? "à espera do próximo ciclo"
      : a.last_ok === false ? "bloqueou neste ciclo" : "concluiu neste ciclo";
    // snake layout on wide screens: row 1 left→right, row 2 right→left
    let dir = "", order = i;
    if (cols) {
      const row = Math.floor(i / cols), col = i % cols, last = i === shown.length - 1;
      if (row % 2 === 1) order = row * cols + (cols - 1 - col);
      if (!last) dir = col === cols - 1 ? "d" : row % 2 === 0 ? "r" : "l";
    }
    return `<div class="st ${cls} ${working ? "flow" : ""}" data-dir="${dir}" style="order:${order}" title="${esc(role)}">
      <div class="top"><div class="av">${icon}</div><div><div class="nm">${esc(label)}</div><div class="rl">${esc(role)}</div></div></div>
      <div class="state"><span class="dot"></span>${stateTxt}</div>
      <div class="bubble">${esc((working ? live.detail : a.last_reason) || (DOWNSTREAM.has(n) ? "Só entra em ação quando uma estratégia dá sinal" : "Ainda não correu"))}</div>
      <div class="cnt">✔ ${a.approved ?? 0} · ✖ ${a.vetoed ?? 0}${a.trades ? ` · ${a.trades} trades ${pnl(a.pnl_usd)}` : ""}${a.capital_usd !== undefined ? ` · capital ${usd(a.capital_usd, 0)}` : ""}</div>
    </div>`;
  }).join("");
}

let nextCycleAt = null;
function renderHead(s) {
  const live = s.live || {};
  const el = document.getElementById("room-head");
  if (live.running && live.agent) {
    const label = (AGENTS.find(([n]) => n === live.agent) || [])[2] || live.agent;
    el.innerHTML = `Ciclo <b>#${s.cycle}</b> em curso · <b>${esc(label)}</b> a trabalhar`;
  } else if (s.running) {
    const left = nextCycleAt ? Math.round((nextCycleAt - Date.now()) / 1000) : null;
    el.innerHTML = `Ciclo <b>#${s.cycle}</b> concluído · próximo em <b>${left > 0 ? `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}` : "instantes"}</b>`;
  } else {
    el.innerHTML = "Bot <b>parado</b> — clique em Iniciar 24/7";
  }
}

const KIND = { entry: "🟢 compra", exit: "🔵 venda", veto: "🟡 bloqueio", error: "🔴 erro", kill_switch: "🛑 kill switch",
               reconcile: "🧾 reconciliação", pause: "⏸ pausa", resume: "▶ retoma", arm: "🔓 ativado", disarm: "🔒 desativado",
               mode: "🔀 modo" };

function renderFeed(decisions, s) {
  const lc = s.last_cycle || {};
  const head = lc.cycle ? `<div class="fi"><span class="w">${ago(lc.finished)}</span><span>⚙️ Ciclo #${lc.cycle}: ${lc.candidates} mercados, ${lc.safe} seguros, ${lc.signals} sinais, ${lc.entries} compras</span></div>` : "";
  document.getElementById("feed").innerHTML = head + decisions.map((d) => `
    <div class="fi"><span class="w">${ago(d.ts)}</span>
    <span><span class="k-${esc(d.kind)}">${KIND[d.kind] || esc(d.kind)}</span> <b>${esc(d.symbol || "")}</b>
    ${d.strategy ? `<span class="muted">(${esc(d.strategy)})</span>` : ""} — ${esc(d.reason)}</span></div>`).join("");
}

function renderKpis(s, positions) {
  lastStatus = s;
  document.getElementById("k-equity").textContent = usd(s.equity);
  document.getElementById("k-cash").textContent = `cash ${usd(s.cash)} · em posições ${usd(s.exposure)}`;
  document.getElementById("k-pnl").innerHTML = pnl(s.total_pnl);
  document.getElementById("k-pnl-s").textContent = `realizado ${usd(s.realized_pnl)} · aberto ${usd(s.unrealized_pnl)}`;
  const d = s.performance?.day;
  document.getElementById("k-day").innerHTML = d ? `<span class="${d.twr >= 0 ? "green" : "red"}">${pct(d.twr)}</span>` : "–";
  document.getElementById("k-day-s").textContent = d ? `segurar SOL ${pct(d.hold_sol)} · BTC ${pct(d.hold_btc)}` : "";
  document.getElementById("k-pos").textContent = `${positions} · ${s.closed_trades}`;
  document.getElementById("k-pos-s").textContent = s.win_rate === null ? "sem trades fechados" : `taxa de acerto ${pct(s.win_rate)}`;
  const rb = document.getElementById("run-badge");
  rb.textContent = s.kill_switch.active ? "KILL SWITCH" : s.running ? (s.heartbeat_ok ? "A CORRER" : "SEM SINAL") : "PARADO";
  rb.className = `badge ${s.running && s.heartbeat_ok && !s.kill_switch.active ? "badge-on" : "badge-off"}`;
  document.getElementById("btn-run").textContent = s.running ? "Parar" : "Iniciar 24/7";
  document.getElementById("btn-kill").textContent = s.kill_switch.active ? "Rearmar kill switch" : "Kill switch";
  nextCycleAt = s.running && s.heartbeat ? new Date(s.heartbeat.ts).getTime() + s.cycle_interval_sec * 1000 : null;
  // scanning indicator: visible whenever a cycle is running (scheduler or "Analisar agora")
  const scanning = s.scanning || (s.live?.running && Date.now() - new Date(s.live.ts) < 60000);
  const bar = document.getElementById("scanbar");
  bar.hidden = !scanning;
  if (scanning) {
    const who = (AGENTS.find(([n]) => n === s.live?.agent) || [])[2];
    document.getElementById("scan-text").textContent = `A analisar o mercado (ciclo #${s.cycle})${who ? ` — ${who} a trabalhar` : ""}…`;
  }
  const scanBtn = document.getElementById("btn-scan");
  if (!scanBtn.classList.contains("busy")) scanBtn.disabled = !!scanning;
  for (const m of ["rules", "llm"]) document.getElementById(`mode-${m}`).classList.toggle("on", s.decision_mode === m);
  document.getElementById("mode-llm").title = s.llm_available ? "O LLM decide compras e vendas" : "Precisa de ANTHROPIC_API_KEY no .env";
}

async function refreshLive() {
  const [s, { agents }, { decisions }, { positions }] = await Promise.all([
    api("/status"), api("/agents"), api("/decisions?limit=12"), api("/positions")]);
  renderKpis(s, positions.length);
  renderRoom(agents, s);
  renderHead(s);
  renderFeed(decisions, s);
  renderPositions(positions);
}

// ─── wallets (watch-only) ───
function metamaskProvider() {
  const eth = window.ethereum;
  if (!eth) return null;
  return (eth.providers || [eth]).find((p) => p.isMetaMask) || null;
}

async function connectWallet(kind) {
  try {
    let address, label;
    if (kind === "metamask") {
      const mm = metamaskProvider();
      if (!mm) throw new Error("MetaMask não encontrada neste browser. Abra http://127.0.0.1:8000/paper no Chrome/Brave/Firefox com a extensão MetaMask, ou cole o endereço 0x… abaixo.");
      // eth_requestAccounts only shares the public address — no signature, no spending permission.
      [address] = await mm.request({ method: "eth_requestAccounts" });
      label = "MetaMask";
    } else {
      const provider = kind === "phantom" ? window.phantom?.solana : window.solflare;
      if (!provider) throw new Error(`Extensão ${kind === "phantom" ? "Phantom" : "Solflare"} não encontrada. Instale-a ou cole o endereço público.`);
      const res = await provider.connect();
      address = (res?.publicKey || provider.publicKey).toString();
      label = kind === "phantom" ? "Phantom" : "Solflare";
    }
    await api("/wallets", "POST", { address, label });
    safe(loadWallets);
    toast(`✓ ${label} ligada: ${short(address)} (só leitura)`);
  } catch (e) { toast(`✖ ${e.message || e}`, true); }
}

async function addWallet(ev) {
  ev.preventDefault();
  const el = document.getElementById("w-addr");
  try { await api("/wallets", "POST", { address: el.value.trim(), label: el.value.trim().startsWith("0x") ? "EVM" : "Solana" }); el.value = ""; }
  catch (e) { toast(`✖ ${e.message}`, true); }
  safe(loadWallets);
  return false;
}

async function removeWallet(addr) {
  await api(`/wallets/${encodeURIComponent(addr)}`, "DELETE");
  safe(loadWallets);
}

function walletCard(w) {
  const head = `<h3><span>${esc(w.label || "Carteira")} <span class="muted">${short(w.address)}</span></span>
    <button onclick="removeWallet('${esc(w.address)}')">remover</button></h3>`;
  if (w.error) return `<div class="wcard">${head}<div class="red note">${esc(w.error)}</div></div>`;
  if (w.chain === "evm") {
    const hl = (k, name) => {
      const x = w[k] || {};
      return x.error ? `<div>${name}<b>–</b>indisponível</div>`
        : `<div>${name}<b>${usd(x.account_value)}</b>${x.positions || 0} posições${(x.spot || []).length ? ` · spot ${x.spot.map((b) => `${esc(b.coin)} ${Number(b.total).toFixed(2)}`).join(", ")}` : ""}</div>`;
    };
    return `<div class="wcard">${head}<div class="wbal">
      <div>USDC (Arbitrum)<b>${Number(w.arbitrum_usdc).toFixed(2)}</b>pronto a depositar</div>
      <div>ETH (Arbitrum)<b>${Number(w.arbitrum_eth).toFixed(5)}</b>para o gás</div>
      ${hl("hyperliquid", "Hyperliquid")}${hl("hyperliquid_testnet", "Hyperliquid testnet")}</div></div>`;
  }
  return `<div class="wcard">${head}<div class="wbal"><div>SOL<b>${Number(w.sol).toFixed(4)}</b>Solana</div>
    <div>Tokens<b>${w.tokens.length}</b>${w.tokens.slice(0, 3).map((t) => `${short(t.mint)} ${Number(t.amount).toPrecision(4)}`).join(" · ")}</div></div></div>`;
}

async function loadWallets() {
  const { wallets } = await api("/wallets");
  document.getElementById("wallets").innerHTML = wallets.length ? wallets.map(walletCard).join("")
    : '<p class="note" style="margin-top:10px">Nenhuma carteira ligada ainda.</p>';
}

// ─── performance, trades, positions ───
let chart, chartPeriod = "all";
function setChartPeriod(p, btn) {
  chartPeriod = p;
  document.querySelectorAll(".period-btn").forEach((b) => b.classList.toggle("active", b === btn));
  safe(loadChart);
}
async function loadChart() {
  let { history } = await api("/equity?limit=20000");
  const span = { day: 864e5, week: 7 * 864e5, month: 30 * 864e5 }[chartPeriod];
  if (span) history = history.filter((h) => Date.now() - new Date(h.ts) <= span);
  const labels = history.map((h) => new Date(h.ts).toLocaleString());
  const sets = [history.map((h) => h.equity), history.map((h) => h.contributed)];
  if (!chart) {
    chart = new Chart(document.getElementById("equity-chart"), {
      type: "line",
      data: { labels, datasets: [
        { label: "Património", data: sets[0], borderColor: "#7f5af0", pointRadius: 0, tension: 0.2 },
        { label: "Capital aportado", data: sets[1], borderColor: "#72757e", borderDash: [4, 4], pointRadius: 0 },
      ] },
      options: { animation: false, plugins: { legend: { labels: { color: "#72757e" } } },
        scales: { x: { ticks: { color: "#72757e", maxTicksLimit: 6 } }, y: { ticks: { color: "#72757e" } } } },
    });
  } else {
    chart.data.labels = labels;
    sets.forEach((d, i) => (chart.data.datasets[i].data = d));
    chart.update();
  }
  const perf = lastStatus?.performance || {};
  const names = { day: "Hoje", week: "7 dias", month: "30 dias", all: "Total" };
  document.getElementById("perf").innerHTML = Object.entries(names).map(([k, l]) => {
    const p = perf[k];
    return p ? `<div>${l}<b class="${p.twr >= 0 ? "green" : "red"}">${pct(p.twr)}</b>${pnl(p.gain_usd)} · SOL ${pct(p.hold_sol)}</div>` : `<div>${l}<b>–</b></div>`;
  }).join("");
}

let allTrades = [];
function fillSelect(id, values) {
  const el = document.getElementById(id), cur = el.value;
  el.innerHTML = `<option value="">${el.dataset.all}</option>` + [...new Set(values)].sort().map((v) => `<option>${esc(v)}</option>`).join("");
  el.value = cur;
}
function renderTrades() {
  const v = document.getElementById("f-venue").value, a = document.getElementById("f-asset").value,
        st = document.getElementById("f-strat").value;
  const rows = allTrades.filter((t) => (!v || t.venue === v) && (!a || t.symbol === a) && (!st || t.strategy === st));
  document.getElementById("trades-total").innerHTML = rows.length ? `${rows.length} · ${pnl(rows.reduce((x, t) => x + t.pnl_usd, 0))}` : "";
  table("trades", ["Ativo", "Entrada → saída", "Custos", "Lucro", "Motivo"], rows.map((t) => `
    <tr><td><b>${esc(t.symbol)}</b><br><span class="muted">${esc(t.venue)} · ${esc(t.strategy)}</span></td>
    <td>${Number(t.entry_price).toPrecision(5)} → ${Number(t.exit_price).toPrecision(5)}<br><span class="muted">${time(t.exit_ts)}</span></td>
    <td>${usd(t.fees_usd + (t.funding_usd || 0), 3)}</td>
    <td>${pnl(t.pnl_usd)}<br><span class="muted">${pct(t.pnl_pct)}</span></td><td class="muted">${esc(t.reason)}</td></tr>`));
}
async function loadTrades() {
  allTrades = (await api("/trades?limit=1000")).trades;
  fillSelect("f-venue", allTrades.map((t) => t.venue));
  fillSelect("f-asset", allTrades.map((t) => t.symbol));
  fillSelect("f-strat", allTrades.map((t) => t.strategy));
  renderTrades();
}
function renderPositions(positions) {
  table("positions", ["Ativo", "Custo → valor", "Lucro", "Stop / alvo"], positions.map((p) => `
    <tr><td><b>${esc(p.symbol)}</b><br><span class="muted">${esc(p.venue)} · ${esc(p.strategy)}</span></td>
    <td>${usd(p.cost_usd)} → ${usd(p.mark_value)}</td><td>${pnl(p.unrealized_pnl)}</td>
    <td>${Number(p.stop_price).toPrecision(4)} / ${Number(p.tp_price).toPrecision(4)}</td></tr>`));
}

// ─── secondary (collapsed) sections ───
async function move(ev, kind) {
  ev.preventDefault();
  const input = document.getElementById(kind === "deposit" ? "dep" : "wd");
  try { await api(`/funds/${kind}`, "POST", { amount_usd: Number(input.value), note: "dashboard" }); input.value = ""; }
  catch (e) { toast(`✖ ${e.message}`, true); }
  safe(loadSecondary); safe(refreshLive);
  return false;
}
async function togglePause(market, paused) {
  try { await api("/markets/pause", "POST", { market, paused }); } catch (e) { toast(`✖ ${e.message}`, true); }
  safe(loadSecondary);
}
async function researchNow(ev) {
  ev.preventDefault();
  const el = document.getElementById("r-mint");
  try { await api("/research", "POST", { mint: el.value.trim() }); el.value = ""; } catch (e) { toast(`✖ ${e.message}`, true); }
  safe(loadSecondary);
  return false;
}
async function loadSecondary() {
  const [funds, r, m, res, dec] = await Promise.all([api("/funds"), api("/readiness"), api("/markets"), api("/research"), api("/decisions?limit=200")]);
  table("funds", ["Quando", "Tipo", "Valor"], funds.movements.map((x) =>
    `<tr><td>${time(x.ts)}</td><td>${x.kind === "deposit" ? "Depósito" : "Levantamento"}</td><td>${usd(x.amount_usd)}</td></tr>`));
  document.getElementById("readiness").innerHTML = `<p>${r.ready ? '<b class="green">Todos os critérios cumpridos</b>' : '<b class="red">Ainda não</b>'}</p>` +
    r.checks.map((c) => `<div class="note">${c.ok ? "✅" : "⬜"} ${esc(c.name)} (${esc(c.value)})</div>`).join("") + `<p class="note">${esc(r.note)}</p>`;
  const paused = new Set(m.paused);
  document.getElementById("markets").innerHTML = m.markets.map((x) => `
    <div class="fi" style="justify-content:space-between"><span>${esc(x.symbol)} <span class="muted">${esc(x.venue)}</span>${paused.has(x.id) ? ' <span class="tag">pausado</span>' : ""}</span>
    <button onclick="togglePause('${esc(x.id)}', ${!paused.has(x.id)})">${paused.has(x.id) ? "Retomar" : "Pausar"}</button></div>`).join("") || '<p class="note">Sem mercados ainda.</p>';
  document.getElementById("research").innerHTML = res.briefs.map((x) => {
    const b = x.brief, q = b.quant || {};
    return `<div class="wcard"><h3><span>${esc(b.symbol)}</span><span>risco ${esc(b.risk_level)}</span></h3>
      ${(q.flags || []).map((f) => `<div class="note red">⚠ ${esc(f)}</div>`).join("")}
      ${b.llm?.summary ? `<div class="note">${esc(b.llm.summary)}</div>` : ""}<div class="note">${time(x.ts)}</div></div>`;
  }).join("") || '<p class="note">Sem fichas ainda.</p>';
  table("decisions", ["Quando", "Tipo", "Ativo", "Motivo"], dec.decisions.map((d) =>
    `<tr><td>${time(d.ts)}</td><td class="k-${esc(d.kind)}">${esc(d.kind)}</td><td>${esc(d.symbol || "")}</td><td>${esc(d.reason)}</td></tr>`));
}

// ─── loops: live every 2.5s, the rest less often ───
async function safe(fn) { try { await fn(); } catch (e) { console.warn(e); } }
safe(refreshLive); safe(refreshLiveTrading); safe(loadWallets); safe(loadTrades); safe(loadSecondary);
setInterval(() => safe(refreshLiveTrading), 10000);
setTimeout(() => safe(loadChart), 500);
setInterval(() => safe(refreshLive), 2500);
setInterval(() => lastStatus && renderHead(lastStatus), 1000);
setInterval(() => { safe(loadTrades); safe(loadChart); }, 20000);
setInterval(() => safe(loadSecondary), 30000);
setInterval(() => safe(loadWallets), 60000);

// ─── model arena ───
let lastArena = null;
async function loadArena() {
  const a = await api("/arena");
  lastArena = a;
  const btn = document.getElementById("btn-arena");
  if (!btn.classList.contains("busy")) btn.textContent = a.running ? "Parar arena" : "Iniciar arena";
  const sel = document.getElementById("live-model");
  if (document.activeElement !== sel) {
    sel.innerHTML = a.models.map((m) => `<option ${m === a.live_model ? "selected" : ""}>${esc(m)}</option>`).join("");
  }
  table("arena-table", ["#", "Modelo", "Património", "Retorno", "Trades", "Acerto", "Aberto", "Última decisão"],
    a.leaderboard.map((r, i) => `<tr><td>${i + 1}</td><td><b>${esc(r.model)}</b>${r.running ? "" : ' <span class="muted">(parado)</span>'}${r.kill_switch ? ' <span class="red">kill</span>' : ""}</td>
      <td>${usd(r.equity)}</td><td><span class="${r.twr >= 0 ? "green" : "red"}">${pct(r.twr)}</span><br><span class="muted">BTC ${pct(r.hold_btc)}</span></td>
      <td>${r.trades}</td><td>${r.win_rate === null ? "–" : pct(r.win_rate)}</td><td>${esc(r.open.join(", ")) || "–"}</td>
      <td class="muted">${r.last_decision ? `${esc(r.last_decision.market)}: <b>${esc(r.last_decision.action)}</b> — ${esc(r.last_decision.reason || "")}` : "ainda sem decisões"}</td></tr>`));
}
function toggleArena(btn) {
  const on = !lastArena?.running;
  run(btn, on ? "A iniciar arena…" : "A parar arena…", async () => { await api("/arena", "POST", { on }); await loadArena(); },
      on ? "🏁 Arena iniciada — cada modelo decide na sua carteira simulada" : "⏸ Arena parada");
}
function setLiveModel(sel) {
  api("/live/model", "POST", { model: sel.value })
    .then(() => toast(`✓ A conta real passa a ser decidida por ${sel.value} (no modo LLM)`))
    .catch((e) => toast(`✖ ${e.message}`, true));
}

safe(loadArena);
setInterval(() => safe(loadArena), 10000);
