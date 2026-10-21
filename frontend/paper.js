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

// Token names, reasons and LLM text come from outside: always escape.
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const usd = (v, d = 2) => (v === null || v === undefined ? "–" : `$${Number(v).toFixed(d)}`);
const pnl = (v) => {
  if (v === null || v === undefined) return "–";
  const n = Number(v);
  return `<span class="${n >= 0 ? "green" : "red"}">${n >= 0 ? "+" : "−"}$${Math.abs(n).toFixed(2)}</span>`;
};
const pct = (v) => (v === null || v === undefined ? "–" : `${(v * 100).toFixed(2)}%`);
const time = (iso) => (iso ? new Date(iso).toLocaleString("pt-BR") : "–");
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
async function safe(fn) { try { await fn(); } catch (e) { console.warn(e); } }

// ─── feedback: every click shows "working…" then a visible result ───
let toastTimer;
function toast(msg, err = false) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.className = `toast${err ? " err" : ""}`;
  t.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (t.hidden = true), err ? 8000 : 3500);
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
    if (btn.textContent === busyLabel) btn.textContent = label;   // buttons whose label isn't driven by refresh()
    safe(refresh);
  }
}

// ─── account: simulation and real are the same view ───
let KIND = "sim";
try { KIND = localStorage.getItem("acct") || "sim"; } catch (e) { /* storage blocked */ }
let acc = null;

function setKind(k) {
  KIND = k;
  setTimeout(() => safe(loadDaily), 0);
  try { localStorage.setItem("acct", k); } catch (e) { /* ignore */ }
  replay = null;
  lastCycleSeen = null;
  safe(refresh);
}

function go(btn) {
  const on = acc?.running;
  if (!on && KIND === "real") {
    const L = acc?.limits || {};
    const net = acc?.network === "mainnet" ? "MAINNET — DINHEIRO REAL" : "testnet (dinheiro de teste)";
    if (!confirm(`Ligar os bots na conta REAL (${net})?\n\nLimites: capital ${usd(L.max_capital_usd, 0)}, posição ${usd(L.max_position_usd, 0)}, 1x, stop e alvo na exchange, perda diária ${pct(L.max_daily_loss_pct)}.`)) return;
  }
  run(btn, on ? "Parando…" : "Ligando os bots…",
      () => api(`/account/${on ? "stop" : "start"}`, "POST", { kind: KIND }),
      on ? "■ Equipe parada — não abre novas posições (stops continuam ativos)"
         : (r) => `▶ Equipe trabalhando na carteira ${KIND === "sim" ? "simulada" : "REAL"}${r.llm_bots_need_key ? " — só o bot de Regras trabalha até você colocar a ANTHROPIC_API_KEY" : ""}`);
}

function scanNow(btn) {
  run(btn, "Acordando a equipe…", () => api("/account/scan", "POST", { kind: KIND }),
      (r) => (r.started ? "🔍 Equipe acordada — analisando os mercados agora" : "Já tem uma análise rodando"));
}

function giveCoffee(btn) {
  if (!acc?.running) return toast("Clique em ▶ Start primeiro", true);
  run(btn, "Preparando o café…", () => api("/account/coffee", "POST", { kind: KIND, hours: 4 }),
      (r) => `☕ Equipe com cafeína até ${hhmm(r.coffee_until)} nas contas simulada e real — sem pausas, analisando todos os ciclos`);
}

function setMode(mode, btn) {
  run(btn, "…", () => api("/account/mode", "POST", { mode }),
      mode === "llm" ? "🧠 A equipe de bots decide; o Coordenador escolhe o melhor por mercado" : "✓ Só o bot de regras decide");
}

