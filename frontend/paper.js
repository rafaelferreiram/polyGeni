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

// Token names/symbols come from public APIs: always escape.
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const usd = (v, d = 2) => (v === null || v === undefined ? "–" : `$${Number(v).toFixed(d)}`);
const pnl = (v) => {
  if (v === null || v === undefined) return "–";
  const n = Number(v);
  return `<span class="${n >= 0 ? "green" : "red"}">${n >= 0 ? "+" : "-"}$${Math.abs(n).toFixed(2)}</span>`;
};
const pct = (v) => (v === null || v === undefined ? "–" : `${(v * 100).toFixed(2)}%`);
const time = (iso) => (iso ? new Date(iso).toLocaleString() : "–");
const short = (m) => (m ? `${m.slice(0, 4)}…${m.slice(-4)}` : "");

async function act(path) {
  try { await api(path, "POST"); } catch (e) { alert(e.message); }
  refresh();
}

async function kill() {
  const st = await api("/status");
  if (st.kill_switch.active) {
    if (confirm("Rearmar o kill switch e permitir novas entradas?")) await api("/kill-switch/reset", "POST", { reason: "reset via dashboard" });
  } else if (confirm("Ativar kill switch? Bloqueia novas entradas (saídas continuam).")) {
    await api("/kill-switch", "POST", { reason: "dashboard" });
  }
  refresh();
}

async function move(ev, kind) {
  ev.preventDefault();
  const input = document.getElementById(kind === "deposit" ? "dep" : "wd");
  try { await api(`/funds/${kind}`, "POST", { amount_usd: Number(input.value), note: "dashboard" }); input.value = ""; }
  catch (e) { alert(e.message); }
  refresh();
  return false;
}

async function addWallet(ev) {
  ev.preventDefault();
  try {
    await api("/wallets", "POST", { address: document.getElementById("w-addr").value.trim(), label: document.getElementById("w-label").value });
    document.getElementById("w-addr").value = "";
  } catch (e) { alert(e.message); }
  loadWallets();
  return false;
}

async function removeWallet(addr) {
  await api(`/wallets/${encodeURIComponent(addr)}`, "DELETE");
  loadWallets();
}

function table(el, head, rows) {
  document.getElementById(el).innerHTML =
    `<thead><tr>${head.map((h) => `<th>${h}</th>`).join("")}</tr></thead>` +
    `<tbody>${rows.length ? rows.join("") : `<tr><td colspan="${head.length}" class="muted">Sem dados ainda</td></tr>`}</tbody>`;
}

