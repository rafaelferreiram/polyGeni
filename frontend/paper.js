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
    safe(refresh);
  }
}

// ─── account: simulation and real are the same view ───
let KIND = "sim";
try { KIND = localStorage.getItem("acct") || "sim"; } catch (e) { /* storage blocked */ }
let acc = null;

function setKind(k) {
  KIND = k;
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
  run(btn, on ? "A parar…" : "A ligar os bots…",
      () => api(`/account/${on ? "stop" : "start"}`, "POST", { kind: KIND }),
      on ? "■ Equipa parada — não abre novas posições (stops continuam ativos)"
         : (r) => `▶ Equipa a trabalhar na carteira ${KIND === "sim" ? "simulada" : "REAL"}${r.llm_bots_need_key ? " — só o bot de Regras trabalha até pôr ANTHROPIC_API_KEY" : ""}`);
}

function setMode(mode, btn) {
  run(btn, "…", () => api("/account/mode", "POST", { mode }),
      mode === "llm" ? "🧠 A equipa de bots decide; o Coordenador escolhe o melhor por mercado" : "✓ Só o bot de regras decide");
}

// ─── animated bot room ───
const STATIONS = [
  { key: "scout", ico: "🔭", name: "Scout", role: "observa os mercados" },
  { key: "decider", ico: "🎯", name: "Estratégia", role: "decide se compra" },
  { key: "risk", ico: "🛡️", name: "Risk", role: "limites e tamanho" },
  { key: "executor", ico: "⚙️", name: "Executor", role: "envia a ordem" },
  { key: "guard", ico: "👁️", name: "Guarda", role: "vigia stop e alvo" },
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
    const state = !a.running ? "desligado" : working ? "a trabalhar" : cls === "idle" ? "à espera" : cls === "blocked" ? "bloqueou" : "feito";
    const bubble = !a.running ? "Carregue em ▶ Start para me pôr a trabalhar"
      : working && !replay ? (a.live.detail || "a pensar…") : (d.last_reason || (st.key === "risk" || st.key === "executor" ? "Só entro em ação quando há sinal de compra" : "…"));
    const wireOn = a.running && (working || (replay && replay.order.indexOf(st.key) >= 0 && replay.order.indexOf(st.key) < replay.i));
    return `${i ? `<div class="wire ${wireOn ? "on" : ""}"></div>` : ""}
      <div class="st ${cls}">
        ${cls === "idle" ? '<span class="zz">z z</span>' : ""}
        <div class="head"><div class="bot"><span class="ant"></span><span class="mouth"></span><span class="badge-ico">${d.ico || st.ico}</span></div>
          <div><div class="nm">${esc(d.name || st.name)}</div><div class="rl">${esc(d.role || st.role)}</div></div></div>
        <div class="state"><span class="dot"></span>${state}</div>
        <div class="bubble">${esc(bubble)}</div>
      </div>`;
  }).join("");
  document.getElementById("line").innerHTML = html;
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
                reconcile: "🧾 reconciliação", arm: "🔓 ligado", disarm: "🔒 desligado", mode: "🔀 modo" };
let nextCycleAt = null;