// ─── cute bot face (inline SVG): colour via --c, mood via the card's class (working / sleeping / blocked…) ───
function botFace(color, ico) {
  return `<div class="bot2" style="--c:${color}" aria-hidden="true"><svg viewBox="0 0 64 64">
    <line x1="32" y1="10" x2="32" y2="4" class="ant-l"/><circle cx="32" cy="4" r="3.4" class="bulb"/>
    <rect x="4" y="26" width="7" height="14" rx="3.5" class="ear"/><rect x="53" y="26" width="7" height="14" rx="3.5" class="ear"/>
    <rect x="9" y="10" width="46" height="44" rx="17" class="head"/>
    <rect x="15" y="18" width="34" height="25" rx="12" class="visor"/>
    <g class="eyes"><ellipse cx="25" cy="29" rx="4.2" ry="4.8" class="eye"/><ellipse cx="39" cy="29" rx="4.2" ry="4.8" class="eye"/>
      <circle cx="26.5" cy="27.2" r="1.4" class="glint"/><circle cx="40.5" cy="27.2" r="1.4" class="glint"/></g>
    <ellipse cx="19" cy="37" rx="2.8" ry="1.6" class="blush"/><ellipse cx="45" cy="37" rx="2.8" ry="1.6" class="blush"/>
    <path d="M28 37 Q32 40.5 36 37" class="smile"/></svg><span class="badge-ico">${ico}</span></div>`;
}
// re-render only when something changed, so a hovered/focused card keeps its tooltip open
function setHTML(id, html) {
  const el = document.getElementById(id);
  if (el.dataset.html !== html) { el.innerHTML = html; el.dataset.html = html; }
}
const tipRows = (rows) => `<dl>${rows.filter(([, v]) => v !== null && v !== undefined && v !== "").map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl>`;
const hhmm = (iso) => (iso ? new Date(iso).toLocaleTimeString("pt-BR").slice(0, 5) : "–");

// ─── animated bot room ───
const STATIONS = [
  { key: "scout", ico: "🔭", name: "Scout", role: "observa os mercados", color: "#7dd3fc",
    about: "Busca preço, candles de 1h e funding de cada mercado e entrega os candidatos aos outros bots." },
  { key: "decider", ico: "🎯", name: "Estratégia", role: "decide se compra", color: "#34d399",
    about: "Procura uma queda forte dentro de uma tendência de alta. Sem queda, não compra." },
  { key: "risk", ico: "🛡️", name: "Risk", role: "limites e tamanho", color: "#fbbf24",
    about: "Define o tamanho da posição e pode vetar qualquer compra: limites de capital, perda diária e kill switch." },
  { key: "executor", ico: "⚙️", name: "Executor", role: "envia a ordem", color: "#fb923c",
    about: "O único que fala com a corretora. Envia a ordem aprovada com stop e alvo na Hyperliquid." },
  { key: "guard", ico: "👁️", name: "Guarda", role: "vigia stop e alvo", color: "#a3e635",
    about: "Acompanha as posições abertas, o stop-loss e o alvo, e registra as saídas." },
];
let replay = null, lastCycleSeen = null;

function stationData(st, a) {
  const deciderKey = a.decision_mode === "llm" ? "llm" : "mean_reversion";
  if (st.key === "decider") {
    return { ...(a.agents[deciderKey] || {}), name: a.decision_mode === "llm" ? "Coordenador" : "Estratégia",
             ico: a.decision_mode === "llm" ? "🧭" : "🎯",
             role: a.decision_mode === "llm" ? "escolhe o melhor bot" : "compra quedas exageradas" };
  }
  if (st.key === "guard") {
    const p = a.positions[0];
    const lastExit = a.decisions.find((d) => d.kind === "exit");
    return { last_reason: p ? `${p.symbol}: stop ${Number(p.stop_price).toPrecision(5)} · alvo ${Number(p.tp_price).toPrecision(5)} · agora ${usd(p.mark_value)}`
                            : lastExit ? `Última saída: ${lastExit.symbol} — ${lastExit.reason}` : "Sem posições para vigiar",
             last_run: a.heartbeat?.ts, last_ok: true, guard: true };
  }
  return a.agents[st.key] || {};
}