async function loadStatus() {
  const s = await api("/status");
  story(s);
  document.getElementById("equity").textContent = usd(s.equity);
  document.getElementById("cash").textContent = usd(s.cash);
  document.getElementById("exposure").textContent = usd(s.exposure);
  document.getElementById("total-pnl").innerHTML = pnl(s.total_pnl);
  document.getElementById("r-u").innerHTML = `${pnl(s.realized_pnl)} / ${pnl(s.unrealized_pnl)}`;
  document.getElementById("wr").textContent = `${s.closed_trades} · ${s.win_rate === null ? "–" : pct(s.win_rate)}`;

  const run = document.getElementById("run-badge");
  run.textContent = s.running ? (s.heartbeat_ok ? "A CORRER" : "SEM HEARTBEAT") : "PARADO";
  run.className = `badge ${s.running && s.heartbeat_ok ? "badge-on" : "badge-off"}`;
  const kb = document.getElementById("kill-badge");
  kb.textContent = `kill switch: ${s.kill_switch.active ? "ATIVO" : "off"}`;
  kb.className = `badge ${s.kill_switch.active ? "badge-auto" : "badge-off"}`;
  if (s.kill_switch.active) kb.style.color = "var(--red)";

  const labels = { day: "Hoje (24h)", week: "7 dias", month: "30 dias", all: "Desde o início" };
  document.getElementById("perf").innerHTML = Object.entries(labels).map(([k, l]) => {
    const p = (s.performance || {})[k];
    if (!p) return `<div><div class="lbl">${l}</div><div class="val">–</div></div>`;
    const beat = p.hold_sol === null ? "" : p.twr >= p.hold_sol ? "green" : "red";
    return `<div><div class="lbl">${l}</div><div class="val">${pnl(p.gain_usd)}</div>
      <div class="lbl">${pct(p.twr)} TWR</div>
      <div class="lbl ${beat}">hold SOL ${pct(p.hold_sol)}</div><div class="lbl">hold BTC ${pct(p.hold_btc)}</div></div>`;
  }).join("");

  const lc = s.last_cycle || {};
  document.getElementById("loop").innerHTML = `
    Modo: <b>PAPER</b> (LIVE_TRADING desligado) · ciclo #${s.cycle} a cada ${s.cycle_interval_sec}s<br>
    Último heartbeat: ${time(s.heartbeat?.ts)} ${s.heartbeat_ok ? '<span class="green">●</span>' : '<span class="red">●</span>'}<br>
    Último ciclo: ${lc.candidates ?? "–"} candidatos · ${lc.safe ?? "–"} seguros · ${lc.signals ?? "–"} sinais · ${lc.entries ?? "–"} entradas · ${lc.vetoes ?? "–"} vetos ${lc.error ? `<span class="red">erro: ${esc(lc.error)}</span>` : ""}<br>
    Kill switch: ${s.kill_switch.active ? `<span class="red">ATIVO — ${esc(s.kill_switch.reason)}</span>` : "off"}<br>
    Capital aportado: ${usd(s.contributed)} · LLM debate: ${s.debate_llm.enabled ? esc(s.debate_llm.model) + (s.debate_llm.mcp_servers ? " + MCP" : "") + " (só veto)" : "desligado (sem ANTHROPIC_API_KEY)"}<br>
    Limites: ${pct(s.limits.max_position_pct)}/posição · exposição máx ${pct(s.limits.max_exposure_pct)} · ${s.limits.max_positions} posições · perda diária máx ${pct(s.limits.max_daily_loss_pct)} · par ≥ ${s.limits.min_pair_age_h}h`;
}

const AGENT_UI = {
  scout: ["🔭", "Scout", "Procura tokens com volume e liquidez"],
  safety: ["🛡️", "Safety", "Anti-rug: rejeita o que não consegue verificar"],
  momentum: ["🚀", "Momentum", "Compra quando o preço rompe máximos com volume"],
  mean_reversion: ["🎯", "Mean reversion", "Compra quedas exageradas (sobrevenda)"],
  research: ["📚", "Research", "Analisa métricas, notícias e riscos"],
  debate: ["⚖️", "Debate (LLM)", "Bull vs bear; só pode vetar"],
  risk: ["🧮", "Risk", "Limites, tamanho e kill switch — palavra final"],
  executor: ["⚙️", "Executor", "Executa a ordem no PaperBroker"],
};

let lastAgents = [];
async function loadAgents() {
  const { agents } = await api("/agents");
  lastAgents = agents;
  document.getElementById("pipeline").innerHTML = agents.map((a, i) => {
    const [icon, label, plain] = AGENT_UI[a.name] || ["🤖", a.name, a.role];
    const reason = a.last_reason || "";
    const cls = !a.last_run ? "idle" : a.last_ok === false ? "blocked" : "ok";
    return `${i ? '<span class="arrow">→</span>' : ""}<div class="agent ${cls}" title="${esc(a.role)}">
      <h3><span><span class="icon">${icon}</span>${esc(label)}</span></h3>
      <div class="role">${esc(plain)}</div>
      ${a.capital_usd !== undefined ? `<div class="last">Capital: ${usd(a.capital_usd)}</div>` : ""}
      <div class="last">✔ ${a.approved ?? 0} · ✖ ${a.vetoed ?? 0}${a.trades ? ` · ${a.trades} trades · ${pnl(a.pnl_usd)} · win ${pct(a.win_rate)}` : ""}</div>
      <div class="last muted">${a.last_run ? esc(reason) : "ainda não correu"}</div>
      <div class="last muted">${a.last_run ? time(a.last_run) : ""}</div>
    </div>`;
  }).join("");
}