async function refresh() {
  const a = await api(`/account?kind=${KIND}`);
  acc = a;
  // header
  document.querySelectorAll("#acct-seg button").forEach((b) => b.classList.toggle("on", b.dataset.k === KIND));
  document.querySelectorAll("#mode-seg button").forEach((b) => b.classList.toggle("on", b.dataset.m === a.decision_mode));
  document.querySelector('#mode-seg [data-m="llm"]').title = a.llm_available ? "O LLM decide compras e vendas" : "Precisa de ANTHROPIC_API_KEY no .env";
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
    setup.innerHTML = `<b>Para ligar a conta real falta:</b> ${problems.map(esc).join(" · ")}${a.exchange_error ? ` <span class="red">${esc(a.exchange_error)}</span>` : ""}
      <ol><li>Depositar ≥ 5 USDC na Hyperliquid (rede Arbitrum) com a MetaMask</li>
      <li>Testnet: Faucet → More → API → <b>Generate</b> → Authorize (guarde a chave só no <code>.env</code>)</li>
      <li>No <code>.env</code>: <code>HL_NETWORK=testnet</code>, <code>HL_ACCOUNT_ADDRESS=0x…</code>, <code>HL_API_WALLET_KEY=…</code> e reiniciar</li></ol>`;
  }
  // KPIs
  document.getElementById("k-eq-l").textContent = KIND === "sim" ? "Saldo simulado" : "Saldo real na Hyperliquid";
  document.getElementById("k-eq").textContent = usd(KIND === "real" && a.exchange_balance !== undefined ? a.exchange_balance : a.equity);
  document.getElementById("k-eq-s").textContent = `cash ${usd(a.cash)} · em posição ${usd(a.exposure)}`;
  const P = a.profit || {};
  document.getElementById("k-pnl").innerHTML = pnl(P.net_all);
  const all = a.performance?.all;
  document.getElementById("k-pnl-s").textContent = `trades ${usd(P.trading_all)} · IA −${usd(P.ai_all)}${all ? ` · segurar BTC ${pct(all.hold_btc)}` : ""}`;
  renderProfit(P);
  const p = a.positions[0];
  const kp = document.getElementById("k-pos");
  kp.textContent = p ? p.symbol : "Sem posição";
  kp.classList.toggle("soft", !p);
  document.getElementById("k-pos-s").innerHTML = p ? `${usd(p.cost_usd)} → ${usd(p.mark_value)} ${pnl(p.mark_value !== null ? p.mark_value - p.cost_usd : null)}` : "à espera de uma oportunidade";
  const kt = document.getElementById("k-tr");
  kt.textContent = a.trades.length ? `${a.trades.length} · ${pct(a.win_rate)}` : "Nenhum";
  kt.classList.toggle("soft", !a.trades.length);
  document.getElementById("k-tr-s").innerHTML = a.trades.length ? `total ${pnl(a.trades.reduce((x, t) => x + t.pnl_usd, 0))}` : "sem trades fechados";
  // scan bar + room
  nextCycleAt = a.running && a.heartbeat ? new Date(a.heartbeat.ts).getTime() + a.cycle_interval_sec * 1000 : null;
  const scanning = a.scanning || (a.live?.running && Date.now() - new Date(a.live.ts) < 60000);
  document.getElementById("scanbar").hidden = !scanning;
  if (scanning) document.getElementById("scan-text").textContent = `A analisar o mercado (ciclo #${a.cycle})…`;
  document.getElementById("room-tag").textContent = KIND === "sim" ? "simulação" : `real · ${a.network}`;
  document.getElementById("room-head").innerHTML = scanning ? '<span class="live-chip">a analisar o mercado</span>' : "";
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
  v.style.color = P.verdict === "a dar lucro" ? "var(--green)" : P.verdict === "a dar prejuízo" ? "var(--red)" : "";
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
  left.innerHTML = acc.scanning ? `Ciclo <b>#${acc.cycle}</b> a decorrer` : `Próxima análise em <b>${secs > 0 ? `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, "0")}` : "instantes"}</b>`;
  right.innerHTML = `Quem decide: <b>${acc.decision_mode === "llm" ? `🧠 ${esc(acc.model)}` : "regras"}</b>`;
}

// ─── advanced actions (real account) ───
function flatten(btn) {
  if (!confirm("Fechar TODAS as posições reais a mercado e desligar os bots da conta real?")) return;
  run(btn, "A fechar…", () => api("/live/flatten", "POST"),
      (r) => (r.errors.length ? `Fechadas: ${r.closed.join(", ") || "nenhuma"} · erros: ${r.errors.join("; ")}` : `✓ Fechadas: ${r.closed.join(", ") || "nenhuma posição aberta"}`));
}
function testOrder(btn) {
  run(btn, "A enviar…", () => api("/live/test-order", "POST"),
      (r) => (r.position ? `✓ Ordem de teste: ${r.position.symbol} com stop e alvo na exchange` : "Sem ordem — ver o feed"));
}
function killReset(btn) {
  run(btn, "A rearmar…", () => api(KIND === "real" ? "/live/kill-switch/reset" : "/kill-switch/reset", "POST", { reason: "dashboard" }), "✓ Kill switch rearmado");
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
      if (!mm) throw new Error("MetaMask não encontrada. Só ligamos à MetaMask e à Phantom — instale/ative a extensão MetaMask neste browser (outras carteiras são ignoradas).");
      [address] = await mm.request({ method: "eth_requestAccounts" });   // public address only, no signature
      label = "MetaMask";
    } else {
      const provider = phantomProvider();
      if (!provider) throw new Error("Phantom não encontrada neste browser. Instale a extensão Phantom no Chrome ou Brave.");
      const res = await provider.connect();
      address = (res?.publicKey || provider.publicKey).toString();
      label = "Phantom";
    }
    await api("/wallets", "POST", { address, label });
    toast(`✓ ${label} ligada: ${short(address)} (só leitura)`);
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
  document.getElementById("wallets").innerHTML = wallets.length ? wallets.map(walletCard).join("") : '<p class="note">Nenhuma carteira ligada.</p>';
}