function renderRoom(a) {
  const cycleStart = a.heartbeat ? new Date(a.heartbeat.ts).getTime() : 0;
  const liveAgent = a.live?.running && Date.now() - new Date(a.live.ts) < 30000 ? a.live.agent : null;
  const activeKey = replay ? replay.order[replay.i] : liveAgent === "llm" || liveAgent === "mean_reversion" ? "decider" : liveAgent;
  const html = STATIONS.map((st, i) => {
    const d = stationData(st, a);
    const acted = d.last_run && new Date(d.last_run).getTime() >= cycleStart - 1000;
    const working = a.running && activeKey === st.key;
    let cls = !a.running ? "off" : working ? "working" : !acted ? "idle" : d.last_ok === false ? "blocked" : "ok";
    if (st.key === "guard" && a.running && !working) cls = a.positions.length ? "ok" : "idle";
    const state = !a.running ? "desligado" : working ? "trabalhando" : cls === "idle" ? "aguardando" : cls === "blocked" ? "bloqueou" : "feito";
    const bubble = !a.running ? "Clique em ▶ Start para me colocar para trabalhar"
      : working && !replay ? (a.live.detail || "pensando…") : (d.last_reason || (st.key === "risk" || st.key === "executor" ? "Só entro em ação quando há sinal de compra" : "…"));
    const tip = `<b>${esc(d.name || st.name)}</b> · ${esc(d.role || st.role)}<div class="note" style="margin-top:4px">${esc(d.about || st.about)}</div>
      ${tipRows([["Status", esc(state)], ["Última vez", d.last_run ? `${hhmm(d.last_run)} (há ${ago(d.last_run)})` : "ainda não rodou"],
                 ["Ciclo", a.cycle ? `#${a.cycle}` : null], ["Próxima", nextCycleAt && a.running ? hhmm(new Date(nextCycleAt).toISOString()) : null]])}
      <div class="wip"><b>Em andamento</b><br>${esc(working ? (a.live.detail || "pensando…") : bubble)}</div>`;
    const wireOn = a.running && (working || (replay && replay.order.indexOf(st.key) >= 0 && replay.order.indexOf(st.key) < replay.i));
    return `${i ? `<div class="wire ${wireOn ? "on" : ""}"></div>` : ""}
      <div class="st ${cls}" tabindex="0" style="--c:${st.color}" aria-label="${esc(d.name || st.name)}: ${esc(state)}">
        ${cls === "idle" ? '<span class="zz">z z</span>' : ""}
        <div class="head">${botFace(st.color, d.ico || st.ico)}
          <div><div class="nm">${esc(d.name || st.name)}</div><div class="rl">${esc(d.role || st.role)}</div></div></div>
        <div class="state"><span class="dot"></span>${state}</div>
        <div class="bubble">${esc(bubble)}</div>
        <div class="tip" role="tooltip">${tip}</div>
      </div>`;
  }).join("");
  setHTML("line", html);
}

function maybeReplay(a) {
  // after each finished cycle, replay what every bot did, one after the other
  const lc = a.last_cycle;
  if (!lc || !a.running || a.scanning) return;
  if (lastCycleSeen === null) { lastCycleSeen = lc.cycle; if (Date.now() - new Date(lc.finished) > 120000) return; }
  else if (lc.cycle === lastCycleSeen) return;
  lastCycleSeen = lc.cycle;
  const cycleStart = a.heartbeat ? new Date(a.heartbeat.ts).getTime() - 1000 : 0;
  const ranAt = (k) => {
    const d = k === "decider" ? (a.agents.llm?.last_run && a.decision_mode === "llm" ? a.agents.llm : a.agents.mean_reversion) : a.agents[k];
    return d?.last_run && new Date(d.last_run).getTime() >= cycleStart ? new Date(d.last_run).getTime() : null;
  };
  const order = ["scout", "decider", "risk", "executor"].filter((k) => ranAt(k) !== null);
  if (a.positions.length) order.push("guard");
  if (!order.length) return;
  replay = { order, i: 0 };
  const step = () => {
    renderRoom(acc);
    if (++replay.i <= order.length) setTimeout(step, 900);
    else { replay = null; renderRoom(acc); }
  };
  step();
}

// ─── main refresh ───
const KINDS = { entry: "🟢 compra", exit: "🔵 venda", veto: "🟡 bloqueio", error: "🔴 erro", kill_switch: "🛑 kill switch",
                reconcile: "🧾 reconciliação", arm: "🔓 ligado", disarm: "🔒 desligado", mode: "🔀 modo",
                sleep: "😴 pausa", wake: "⏰ acordou", coffee: "☕ café" };
let nextCycleAt = null;

async function refresh() {
  const a = await api(`/account?kind=${KIND}`);
  acc = a;
  // header
  document.querySelectorAll("#acct-seg button").forEach((b) => b.classList.toggle("on", b.dataset.k === KIND));
  document.querySelectorAll("#mode-seg button").forEach((b) => b.classList.toggle("on", b.dataset.m === a.decision_mode));
  document.querySelector('#mode-seg [data-m="llm"]').title = a.llm_available ? "O LLM decide compras e vendas" : "Precisa da ANTHROPIC_API_KEY no .env";
  const strip = document.getElementById("strip");
  strip.className = `strip ${KIND}`;
  strip.textContent = KIND === "sim" ? "SIMULAÇÃO — dinheiro falso, preços e order books reais da Hyperliquid"
    : `CONTA REAL (${a.network.toUpperCase()}) — ${a.network === "mainnet" ? "dinheiro real" : "dinheiro de teste da Hyperliquid"}`;
  const btn = document.getElementById("btn-go");
  if (!btn.classList.contains("busy")) {
    btn.textContent = a.running ? "■ Stop" : "▶ Start";
    btn.className = `go ${a.running ? "stop" : "start"}`;
  }
  // setup (real account not configured yet)
  const setup = document.getElementById("setup");
  const problems = a.config?.problems || [];
  setup.hidden = !(KIND === "real" && (problems.length || a.exchange_error));
  if (!setup.hidden) {
    setup.innerHTML = `<b>Para ativar a conta real falta:</b> ${problems.map(esc).join(" · ")}${a.exchange_error ? ` <span class="red">${esc(a.exchange_error)}</span>` : ""}
      <ol><li>Depositar ≥ 5 USDC na Hyperliquid (rede Arbitrum) com a MetaMask</li>
      <li>Testnet: Faucet → More → API → <b>Generate</b> → Authorize (salve a chave só no <code>.env</code>)</li>
      <li>No <code>.env</code>: <code>HL_NETWORK=testnet</code>, <code>HL_ACCOUNT_ADDRESS=0x…</code>, <code>HL_API_WALLET_KEY=…</code> e reinicie</li></ol>`;
  }
  // KPIs
  document.getElementById("k-eq-l").textContent = KIND === "sim" ? "Saldo simulado" : "Saldo real na Hyperliquid";
  document.getElementById("k-eq").textContent = usd(KIND === "real" && a.exchange_balance !== undefined ? a.exchange_balance : a.equity);
  document.getElementById("k-eq-s").textContent = `caixa ${usd(a.cash)} · em posição ${usd(a.exposure)}`;
  const P = a.profit || {};
  document.getElementById("k-pnl").innerHTML = pnl(P.net_all);
  const all = a.performance?.all;
  document.getElementById("k-pnl-s").textContent = `trades ${usd(P.trading_all)} · IA −${usd(P.ai_all)}${all ? ` · segurar BTC ${pct(all.hold_btc)}` : ""}`;
  renderProfit(P);
  const p = a.positions[0];
  const kp = document.getElementById("k-pos");
  kp.textContent = p ? p.symbol : "Sem posição";
  kp.classList.toggle("soft", !p);
  document.getElementById("k-pos-s").innerHTML = p ? `${usd(p.cost_usd)} → ${usd(p.mark_value)} ${pnl(p.mark_value !== null ? p.mark_value - p.cost_usd : null)}` : "esperando uma oportunidade";
  const kt = document.getElementById("k-tr");
  kt.textContent = a.trades.length ? `${a.trades.length} · ${pct(a.win_rate)}` : "Nenhum";
  kt.classList.toggle("soft", !a.trades.length);
  document.getElementById("k-tr-s").innerHTML = a.trades.length ? `total ${pnl(a.trades.reduce((x, t) => x + t.pnl_usd, 0))}` : "sem trades fechados";
  // scan bar + room
  nextCycleAt = a.running && a.heartbeat ? new Date(a.heartbeat.ts).getTime() + a.cycle_interval_sec * 1000 : null;
  const scanning = a.scanning || (a.live?.running && Date.now() - new Date(a.live.ts) < 60000);
  document.getElementById("scanbar").hidden = !scanning;
  if (scanning) document.getElementById("scan-text").textContent = `Analisando o mercado (ciclo #${a.cycle})…`;
  document.getElementById("room-tag").textContent = KIND === "sim" ? "simulação" : `real · ${a.network}`;
  document.getElementById("room-head").innerHTML = scanning ? '<span class="live-chip">analisando o mercado</span>' : "";
  if (!replay) renderRoom(a);
  renderTeam();
  maybeReplay(a);
  renderFoot();
  // feed + trades
  const lc = a.last_cycle;
  document.getElementById("feed").innerHTML = (lc ? `<div class="fi"><span class="w">${ago(lc.finished)}</span><span>⚙️ Ciclo #${lc.cycle}: ${lc.candidates} mercados, ${lc.signals} sinais, ${lc.entries} compras</span></div>` : "") +
    (a.decisions.map((d) => `<div class="fi"><span class="w">${ago(d.ts)}</span><span><span class="k-${esc(d.kind)}">${KINDS[d.kind] || esc(d.kind)}</span> <b>${esc(d.symbol || "")}</b> — ${esc(d.reason)}</span></div>`).join("")
     || '<p class="note">Os bots ainda não fizeram nada nesta conta.</p>');
  document.getElementById("trades-total").innerHTML = a.trades.length ? pnl(a.trades.reduce((x, t) => x + t.pnl_usd, 0)) : "";
  table("trades", ["Ativo", "Entrada → saída", "Lucro", "Motivo"], a.trades.map((t) => `
    <tr><td><b>${esc(t.symbol)}</b><br><span class="muted">${time(t.exit_ts)}</span></td>
    <td>${Number(t.entry_price).toPrecision(6)} → ${Number(t.exit_price).toPrecision(6)}</td>
    <td>${pnl(t.pnl_usd)}<br><span class="muted">${pct(t.pnl_pct)}</span></td><td class="muted">${esc(t.reason)}</td></tr>`));
  const L = a.limits;
  document.getElementById("limits").textContent = `Limites (iguais na simulação e no real): capital ${usd(L.max_capital_usd, 0)}, posição ${usd(L.max_position_usd, 0)}, ${L.max_positions} de cada vez, 1x só compra, perda diária ${pct(L.max_daily_loss_pct)}, mercados ${L.markets.join(", ")}.`;
}

function renderProfit(P) {
  if (!P) return;
  document.getElementById("f-trade").innerHTML = pnl(P.trading_30d);
  document.getElementById("f-ai").innerHTML = P.ai_30d ? `<span class="red">−${usd(P.ai_30d)}</span>` : "$0.00";
  document.getElementById("f-ai-s").textContent = `${P.ai_calls_30d} análises pagas em 30 dias${P.prefilter ? " · só quando há oportunidade" : ""}`;
  document.getElementById("f-vps").innerHTML = P.server_30d ? `<span class="red">−${usd(P.server_30d)}</span>` : "$0.00";
  document.getElementById("f-net").innerHTML = pnl(P.net_30d);
  document.getElementById("f-net").parentElement.classList.toggle("loss", P.net_30d < 0);
  document.getElementById("f-net-s").textContent = `últimos 30 dias · desde o início ${P.net_all >= 0 ? "+" : "−"}$${Math.abs(P.net_all).toFixed(2)}`;
  const v = document.getElementById("verdict");
  v.textContent = P.verdict;
  v.style.color = P.verdict === "dando lucro" ? "var(--green)" : P.verdict === "dando prejuízo" ? "var(--red)" : "";
  const used = P.ai_budget ? Math.min(P.ai_month / P.ai_budget, 1) : 0;
  const bar = document.getElementById("budget-bar");
  bar.style.width = `${(used * 100).toFixed(1)}%`;
  bar.className = used >= 1 ? "full" : used > 0.8 ? "warn" : "";
  document.getElementById("budget-note").innerHTML = `Orçamento de IA este mês: <b>${usd(P.ai_month)}</b> de ${usd(P.ai_budget)}${used >= 1 ? " — esgotado: só o bot de Regras (grátis) trabalha" : ""}`;
}

function renderFoot() {
  if (!acc) return;
  const left = document.getElementById("room-left"), right = document.getElementById("room-right");
  if (!acc.running) { left.innerHTML = "Bots <b>desligados</b>"; right.textContent = ""; return; }
  const secs = nextCycleAt ? Math.round((nextCycleAt - Date.now()) / 1000) : null;
  left.innerHTML = acc.scanning ? `Ciclo <b>#${acc.cycle}</b> em andamento` : `Próxima análise em <b>${secs > 0 ? `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, "0")}` : "instantes"}</b>`;
  right.innerHTML = `Quem decide: <b>${acc.decision_mode === "llm" ? `🧠 ${esc(acc.model)}` : "regras"}</b>`;
}

// ─── advanced actions (real account) ───
function flatten(btn) {
  if (!confirm("Fechar TODAS as posições reais a preço de mercado e desligar os bots da conta real?")) return;
  run(btn, "Fechando…", () => api("/live/flatten", "POST"),
      (r) => (r.errors.length ? `Fechadas: ${r.closed.join(", ") || "nenhuma"} · erros: ${r.errors.join("; ")}` : `✓ Fechadas: ${r.closed.join(", ") || "nenhuma posição aberta"}`));
}
function testOrder(btn) {
  run(btn, "Enviando…", () => api("/live/test-order", "POST"),
      (r) => (r.position ? `✓ Ordem de teste: ${r.position.symbol} com stop e alvo na exchange` : "Sem ordem — veja o feed"));
}
function killReset(btn) {
  run(btn, "Rearmando…", () => api(KIND === "real" ? "/live/kill-switch/reset" : "/kill-switch/reset", "POST", { reason: "dashboard" }), "✓ Kill switch rearmado");
}

// ─── wallets (watch-only) ───
// EIP-6963: each wallet extension announces itself with a stable id (rdns), so we can ask for MetaMask by name
// instead of trusting window.ethereum, which Coinbase Wallet and others take over.
const announced = new Map();
window.addEventListener("eip6963:announceProvider", (e) => announced.set(e.detail.info.rdns, e.detail.provider));
window.dispatchEvent(new Event("eip6963:requestProvider"));

function metamaskProvider() {
  const byId = announced.get("io.metamask") || announced.get("io.metamask.flask");
  if (byId) return byId;
  const eth = window.ethereum;
  const list = eth?.providers || (eth ? [eth] : []);
  // older extensions without EIP-6963: only a provider that is MetaMask and nothing else
  return list.find((p) => p.isMetaMask && !p.isCoinbaseWallet && !p.isPhantom && !p.isBraveWallet && !p.isRabby && !p.isTrust) || null;
}

function phantomProvider() {
  const p = window.phantom?.solana;
  return p?.isPhantom ? p : null;
}
async function connectWallet(kind) {
  try {
    let address, label;
    if (kind === "metamask") {
      const mm = metamaskProvider();
      if (!mm) throw new Error("MetaMask não encontrada. Só conectamos a MetaMask e a Phantom — instale/ative a extensão MetaMask neste navegador (outras carteiras são ignoradas).");
      [address] = await mm.request({ method: "eth_requestAccounts" });   // public address only, no signature
      label = "MetaMask";
    } else {
      const provider = phantomProvider();
      if (!provider) throw new Error("Phantom não encontrada neste navegador. Instale a extensão Phantom no Chrome ou no Brave.");
      const res = await provider.connect();
      address = (res?.publicKey || provider.publicKey).toString();
      label = "Phantom";
    }
    await api("/wallets", "POST", { address, label });
    toast(`✓ ${label} conectada: ${short(address)} (só leitura)`);
    safe(loadWallets);
  } catch (e) { toast(`✖ ${e.message || e}`, true); }
}
async function removeWallet(addr) {
  await api(`/wallets/${encodeURIComponent(addr)}`, "DELETE");
  safe(loadWallets);
}
function walletCard(w) {
  const head = `<h3><span>${esc(w.label || "Carteira")} <span class="muted">${short(w.address)}</span></span><button onclick="removeWallet('${esc(w.address)}')">remover</button></h3>`;
  if (w.error) return `<div class="wcard">${head}<div class="red note">${esc(w.error)}</div></div>`;
  if (w.chain === "evm") {
    const hl = (k, name) => { const x = w[k] || {}; return x.error ? `<div>${name}<b>–</b></div>` : `<div>${name}<b>${usd(x.account_value)}</b>${x.positions || 0} posições</div>`; };
    return `<div class="wcard">${head}<div class="wbal"><div>USDC Arbitrum<b>${Number(w.arbitrum_usdc).toFixed(2)}</b></div>
      <div>ETH Arbitrum<b>${Number(w.arbitrum_eth).toFixed(5)}</b></div>${hl("hyperliquid", "Hyperliquid")}${hl("hyperliquid_testnet", "HL testnet")}</div></div>`;
  }
  return `<div class="wcard">${head}<div class="wbal"><div>SOL<b>${Number(w.sol).toFixed(4)}</b></div><div>Tokens<b>${w.tokens.length}</b></div></div></div>`;
}
async function loadWallets() {
  const { wallets } = await api("/wallets");
  document.getElementById("wallets").innerHTML = wallets.length ? wallets.map(walletCard).join("") : '<p class="note">Nenhuma carteira conectada.</p>';
}

// ─── team of bots (one shared wallet) + lab results ───
const MODEL_LOOK = {
  "rules": { label: "Regras · quedas", color: "#a3b1ab", ico: "📐" },
  "trend": { label: "Regras · tendência", color: "#67e8f9", ico: "📈" },
  "claude-fable-5-1": { label: "Fable 5.1", color: "#f5c451", ico: "✨" },
  "claude-opus-5-5": { label: "Opus 5.5", color: "#fb923c", ico: "🎼" },
  "claude-opus-5": { label: "Opus 5", color: "#f97362", ico: "🎻" },
  "claude-sonnet-5-5": { label: "Sonnet 5.5", color: "#60a5fa", ico: "📜" },
  "claude-haiku-5-5": { label: "Haiku 5.5", color: "#34d399", ico: "🍃" },
};
const ACTION_PT = { BUY: "🟢 comprar", HOLD: "⏸ esperar", SELL: "🔵 vender", sleep: "😴 dormindo", wake: "⏰ acordou",
                    entry: "🟢 comprou", exit: "🔵 vendeu", veto: "🟡 bloqueado" };
const FREE = new Set(["rules", "trend"]);   // deterministic bots: no AI cost
const look = (m) => MODEL_LOOK[m] || { label: m, color: "#9fe8c7", ico: "🤖" };
let lastArena = null;

async function loadArena() {
  const a = await api("/arena");
  lastArena = a;
  table("arena-table", ["#", "Bot", "Saldo", "Retorno", "Trades", "Acerto", "Estado"],
    a.leaderboard.map((r, i) => `<tr><td>${i + 1}</td><td><b>${esc(look(r.model).label)}</b><br><span class="muted">${esc(r.model)}</span></td>
      <td>${usd(r.equity)}</td><td><span class="${r.twr >= 0 ? "green" : "red"}">${pct(r.twr)}</span></td><td>${r.trades}</td>
      <td>${r.win_rate === null ? "–" : pct(r.win_rate)}</td><td class="muted">${esc(r.state)}</td></tr>`));
  renderTeam();
}

function renderTeam() {
  if (!acc || !lastArena) return;
  const lab = Object.fromEntries(lastArena.leaderboard.map((r) => [r.model, r]));
  const members = acc.team && acc.team.length ? acc.team : lastArena.leaderboard.map((r) => ({ model: r.model, name: r.model }));
  const liveName = acc.live?.running && Date.now() - new Date(acc.live.ts) < 30000 ? acc.live.agent : null;
  const awake = members.filter((m) => lab[m.model]?.running && !lab[m.model]?.needs_key).length;
  document.getElementById("team-head").innerHTML = acc.running
    ? `<b>${awake}</b> de ${members.length} bots ativos${acc.sleep?.until ? ` · carteira pausada até <b>${hhmm(acc.sleep.until)}</b>` : ""}`
    : "equipe desligada — clique em ▶ Start";
  const coffee = acc.running && acc.coffee_until;
  const cb = document.getElementById("btn-coffee");
  cb.classList.toggle("on", !!coffee);
  cb.textContent = coffee ? `☕ até ${hhmm(coffee)}` : "☕ Café";
  setHTML("team-grid", members.map((m) => {
    const L = look(m.model), r = lab[m.model] || {};
    const working = acc.running && (liveName === m.name || (r.state === "trabalhando" || r.state === "a trabalhar"));
    const cls = !acc.running || !r.running ? "off" : r.needs_key ? "nokey" : working ? "working" : (m.open || []).length || r.open?.length ? "holding" : r.sleep_until ? "sleeping" : "";
    const state = !acc.running || !r.running ? "desligado" : r.needs_key ? "falta chave da API" : working ? "analisando…"
      : r.sleep_until ? `dormindo até ${hhmm(r.sleep_until)}` : r.state || "aguardando";
    const last = m.last || (r.last_decision ? { action: r.last_decision.action, reason: r.last_decision.reason, market: r.last_decision.market } : null);
    const bubble = r.needs_key ? "Coloque a ANTHROPIC_API_KEY no .env para eu começar a decidir."
      : last ? `${last.market ? last.market + ": " : ""}${ACTION_PT[last.action] || last.action || ""} — ${(last.reason || "").replace(/^(a dormir|dormindo) [^:]*: /, "")}` : r.sleep_reason || "Ainda sem decisões.";
    const isRules = FREE.has(m.model);
    const tip = `<b>${esc(L.label)}</b> · ${isRules ? "regras fixas, grátis" : "decide com IA (Claude)"}
      ${tipRows([["Modelo", esc(m.model)], ["Status", esc(state)],
                 ["Mercado", last?.market ? esc(last.market) : null],
                 ["Posições", (m.open || []).length ? esc(m.open.join(", ")) : "nenhuma"],
                 ["Laboratório", `${usd(r.equity)} · ${pct(r.twr)} · ${r.trades ?? 0} trades${r.win_rate != null ? ` · acerto ${pct(r.win_rate)}` : ""}`],
                 ["Custo IA", isRules ? "grátis" : `${usd(r.ai_cost_usd ?? 0)} · líquido ${pnl(r.net_usd ?? 0)}`],
                 ["Café", coffee ? `☕ atento até ${hhmm(coffee)}` : null]])}
      <div class="wip"><b>Em andamento</b><br>${esc(working ? (r.live?.detail || "analisando os mercados…") : r.sleep_until ? `Dormindo até ${hhmm(r.sleep_until)} — ${r.sleep_reason || "sem sinais"}` : bubble)}</div>`;
    return `<div class="tb ${cls}" tabindex="0" style="--c:${L.color}" aria-label="${esc(L.label)}: ${esc(state)}">
      ${cls === "sleeping" ? '<span class="zz">z z</span>' : ""}${coffee && cls !== "off" ? '<span class="cup" title="com cafeína: sem pausas">☕</span>' : ""}
      <div class="head">${botFace(L.color, L.ico)}
        <div><div class="nm">${esc(L.label)}</div><span class="pill ${cls === "working" || cls === "holding" ? "on" : ""}">${esc(state)}</span></div></div>
      <div class="bubble">${esc(bubble)}</div>
      <div class="meta"><span>Na carteira<br><b>${m.trades ?? 0} trades ${pnl(m.pnl_usd ?? 0)}</b></span>
        <span>Laboratório<br><b>${usd(r.equity)} · ${pct(r.twr)}</b></span>
        <span>Custo IA<br><b>${FREE.has(m.model) ? "grátis" : usd(r.ai_cost_usd ?? 0)}</b></span>
        <span>Líquido<br><b>${pnl(r.net_usd ?? 0)}</b></span></div>
      <div class="tip" role="tooltip">${tip}</div>
    </div>`;
  }).join(""));
}

// ─── advanced (old Solana paper + readiness) ───
async function loadAdvanced() {
  const [s, r, dec] = await Promise.all([api("/status"), api("/readiness"), api("/decisions?limit=100")]);
  document.getElementById("old-paper").textContent = `${s.running ? "Rodando" : "Parado"} · patrimônio ${usd(s.equity)} · ${s.closed_trades} trades · ciclo #${s.cycle}`;
  document.getElementById("readiness").innerHTML = r.checks.map((c) => `<div class="note">${c.ok ? "✅" : "⬜"} ${esc(c.name)} (${esc(c.value)})</div>`).join("");
  table("decisions", ["Quando", "Tipo", "Ativo", "Motivo"], dec.decisions.map((d) =>
    `<tr><td>${time(d.ts)}</td><td class="k-${esc(d.kind)}">${esc(d.kind)}</td><td>${esc(d.symbol || "")}</td><td>${esc(d.reason)}</td></tr>`));
}

safe(refresh); safe(loadWallets); safe(loadArena); safe(loadAdvanced);
setInterval(() => safe(loadArena), 5000);
setInterval(() => safe(refresh), 2000);
setInterval(renderFoot, 1000);
setInterval(() => safe(loadAdvanced), 30000);
setInterval(() => safe(loadWallets), 60000);

// ─── daily report ───
async function loadDaily() {
  const r = await api(`/report/daily?kind=${KIND}&days=14`);
  document.getElementById("rep-tag").textContent = KIND === "sim" ? "conta simulada" : "conta real";
  table("daily", ["Dia", "Trades", "Ganhos / perdas", "Acerto", "Lucro trades", "Custo IA", "Líquido", "Saldo", "Melhor / pior"],
    r.days.map((d) => `<tr><td><b>${esc(d.date)}</b><br><span class="muted">${d.cycles} ciclos · ${d.sleeps} pausas${d.errors ? ` · <span class="red">${d.errors} erros</span>` : ""}</span></td>
      <td>${d.trades}</td><td><span class="green">${d.wins}</span> / <span class="red">${d.losses}</span></td>
      <td>${d.win_rate === null ? "–" : pct(d.win_rate)}</td><td>${pnl(d.pnl_usd)}<br><span class="muted">taxas ${usd(d.fees_usd, 3)}</span></td>
      <td>${d.ai_cost_usd ? `<span class="red">−${usd(d.ai_cost_usd, 3)}</span>` : "$0"}<br><span class="muted">${d.ai_calls} análises</span></td>
      <td>${pnl(d.net_usd)}</td><td>${d.equity_start === null ? "–" : `${usd(d.equity_start)} → ${usd(d.equity_end)}`}</td>
      <td class="muted">${d.best ? `${esc(d.best.symbol)} ${pnl(d.best.pnl_usd)} / ${esc(d.worst.symbol)} ${pnl(d.worst.pnl_usd)}` : "–"}</td></tr>`));
}
safe(loadDaily);
setInterval(() => safe(loadDaily), 30000);