function story(s) {
  const lc = s.last_cycle || {};
  const state = !s.running ? "⏸️ O bot está <b>parado</b>. Clica em <b>Iniciar 24/7</b> para pôr os agents a trabalhar."
    : s.heartbeat_ok ? `🟢 O bot está <b>a trabalhar 24/7</b> (ciclo #${s.cycle}, a cada ${Math.round(s.cycle_interval_sec / 60)} min).`
    : "🟠 O bot devia estar a correr mas <b>não dá sinal de vida</b> — vê o decision log por erros.";
  const kill = s.kill_switch.active ? ` 🛑 <b>Kill switch ativo</b> (${esc(s.kill_switch.reason)}): não abre novas posições.` : "";
  const last = lc.cycle ? ` No último ciclo analisou <b>${lc.candidates}</b> tokens, <b>${lc.safe}</b> passaram a segurança, <b>${lc.signals}</b> deram sinal, <b>${lc.entries}</b> compras e <b>${lc.vetoes}</b> bloqueios.` : "";
  const money = ` Património <b>${usd(s.equity)}</b> (${pnl(s.total_pnl)} desde o início; hoje ${pnl(s.performance?.day?.gain_usd ?? 0)}).`;
  const quiet = lc.cycle && !lc.entries && !s.kill_switch.active ? ' <span class="muted">Sem compras é normal: os agents só entram quando todas as regras concordam.</span>' : "";
  document.getElementById("resumo").innerHTML = `${state}${kill}${last}${money}${quiet}<br><span class="muted">Tudo isto é <b>paper trading</b>: dinheiro simulado com cotações reais da Jupiter.</span>`;
}

async function connectWallet(kind) {
  const provider = kind === "phantom" ? window.phantom?.solana : window.solflare;
  if (!provider) {
    alert(`Extensão ${kind === "phantom" ? "Phantom" : "Solflare"} não encontrada neste browser. Instala-a ou cola o endereço público manualmente.`);
    return;
  }
  try {
    // connect() only shares the public key; nothing is signed and no funds are accessible.
    const res = await provider.connect();
    const address = (res?.publicKey || provider.publicKey).toString();
    await api("/wallets", "POST", { address, label: kind === "phantom" ? "Phantom" : "Solflare" });
    loadWallets();
  } catch (e) { alert(`Ligação cancelada: ${e.message || e}`); }
}

async function loadPositions() {
  const { positions } = await api("/positions");
  table("positions", ["Venue", "Ativo", "Estratégia", "Custo", "Valor", "P&L", "Stop / TP", "Aberta"], positions.map((p) => `
    <tr><td>${esc(p.venue)}</td><td>${esc(p.symbol)} <span class="muted">${p.venue === "jupiter" ? short(p.mint) : "1x"}</span></td><td>${esc(p.strategy)}</td>
    <td>${usd(p.cost_usd)}</td><td>${usd(p.mark_value)}</td><td>${pnl(p.unrealized_pnl)}</td>
    <td>${Number(p.stop_price).toPrecision(4)} / ${Number(p.tp_price).toPrecision(4)}</td><td>${time(p.opened_at)}</td></tr>`));
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
  const total = rows.reduce((x, t) => x + t.pnl_usd, 0);
  document.getElementById("trades-total").innerHTML = `${rows.length} trades · ${pnl(total)}`;
  table("trades", ["Venue", "Ativo", "Estratégia", "Entrada → saída", "Fees", "Slip. (bps)", "P&L", "Motivo", "Fecho"], rows.map((t) => `
    <tr><td>${esc(t.venue)}</td><td>${esc(t.symbol)}</td><td>${esc(t.strategy)}</td>
    <td>${Number(t.entry_price).toPrecision(5)} → ${Number(t.exit_price).toPrecision(5)}</td>
    <td>${usd(t.fees_usd + (t.funding_usd || 0), 3)}${t.funding_usd ? `<br><span class="muted">funding ${usd(t.funding_usd, 3)}</span>` : ""}</td>
    <td>${(t.slippage_bps || []).join(" / ")}</td>
    <td>${pnl(t.pnl_usd)} <span class="muted">${pct(t.pnl_pct)}</span></td><td>${esc(t.reason)}</td><td>${time(t.exit_ts)}</td></tr>`));
}
async function loadTrades() {
  allTrades = (await api("/trades?limit=1000")).trades;
  fillSelect("f-venue", allTrades.map((t) => t.venue));
  fillSelect("f-asset", allTrades.map((t) => t.symbol));
  fillSelect("f-strat", allTrades.map((t) => t.strategy));
  renderTrades();
}