// ─── team of bots (one shared wallet) + lab results ───
const MODEL_LOOK = {
  "rules": { label: "Regras", color: "#a3b1ab", ico: "📐" },
  "claude-fable-5-1": { label: "Fable 5.1", color: "#f5c451", ico: "✨" },
  "claude-opus-5-5": { label: "Opus 5.5", color: "#fb923c", ico: "🎼" },
  "claude-opus-5": { label: "Opus 5", color: "#f97362", ico: "🎻" },
  "claude-sonnet-5-5": { label: "Sonnet 5.5", color: "#60a5fa", ico: "📜" },
  "claude-haiku-5-5": { label: "Haiku 5.5", color: "#34d399", ico: "🍃" },
};
const ACTION_PT = { BUY: "🟢 comprar", HOLD: "⏸ esperar", SELL: "🔵 vender", sleep: "😴 a dormir", wake: "⏰ acordou",
                    entry: "🟢 comprou", exit: "🔵 vendeu", veto: "🟡 bloqueado" };
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
    ? `<b>${awake}</b> de ${members.length} bots ativos${acc.sleep?.until ? ` · carteira em pausa até <b>${new Date(acc.sleep.until).toLocaleTimeString().slice(0, 5)}</b>` : ""}`
    : "equipa desligada — carregue em ▶ Start";
  document.getElementById("team-grid").innerHTML = members.map((m) => {
    const L = look(m.model), r = lab[m.model] || {};
    const working = acc.running && (liveName === m.name || r.state === "a trabalhar");
    const cls = !acc.running || !r.running ? "off" : r.needs_key ? "nokey" : working ? "working" : (m.open || []).length || r.open?.length ? "holding" : r.sleep_until ? "sleeping" : "";
    const state = !acc.running || !r.running ? "desligado" : r.needs_key ? "falta chave da API" : working ? "a analisar…"
      : r.sleep_until ? `a dormir até ${new Date(r.sleep_until).toLocaleTimeString().slice(0, 5)}` : r.state || "à espera";
    const last = m.last || (r.last_decision ? { action: r.last_decision.action, reason: r.last_decision.reason, market: r.last_decision.market } : null);
    const bubble = r.needs_key ? "Ponha ANTHROPIC_API_KEY no .env para eu começar a decidir."
      : last ? `${last.market ? last.market + ": " : ""}${ACTION_PT[last.action] || last.action || ""} — ${(last.reason || "").replace(/^a dormir [^:]*: /, "")}` : r.sleep_reason || "Ainda sem decisões.";
    return `<div class="tb ${cls}">
      ${cls === "sleeping" ? '<span class="zz">z z</span>' : ""}
      <div class="head"><div class="bot" style="--c:${L.color}"><span class="ant"></span><span class="mouth"></span><span class="badge-ico">${L.ico}</span></div>
        <div><div class="nm">${esc(L.label)}</div><span class="pill ${cls === "working" || cls === "holding" ? "on" : ""}">${esc(state)}</span></div></div>
      <div class="bubble">${esc(bubble)}</div>
      <div class="meta"><span>Na carteira<br><b>${m.trades ?? 0} trades ${pnl(m.pnl_usd ?? 0)}</b></span>
        <span>Laboratório<br><b>${usd(r.equity)} · ${pct(r.twr)}</b></span>
        <span>Custo IA<br><b>${m.model === "rules" ? "grátis" : usd(r.ai_cost_usd ?? 0)}</b></span>
        <span>Líquido<br><b>${pnl(r.net_usd ?? 0)}</b></span></div>
    </div>`;
  }).join("");
}

// ─── advanced (old Solana paper + readiness) ───
async function loadAdvanced() {
  const [s, r, dec] = await Promise.all([api("/status"), api("/readiness"), api("/decisions?limit=100")]);
  document.getElementById("old-paper").textContent = `${s.running ? "A correr" : "Parado"} · património ${usd(s.equity)} · ${s.closed_trades} trades · ciclo #${s.cycle}`;
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