async function loadDecisions() {
  const { decisions } = await api("/decisions?limit=150");
  document.getElementById("dec-count").textContent = decisions.length;
  table("decisions", ["Quando", "Tipo", "Token", "Estratégia", "Motivo", "Votos"], decisions.map((d) => {
    const votes = Object.entries(d.votes || {}).map(([k, v]) => `${v.approve ? "✔" : "✖"} ${esc(k)}`).join(" · ");
    return `<tr><td>${time(d.ts)}<br><span class="muted">#${d.cycle ?? ""}</span></td><td class="kind-${esc(d.kind)}">${esc(d.kind)}</td>
      <td>${esc(d.symbol || "")}</td><td>${esc(d.strategy || "")}</td><td>${esc(d.reason)}</td>
      <td>${votes}${Object.keys(d.votes || {}).length ? `<details><summary>detalhe</summary><pre>${esc(JSON.stringify({ votes: d.votes, signals: d.signals }, null, 1))}</pre></details>` : ""}</td></tr>`;
  }));
}

async function loadFunds() {
  const { movements } = await api("/funds");
  table("funds", ["Quando", "Tipo", "Valor"], movements.map((m) =>
    `<tr><td>${time(m.ts)}</td><td>${m.kind === "deposit" ? "Depósito" : "Levantamento"}</td><td>${usd(m.amount_usd)}</td></tr>`));
}

async function loadWallets() {
  const { wallets } = await api("/wallets");
  document.getElementById("wallets").innerHTML = wallets.length ? wallets.map((w) => `
    <div class="agent" style="margin-top:8px">
      <h3><span>${esc(w.label || "Carteira")} <span class="muted">${short(w.address)}</span></span>
      <button onclick="removeWallet('${esc(w.address)}')">remover</button></h3>
      ${w.error ? `<div class="red last">${esc(w.error)}</div>` :
        `<div class="last">${Number(w.sol).toFixed(4)} SOL · ${w.tokens.length} tokens</div>
         <div class="last muted">${w.tokens.slice(0, 8).map((t) => `${short(t.mint)}: ${Number(t.amount).toPrecision(5)}`).join(" · ")}</div>`}
    </div>`).join("") : '<p class="note">Nenhuma carteira adicionada.</p>';
}

const RISK_CLS = { low: "green", medium: "", high: "red", critical: "red" };

async function loadResearch() {
  const { briefs } = await api("/research");
  document.getElementById("research").innerHTML = briefs.length ? briefs.map((r) => {
    const b = r.brief, q = b.quant || {}, m = q.metrics || {}, l = b.llm || {};
    return `<div class="agent">
      <h3><span>${esc(b.symbol)} <span class="muted">${short(b.mint)}</span></span>
      <span class="${RISK_CLS[b.risk_level] || ""}">risco ${esc(b.risk_level)}</span></h3>
      <div class="last">organic ${m.organic_score === null || m.organic_score === undefined ? "–" : Number(m.organic_score).toFixed(0)} ·
        holders ${m.holders ?? "–"} · buy/sell 24h ${m.buy_sell_ratio_24h ?? "–"} · top holders ${m.top_holders_pct === null || m.top_holders_pct === undefined ? "–" : Number(m.top_holders_pct).toFixed(0) + "%"}</div>
      ${(q.flags || []).map((f) => `<div class="last red">⚠ ${esc(f)}</div>`).join("")}
      ${l.summary ? `<div class="last" style="margin-top:6px">${esc(l.summary)}</div>` : ""}
      ${l.error ? `<div class="last muted">LLM: ${esc(l.error)}</div>` : ""}
      ${l.summary ? `<details><summary>bull / bear / riscos / catalisadores</summary><pre>${esc(JSON.stringify({ bull: l.bull, bear: l.bear, risks: l.risks, catalysts: l.catalysts, sentiment: l.sentiment, sources: l.sources }, null, 1))}</pre></details>` : ""}
      <div class="last muted">${time(r.ts)}${b.model ? " · " + esc(b.model) : " · só métricas (LLM desligado)"}</div>
    </div>`;
  }).join("") : '<p class="note">Ainda sem fichas — aparecem quando uma estratégia gera sinal, ou pesquisa um token acima.</p>';
}

async function researchNow(ev) {
  ev.preventDefault();
  const el = document.getElementById("r-mint");
  try { await api("/research", "POST", { mint: el.value.trim() }); el.value = ""; } catch (e) { alert(e.message); }
  loadResearch();
  return false;
}

let chart, chartPeriod = "all";
function setChartPeriod(p, btn) {
  chartPeriod = p;
  document.querySelectorAll(".period-btn").forEach((b) => b.classList.toggle("active", b === btn));
  loadChart();
}
async function loadChart() {
  let { history } = await api("/equity?limit=20000");
  const span = { day: 864e5, week: 7 * 864e5, month: 30 * 864e5 }[chartPeriod];
  if (span) history = history.filter((h) => Date.now() - new Date(h.ts) <= span);
  const labels = history.map((h) => new Date(h.ts).toLocaleString());
  const data = history.map((h) => h.equity);
  const contributed = history.map((h) => h.contributed);
  if (!chart) {
    chart = new Chart(document.getElementById("equity-chart"), {
      type: "line",
      data: { labels, datasets: [
        { label: "Equity", data, borderColor: "#7f5af0", pointRadius: 0, tension: 0.2 },
        { label: "Capital aportado", data: contributed, borderColor: "#72757e", borderDash: [4, 4], pointRadius: 0 },
      ] },
      options: { animation: false, plugins: { legend: { labels: { color: "#72757e" } } },
        scales: { x: { ticks: { color: "#72757e", maxTicksLimit: 6 } }, y: { ticks: { color: "#72757e" } } } },
    });
  } else {
    chart.data.labels = labels;
    chart.data.datasets[0].data = data;
    chart.data.datasets[1].data = contributed;
    chart.update();
  }
}

async function loadMarkets() {
  const m = await api("/markets");
  const paused = new Set(m.paused);
  const venues = m.venues.map((v) => `<button onclick="togglePause('${esc(v)}', ${!paused.has(v)})">${paused.has(v) ? "▶ Retomar" : "⏸ Pausar"} ${esc(v)} (venue)</button>`).join(" ");
  document.getElementById("markets").innerHTML = `<div class="wallet-btns">${venues}</div>` + m.markets.map((x) => `
    <div class="agent" style="display:flex;justify-content:space-between;align-items:center;margin-top:6px">
      <span>${esc(x.symbol)} <span class="muted">${esc(x.venue)} · ${Number(x.price).toPrecision(6)}</span>${paused.has(x.id) ? ' <span class="tag">pausado</span>' : ""}</span>
      <button onclick="togglePause('${esc(x.id)}', ${!paused.has(x.id)})">${paused.has(x.id) ? "Retomar" : "Pausar"}</button>
    </div>`).join("");
}
async function togglePause(market, paused) {
  try { await api("/markets/pause", "POST", { market, paused }); } catch (e) { alert(e.message); }
  loadMarkets();
}
async function loadReadiness() {
  const r = await api("/readiness");
  document.getElementById("readiness").innerHTML = `
    <div class="last" style="margin-bottom:6px">${r.ready ? '<b class="green">Todos os critérios cumpridos</b>' : '<b class="red">Ainda não está pronto para dinheiro real</b>'}</div>` +
    r.checks.map((c) => `<div class="last">${c.ok ? "✅" : "⬜"} ${esc(c.name)} <span class="muted">(${esc(c.value)})</span></div>`).join("") +
    `<p class="note" style="margin-top:8px">${esc(r.note)}</p>`;
}

async function refresh() {
  await Promise.allSettled([loadStatus(), loadAgents(), loadPositions(), loadTrades(), loadDecisions(), loadFunds(), loadChart(), loadResearch(), loadMarkets(), loadReadiness()]);
}

refresh();
loadWallets();
setInterval(refresh, 15000);
