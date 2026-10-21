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
  setTimeout(() => { safe(loadChart); safe(loadEquityChart); }, 800);
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
    if (!confirm(`Ligar os bots na conta REAL (${net})?\n\nLimites: capital ${usd(L.max_capital_usd, 0)}, posição ${usd(L.max_position_usd, 0)}, alavancagem ${L.leverage}x isolada, stop e alvo na exchange, perda diária ${pct(L.max_daily_loss_pct)}.`)) return;
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
      (r) => `☕ Equipe com cafeína até ${hhmm(r.coffee_until)} nas contas simulada e real — modo day trade, até 24 compras por dia`);
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

// real numbers of the last finished cycle for each stage (from the engine, not illustrative)
function stageMetric(key, a) {
  const lc = a.last_cycle || {};
  if (a.kill_switch?.active && key !== "guard") return '<span class="red">parado pelo kill switch</span>';
  if (lc.skipped && key !== "guard") return `<span class="muted">${esc(lc.skipped)}</span>`;
  const n = (v) => `<b>${v ?? 0}</b>`;
  return { scout: `${n(lc.candidates)} mercados lidos`, decider: `${n(lc.signals)} sinais de compra`,
           risk: `${n(lc.vetoes)} vetos · ${n(lc.entries)} aprovadas`, executor: `${n(lc.entries)} ordens enviadas`,
           guard: `${n(a.positions.length)} posições vigiadas` }[key] || "";
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
        <div class="state"><span class="dot"></span>${state}${d.last_run ? ` · ${hhmm(d.last_run)}` : ""}</div>
        <div class="metric">${stageMetric(st.key, a)}</div>
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
  // status bar + kill switch banner (always visible)
  renderStatusBar(a);
  // KPIs
  document.getElementById("k-eq-l").textContent = KIND === "sim" ? "Saldo simulado" : "Saldo real na Hyperliquid";
  document.getElementById("k-eq").textContent = usd(KIND === "real" && a.exchange_balance !== undefined ? a.exchange_balance : a.equity);
  document.getElementById("k-eq-s").textContent = `caixa ${usd(a.cash)} · em posição ${usd(a.exposure)}`;
  const P = a.profit || {};
  document.getElementById("k-pnl").innerHTML = pnl(netOf(P, "all"));
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
  document.getElementById("room-tag").textContent = KIND === "sim" ? "simulação · ao vivo" : `real · ${a.network} · ao vivo`;
  const lcx = a.last_cycle || {};
  document.getElementById("room-head").innerHTML = lcx.finished ? `último ciclo <b>#${lcx.cycle}</b> terminou às <b>${hhmm(lcx.finished)}</b> (há ${ago(lcx.finished)})` : "";
  if (scanning) document.getElementById("room-head").innerHTML = '<span class="live-chip">analisando o mercado agora</span>';
  if (!replay) renderRoom(a);
  renderTeam();
  renderThermo(a);
  renderPositions(a);
  renderGoals(a);
  renderRadar(a);
  maybeReplay(a);
  renderFoot();
  // trades
  document.getElementById("trades-total").innerHTML = a.trades.length ? pnl(a.trades.reduce((x, t) => x + t.pnl_usd, 0)) : "";
  table("trades", ["Ativo", "Entrada → saída", "Lucro", "Motivo"], a.trades.map((t) => `
    <tr><td><b>${esc(t.symbol)}</b><br><span class="muted">${time(t.exit_ts)}</span></td>
    <td>${Number(t.entry_price).toPrecision(6)} → ${Number(t.exit_price).toPrecision(6)}</td>
    <td>${pnl(t.pnl_usd)}<br><span class="muted">${pct(t.pnl_pct)}</span></td><td class="muted">${esc(t.reason)}</td></tr>`));
  const L = a.limits;
  document.getElementById("limits").textContent = `Limites (iguais na simulação e no real): capital ${usd(L.max_capital_usd, 0)}, posição ${usd(L.max_position_usd, 0)}, ${L.max_positions || "auto"} de cada vez, ${L.leverage}x isolada só compra, perda diária ${pct(L.max_daily_loss_pct)}, mercados ${L.markets.join(", ")}.`;
}

// ─── open positions: how each trade is doing between its stop and its target ───
const fmtPx = (v) => (v == null ? "–" : `$${Number(v) >= 100 ? Number(v).toFixed(2) : Number(v).toPrecision(4)}`);
const botName = (s) => (s?.startsWith("llm:") ? look(s.slice(4)).label : s === "trend" ? "Tendência" : "Quedas");
function renderPositions(a) {
  const ps = a.positions || [];
  document.getElementById("pos-tag").textContent = KIND === "sim" ? "conta simulada" : "conta real";
  const closed = a.trades.length;
  document.getElementById("pos-head").innerHTML = `${ps.length} aberta${ps.length === 1 ? "" : "s"} · ${closed} trade${closed === 1 ? "" : "s"} fechado${closed === 1 ? "" : "s"}${closed ? ` · resultado ${pnl(a.trades.reduce((x, t) => x + t.pnl_usd, 0))}` : ""}`;
  setHTML("positions", ps.length ? ps.map((p) => {
    const value = p.mark_value ?? p.cost_usd;
    const now = p.mark_price ?? p.entry_price * value / p.cost_usd;
    const pl = value - p.cost_usd, plPct = pl / p.cost_usd;
    const span = p.tp_price - p.stop_price;
    const at = (x) => `${(clamp01((x - p.stop_price) / span) * 100).toFixed(1)}%`;
    return `<div class="pos ${pl >= 0 ? "win" : "lose"}">
      <div class="top"><span class="sym">${esc(p.symbol)}</span><span class="pl">${pnl(pl)} <small class="muted">${(plPct * 100).toFixed(2)}%</small></span></div>
      <div class="sub"><span>bot ${esc(botName(p.strategy))} · aberta há ${ago(p.opened_at)}</span><span>${usd(p.cost_usd)}</span></div>
      <div class="track" title="entre o stop e o alvo"><span class="entry" style="left:${at(p.entry_price)}"></span><span class="now" style="left:${at(now)}"></span></div>
      <div class="ends"><span>🛑 stop ${fmtPx(p.stop_price)}</span><span>entrada ${fmtPx(p.entry_price)} · agora ${fmtPx(now)}</span><span>🎯 ${fmtPx(p.tp_price)}</span></div>
    </div>`;
  }).join("") : `<div class="empty">Nenhuma posição aberta agora — os bots estão procurando a próxima entrada (veja o radar abaixo).</div>`);
}

// ─── daily goals: what the bots aim for today vs what they did ───
function renderGoals(a) {
  const g = a.goals;
  if (!g) return;
  const pctOf = (x, t) => `${(clamp01(t ? x / t : 0) * 100).toFixed(0)}%`;
  const tag = document.getElementById("goal-tag");
  tag.textContent = g.hit ? (g.rest ? "🎯 meta batida — bots descansando até amanhã" : "🎯 meta batida") : KIND === "sim" ? "conta simulada" : "conta real";
  tag.style.color = g.hit ? "var(--green)" : "";
  const card = (done, k, v, w, s) => `<div class="goal ${done ? "done" : ""}"><div class="k">${k}</div><div class="v">${v}</div>
    <div class="bar"><span style="width:${w}"></span></div><div class="s">${s}</div></div>`;
  setHTML("goals", [
    card(g.hit, "💰 Ganho do dia (com posições abertas)", `${pnl(g.day_gain_usd)} <small>de +$${g.profit.target_usd.toFixed(2)} (${(g.profit.target_pct * 100).toFixed(1)}%)</small>`,
         pctOf(g.day_gain_usd, g.profit.target_usd), `${g.rest ? "ao bater a meta os bots param de comprar até amanhã · " : ""}trades fechados ${pnl(g.profit.done_usd)} · perda máx. −$${Math.abs(g.loss_limit_usd).toFixed(2)}`),
    card(g.trades.done >= g.trades.target, "🔁 Trades abertos hoje", `${g.trades.done} <small>de ${g.trades.target}</small>`,
         pctOf(g.trades.done, g.trades.target), `${g.trades.closed} fechados · ${g.trades.wins} com lucro · sem limite baixo: até ${a.coffee_until ? 24 : 12}/dia com sinal válido`),
    card(g.analyses.done >= g.analyses.target_so_far, "🔍 Análises (ciclos) hoje", `${g.analyses.done} <small>de ${g.analyses.target_so_far} até agora</small>`,
         pctOf(g.analyses.done, g.analyses.target_so_far), "1 ciclo a cada 5 min, 29 moedas por ciclo"),
  ].join(""));
}

// ─── radar: every coin the bots watch, and the exact level that would trigger a buy ───
let radarAll = false;
const STATUS_PT = { pronto: "✅ pronto", perto: "👀 perto", aguardando: "⏳ aguardando", "sem tendência": "⬇ sem tendência" };
function renderRadar(a) {
  const w = a.market_temp?.watch || [];
  document.getElementById("radar-all").textContent = radarAll ? "Ver só as melhores" : `Ver todas (${w.length})`;
  const near = w.filter((r) => r.status === "pronto" || r.status === "perto").length;
  document.getElementById("radar-head").textContent = a.market_temp?.ts ? `${near} perto de compra · atualizado há ${ago(a.market_temp.ts)}` : "aguardando a primeira análise";
  const rows = (radarAll ? w : w.slice(0, 8)).map((r) => `<tr onclick="showChart('${esc(r.symbol)}')" title="ver gráfico">
    <td><b>${esc(r.symbol)}</b><br><span class="muted">${r.ch24 >= 0 ? "+" : ""}${r.ch24}% 24h</span></td>
    <td>${fmtPx(r.price)}</td>
    <td>${r.strong ? "⬆⬆ forte" : r.up ? "⬆ alta" : "⬇ baixa"}<br><span class="muted">RSI ${r.rsi}</span></td>
    <td>${r.up ? `abaixo de <b>${fmtPx(r.dip_at)}</b><br><span class="muted">${r.dip_pct}% de distância${r.rsi >= 30 ? " · RSI precisa cair" : ""}</span>` : '<span class="muted">só em alta</span>'}</td>
    <td>${r.strong ? `acima de <b>${fmtPx(r.break_at)}</b><br><span class="muted">+${Math.max(r.break_pct, 0).toFixed(2)}% de distância</span>` : '<span class="muted">precisa de tendência forte</span>'}</td>
    <td>${(() => { const s = lastWhales?.coins?.[r.symbol]; return s && s["1h"].n ? `${WHALE_PT[s.pressure]}<br><span class="muted">${kusd(s["1h"].net_usd)} líquido 1h</span>` : '<span class="muted">—</span>'; })()}</td>
    <td><span class="st-chip ${r.status === "pronto" ? "pronto" : r.status === "perto" ? "perto" : ""}">${STATUS_PT[r.status] || esc(r.status)}</span>${r.ready_by ? `<br><span class="muted">sinal do bot ${esc(r.ready_by === "quedas" ? "Quedas" : "Tendência")}</span>` : ""}</td></tr>`);
  table("radar-table", ["Moeda", "Preço", "Tendência", "📐 Compra na queda", "📈 Compra no rompimento", "🐋 Baleias 1h", "Status"], rows);
}

// ─── thermometer: market temperature (from the bots' own scan) and our performance vs simply holding BTC ───
const clamp01 = (x) => Math.max(0, Math.min(1, x));
function gauge(id, score, label) {
  document.getElementById(`${id}-n`).style.left = `${(clamp01(score / 100) * 100).toFixed(1)}%`;
  document.getElementById(`${id}-lbl`).textContent = label;
}
function renderThermo(a) {
  const t = a.market_temp || {};
  if (t.n) {
    const score = (t.uptrend / t.n) * 60 + clamp01((t.avg_rsi - 30) / 40) * 40;
    gauge("t-mkt", score, score < 35 ? "❄️ Frio" : score < 65 ? "🌤 Morno" : "🔥 Quente");
    const coins = (l) => l.map(([s, c]) => `${esc(s)} ${c >= 0 ? "+" : ""}${c.toFixed(1)}%`).join(" · ");
    document.getElementById("t-mkt-facts").innerHTML = `
      <span><b>${t.uptrend}/${t.n}</b> moedas em alta</span><span><b>${t.avg_ch24 >= 0 ? "+" : ""}${t.avg_ch24}%</b> média 24h</span>
      <span><b>${t.near.length}</b> perto de compra${t.near.length ? `: ${esc(t.near.slice(0, 4).join(", "))}` : ""}</span>
      <span class="wide">⬆ ${coins(t.best)}</span><span class="wide">⬇ ${coins(t.worst)}</span>`;
  } else {
    gauge("t-mkt", 50, "–");
    document.getElementById("t-mkt-facts").innerHTML = "<span>aguardando a primeira análise</span>";
  }
  const all = a.performance?.all || {};
  const ours = all.twr ?? 0, btc = all.hold_btc;
  const edge = btc == null ? 0 : ours - btc;
  gauge("t-bot", 50 + edge * 1000, !a.trades.length ? "⏳ Sem trades ainda" : edge > 0.002 ? "🏆 Acima do mercado" : edge < -0.002 ? "📉 Abaixo do mercado" : "⚖️ Empatado");
  document.getElementById("t-bot-facts").innerHTML = `
    <span><b>${pct(ours)}</b> nossos bots</span><span><b>${btc == null ? "–" : pct(btc)}</b> só segurar BTC</span>
    <span><b>${a.trades.length ? pct(a.win_rate) : "–"}</b> acerto</span><span><b>${pnl(a.profit?.net_all ?? 0)}</b> lucro líquido</span>`;
}

// "Lucro real" can be shown with or without the AI cost (the user's choice, remembered in this browser)
let withAI = true;
try { withAI = localStorage.getItem("withAI") !== "0"; } catch (e) { /* storage blocked */ }
const netOf = (P, span) => (span === "all" ? P.net_all + (withAI ? 0 : P.ai_all) : P.net_30d + (withAI ? 0 : P.ai_30d));
function toggleAI(on) {
  withAI = on;
  try { localStorage.setItem("withAI", on ? "1" : "0"); } catch (e) { /* ignore */ }
  safe(refresh);
}

function renderProfit(P) {
  if (!P) return;
  document.getElementById("ai-toggle").checked = withAI;
  document.getElementById("f-ai").parentElement.classList.toggle("excluded", !withAI);
  document.getElementById("op-ai").classList.toggle("excluded", !withAI);
  document.getElementById("f-trade").innerHTML = pnl(P.trading_30d);
  document.getElementById("f-ai").innerHTML = P.ai_30d ? `<span class="red">−${usd(P.ai_30d)}</span>` : "$0.00";
  document.getElementById("f-ai-s").textContent = `${P.ai_calls_30d} análises pagas em 30 dias${P.prefilter ? " · só quando há oportunidade" : ""}`;
  document.getElementById("f-vps").innerHTML = P.server_30d ? `<span class="red">−${usd(P.server_30d)}</span>` : "$0.00";
  const n30 = netOf(P, "30d"), nAll = netOf(P, "all");
  document.getElementById("f-net").innerHTML = pnl(n30);
  document.getElementById("f-net").parentElement.classList.toggle("loss", n30 < 0);
  document.getElementById("f-net-l").textContent = withAI ? "Lucro líquido" : "Lucro sem a IA";
  document.getElementById("f-net-s").textContent = `últimos 30 dias · desde o início ${nAll >= 0 ? "+" : "−"}$${Math.abs(nAll).toFixed(2)}`;
  const v = document.getElementById("verdict");
  const verdict = !(P.trading_all || (withAI && P.ai_all)) ? "sem dados ainda" : nAll > 0 ? "dando lucro" : "dando prejuízo";
  v.textContent = verdict;
  v.style.color = verdict === "dando lucro" ? "var(--green)" : verdict === "dando prejuízo" ? "var(--red)" : "";
  const used = P.ai_budget ? Math.min(P.ai_month / P.ai_budget, 1) : 0;
  const bar = document.getElementById("budget-bar");
  bar.style.width = `${(used * 100).toFixed(1)}%`;
  bar.className = used >= 1 ? "full" : used > 0.8 ? "warn" : "";
  document.getElementById("budget-note").innerHTML = `IA este mês: <b>${usd(P.ai_month)}</b> de ${usd(P.ai_budget)} permitidos
    <span title="A IA só gasta o que o trading paga: base de ${usd(P.ai_base)} para aprender + ${(P.ai_share * 100).toFixed(0)}% do lucro real do mês, até ${usd(P.ai_hard_cap)}.">(base ${usd(P.ai_base)} + ${(P.ai_share * 100).toFixed(0)}% do lucro ⓘ)</span>${used >= 1 ? " — <b>IA pausada</b>: os bots de regras (grátis) continuam" : ""}`;
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
  if (KIND === "real" && !confirm("Rearmar o kill switch da conta REAL? Os bots voltam a poder abrir posições com dinheiro real.")) return;
  run(btn, "Rearmando…", () => api("/account/kill-switch/reset", "POST", { kind: KIND }), "✓ Kill switch rearmado — os bots voltam a operar");
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
  "rules": { label: "Quedas", color: "#a3b1ab", ico: "📐", role: "compra quando cai numa alta" },
  "trend": { label: "Tendência", color: "#67e8f9", ico: "📈", role: "compra quando dispara" },
  "claude-fable-5-1": { label: "Fable 5.1", color: "#f5c451", ico: "✨" },
  "claude-opus-5-5": { label: "Opus 5.5", color: "#fb923c", ico: "🎼" },
  "claude-opus-5": { label: "Opus 5", color: "#f97362", ico: "🎻" },
  "claude-sonnet-5-5": { label: "Sonnet 5.5", color: "#60a5fa", ico: "📜" },
  "claude-haiku-5-5": { label: "Haiku 5.5", color: "#34d399", ico: "🍃" },
};
const ACTION_PT = { BUY: "🟢 comprar", HOLD: "⏸ esperar", SELL: "🔵 vender", sleep: "😴 dormindo", wake: "⏰ acordou",
                    entry: "🟢 comprou", exit: "🔵 vendeu", veto: "🟡 bloqueado" };
const FREE = new Set(["rules", "trend"]);   // deterministic bots: no AI cost
const look = (m) => ({ role: "IA analisa e decide", ...(MODEL_LOOK[m] || { label: m, color: "#9fe8c7", ico: "🤖" }) });
let lastArena = null;

async function loadArena() {
  const a = await api("/arena");
  lastArena = a;
  table("arena-table", ["#", "Bot", "Saldo", "Retorno", "Trades", "Acerto", "Estado"],
    a.leaderboard.map((r, i) => `<tr><td>${i + 1}</td><td><b>${esc(look(r.model).label)}</b><br><span class="muted">${esc(r.model)}</span></td>
      <td>${usd(r.equity)}</td><td><span class="${r.twr >= 0 ? "green" : "red"}">${pct(r.twr)}</span></td><td>${r.trades}</td>
      <td>${r.win_rate === null ? "–" : pct(r.win_rate)}</td><td class="muted">${esc(r.state)}</td></tr>`));
  renderTeam();
  renderBoost();
}

function renderTeam() {
  if (!acc || !lastArena) return;
  const lab = Object.fromEntries(lastArena.leaderboard.map((r) => [r.model, r]));
  const members = acc.team && acc.team.length ? acc.team : lastArena.leaderboard.map((r) => ({ model: r.model, name: r.model }));
  const liveName = acc.live?.running && Date.now() - new Date(acc.live.ts) < 30000 ? acc.live.agent : null;
  const awake = members.filter((m) => lab[m.model]?.running && !lab[m.model]?.needs_key).length;
  document.getElementById("team-head").innerHTML = acc.running
    ? `<b>${awake}</b> de ${members.length} ligados`
    : "desligados — clique em ▶ Start";
  const coffee = acc.running && acc.coffee_until;
  const cb = document.getElementById("btn-coffee");
  cb.classList.toggle("on", !!coffee);
  cb.textContent = coffee ? `☕ até ${hhmm(coffee)}` : "☕ Café";
  const rankOf = Object.fromEntries([...members].sort((x, y) => ((lab[y.model]?.reward?.points) ?? 0) - ((lab[x.model]?.reward?.points) ?? 0)).map((m, i) => [m.model, i + 1]));
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
                 ["Recompensa", r.reward ? `${r.reward.points} pts · ${esc(r.reward.tier)} · posição ${usd(r.reward.size_usd, 0)}` : null],
                 ["Café", coffee ? `☕ atento até ${hhmm(coffee)}` : null]])}
      <div class="wip"><b>Em andamento</b><br>${esc(working ? (r.live?.detail || "analisando os mercados…") : r.sleep_until ? `Dormindo até ${hhmm(r.sleep_until)} — ${r.sleep_reason || "sem sinais"}` : bubble)}</div>`;
    const open = [...new Set([...(m.open || []), ...(r.open || [])])];
    const chip = cls === "off" ? "⏸ Desligado" : cls === "nokey" ? "🔑 Falta chave" : cls === "working" ? "⚡ Analisando"
      : cls === "holding" ? `📦 Em posição: ${open.join(", ")}` : cls === "sleeping" ? `😴 Dorme até ${hhmm(r.sleep_until)}` : "👀 Vigiando";
    const now = last ? `${last.market ? `<b>${esc(last.market)}</b> · ` : ""}${esc(ACTION_PT[last.action] || last.action || "")}` : "ainda sem decisões";
    return `<div class="tb ${cls}" tabindex="0" style="--c:${L.color}" aria-label="${esc(L.label)}: ${esc(state)}">
      ${coffee && cls !== "off" ? '<span class="cup" title="com cafeína: sem pausas">☕</span>' : ""}
      <div class="head">${botFace(L.color, L.ico)}
        <div><div class="nm">${esc(L.label)}${r.model_now && r.model_now !== m.model ? ` <small class="green">🚀 ${esc(look(r.model_now).label)}</small>` : ""}</div><div class="rl">${esc(L.role)}</div></div></div>
      <span class="chip">${esc(chip)}</span>
      <div class="now">Última decisão: ${now}</div>
      ${rewardLine(r.reward, rankOf[m.model])}
      <div class="stats"><span><b>${pnl(m.pnl_usd ?? 0)}</b>resultado</span><span><b>${m.trades ?? 0}</b>trades</span>
        <span><b>${isRules ? "grátis" : usd(r.ai_cost_usd ?? 0)}</b>custo IA</span></div>
      <div class="tip" role="tooltip">${tip}<div class="note" style="margin-top:6px">${esc(bubble)}</div></div>
    </div>`;
  }).join(""));
}

// ─── AI turbo: a stronger model for N cycles, then back to Haiku ───
function startBoost(btn) {
  const model = document.getElementById("boost-model").value, cycles = +document.getElementById("boost-cycles").value;
  run(btn, "Ativando…", () => api("/arena/boost", "POST", { bot: "claude-haiku-5-5", model, cycles }),
      `🚀 Bot de IA usando ${look(model).label} por ${cycles} ciclo(s); depois volta ao Haiku. Só gasta quando há oportunidade.`);
}
function renderBoost() {
  const r = (lastArena?.leaderboard || []).find((x) => x.model === "claude-haiku-5-5");
  const b = r?.boost || {};
  document.getElementById("boost-status").innerHTML = b.model && b.cycles_left >= 0 && r.model_now !== "claude-haiku-5-5"
    ? `ativo: <b>${esc(look(b.model).label)}</b> · ${b.cycles_left} de ${b.total} ciclos restantes`
    : b.model ? `agendado: <b>${esc(look(b.model).label)}</b> por ${b.total} ciclos (começa no próximo ciclo)` : "modelo atual: Haiku 5.5 (padrão)";
}

// reward: points = net profit of the last 30 days after AI cost; it buys priority and position size
const TIER_PT = { positivo: "✅ positivo", "em observação": "⚠️ em observação", "em avaliação": "⏳ em avaliação" };
function rewardLine(rw, rank) {
  if (!rw) return "";
  const medal = rw.points > 0 ? ["🥇", "🥈", "🥉"][rank - 1] || "" : "";
  return `<div class="reward ${rw.points > 0 ? "up" : rw.points < 0 ? "down" : ""}" title="1 ponto = 1 centavo de lucro líquido (30 dias, já sem o custo da IA). Mais pontos = prioridade no Coordenador e posição cheia; recorde negativo = posição mínima até recuperar.">
    ${medal} <b>${rw.points >= 0 ? "+" : ""}${rw.points} pts</b> · ${TIER_PT[rw.tier] || esc(rw.tier)}${rw.streak >= 2 ? ` · 🔥 ${rw.streak} seguidas` : ""} · posição ${usd(rw.size_usd, 0)}</div>`;
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

// ─── status bar: the few things to see at a glance, always on screen ───
function renderStatusBar(a) {
  const ks = a.kill_switch || {};
  const alert = document.getElementById("alert");
  alert.hidden = !ks.active;
  if (ks.active) document.getElementById("alert-text").textContent = `Kill switch ativo desde ${hhmm(ks.ts)} — ${(ks.reason || "").replace(/\{.*$/, "").slice(0, 140)}. Os bots não abrem novas posições até rearmar.`;
  const secs = nextCycleAt ? Math.round((nextCycleAt - Date.now()) / 1000) : null;
  const today = (a.performance?.day || {}).gain_usd;
  const items = [
    ["Conta", KIND === "sim" ? "🧪 simulação" : `💰 real · ${a.network}`],
    ["Estado", ks.active ? '<span class="red">🛑 kill switch</span>' : a.running ? (a.scanning ? '<span class="green">⚡ analisando</span>' : '<span class="green">● ligada</span>') : "⏸ parada"],
    ["Saldo", usd(KIND === "real" && a.exchange_balance !== undefined ? a.exchange_balance : a.equity)],
    ["Hoje", today == null ? "–" : pnl(today)],
    ["Posições", `${a.positions.length}`],
    ["Próxima análise", !a.running ? "–" : a.scanning ? "agora" : secs > 0 ? `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, "0")}` : "instantes"],
    ["Café", a.coffee_until ? `☕ até ${hhmm(a.coffee_until)}` : "—"],
    ["Baleias", lastWhales?.status?.connected ? '<span class="green">● ao vivo</span>' : '<span class="red">● off</span>'],
    ["Conexões", healthBad == null ? "–" : healthBad ? `<span class="red">● ${healthBad} com problema</span>` : '<span class="green">● todas ok</span>'],
  ];
  setHTML("statusbar-items", items.map(([k, v]) => `<span><small>${k}</small>${v}</span>`).join(""));
}
let healthBad = null;

// ─── live logs: every agent step, decision and cycle of every bot, like a server console ───
let logLines = [], logLast = 0, logSrc = "", logPaused = false;
function logFilter(btn) {
  logSrc = btn.dataset.s;
  document.querySelectorAll("#log-src button").forEach((b) => b.classList.toggle("on", b === btn));
  renderLogs(true);
}
function renderLogs(force) {
  if (logPaused && !force) return;
  const term = document.getElementById("term");
  const atBottom = term.scrollHeight - term.scrollTop - term.clientHeight < 40;
  const debug = document.getElementById("log-debug").checked;
  const rows = logLines.filter((l) => (debug || l.level !== "DEBUG") && (!logSrc || l.source === logSrc || l.source.startsWith(logSrc + ":"))).slice(-600);
  term.innerHTML = rows.length ? rows.map((l) => `<div class="l ${l.level}"><span class="t">${new Date(l.ts).toLocaleTimeString("pt-BR")}</span> <span class="s ${esc(l.source.split(":")[0])}">[${esc(l.source)}]</span> ${l.level.padEnd(5)} ${esc(l.msg)}</div>`).join("")
    : '<div class="l DEBUG">aguardando logs… (os bots escrevem aqui a cada passo de cada ciclo)</div>';
  if (atBottom || force) term.scrollTop = term.scrollHeight;
}
async function loadLogs() {
  const r = await api(`/logs?after=${logLast}&limit=1000`);
  if (!r.lines.length) return;
  logLast = r.last;
  logLines = logLines.concat(r.lines).slice(-2000);
  renderLogs(false);
}
safe(loadLogs);
setInterval(() => safe(loadLogs), 1500);

// ─── whales: big market orders on the coins the bots watch (live Hyperliquid trades feed) ───
let lastWhales = null;
const kusd = (v) => (Math.abs(v) >= 1e6 ? `$${(v / 1e6).toFixed(1)}M` : `$${Math.round(v / 1e3)}k`);
const WHALE_PT = { comprando: "🟢 comprando", vendendo: "🔴 vendendo", neutro: "⚖️ neutro", "sem baleias": "—" };
async function loadWhales() {
  const w = await api("/whales");
  lastWhales = w;
  const st = w.status || {};
  const tag = document.getElementById("whale-tag");
  tag.textContent = st.connected ? "ao vivo" : "desconectado";
  tag.style.color = st.connected ? "var(--green)" : "var(--red)";
  const coins = Object.entries(w.coins).sort((a, b) => (b[1]["1h"].buy_usd + b[1]["1h"].sell_usd) - (a[1]["1h"].buy_usd + a[1]["1h"].sell_usd));
  const active = coins.filter(([, s]) => s["1h"].n);
  document.getElementById("whale-head").textContent = st.connected
    ? `vigiando ${st.watching} moedas · ${active.length + w.market.length} com baleias na última hora · ao vivo desde ${hhmm(new Date(st.since * 1000).toISOString())}` : (st.error || "conectando…");
  const flowCard = ([c, s]) => {
    const h = s["1h"], tot = h.buy_usd + h.sell_usd || 1;
    return `<div class="flow-c ${s.pressure}" title="4h: compras ${kusd(s["4h"].buy_usd)} · vendas ${kusd(s["4h"].sell_usd)} · baleia ≥ ${kusd(s.threshold_usd)}">
      <b>${esc(c)}</b> ${WHALE_PT[s.pressure] || ""}
      <div class="bar2"><i class="b" style="width:${(h.buy_usd / tot * 100).toFixed(0)}%"></i><i class="s" style="width:${(h.sell_usd / tot * 100).toFixed(0)}%"></i></div>
      ${kusd(h.buy_usd)} / ${kusd(h.sell_usd)} · ${h.n} ordens</div>`;
  };
  setHTML("whale-flows", active.length ? active.slice(0, 12).map(flowCard).join("") : '<div class="empty">Ainda sem ordens de baleia nas nossas moedas desde que o feed ligou.</div>');
  setHTML("whale-market", w.market.length ? w.market.map((m) => flowCard([m.coin, m])).join("") : '<div class="empty">Sem baleias no resto do mercado na última hora.</div>');
  table("whale-recent", ["Quando", "Moeda", "Lado", "Valor", "Carteira"], w.recent.map((t) => `<tr>
    <td>${hhmm(new Date(t.ts * 1000).toISOString())}</td><td><b>${esc(t.coin)}</b></td>
    <td class="${t.side === "compra" ? "green" : "red"}">${t.side}</td><td>${kusd(t.usd)}</td>
    <td class="muted">${t.wallet ? `<a href="https://hypurrscan.io/address/${esc(t.wallet)}" target="_blank" rel="noopener">${short(t.wallet)}</a>` : "–"}</td></tr>`));
  if (acc) renderRadar(acc);
}
safe(loadWhales);
setInterval(() => safe(loadWhales), 10000);

// ─── charts (TradingView lightweight-charts): candles + EMAs + entry/stop/target + the bots' buy levels ───
let chart = null, candleS = null, ema50S = null, ema200S = null, priceLines = [], eqChart = null, eqS = null, btcS = null;
const CHART_OPTS = { layout: { background: { color: "transparent" }, textColor: "#8a9690" },
  grid: { vertLines: { color: "rgba(255,255,255,.04)" }, horzLines: { color: "rgba(255,255,255,.04)" } },
  rightPriceScale: { borderColor: "#222a26" }, timeScale: { borderColor: "#222a26", timeVisible: true }, autoSize: true };
const emaSeries = (vals, n) => { const k = 2 / (n + 1); let e = vals[0].value; return vals.map((v) => ({ time: v.time, value: (e = v.value * k + e * (1 - k)) })); };
function showChart(coin) {
  document.getElementById("chart-coin").value = coin;
  safe(loadChart);
  document.getElementById("grafico").scrollIntoView({ behavior: "smooth", block: "start" });
}
async function loadChart() {
  if (!window.LightweightCharts || !acc) return;
  const sel = document.getElementById("chart-coin");
  if (!sel.options.length) {
    sel.innerHTML = acc.limits.markets.map((m) => `<option>${esc(m)}</option>`).join("");
    sel.value = acc.positions[0]?.symbol || acc.market_temp?.watch?.[0]?.symbol || "BTC";
  }
  const coin = sel.value;
  const { candles } = await api(`/candles?coin=${encodeURIComponent(coin)}`);
  if (!chart) {
    chart = LightweightCharts.createChart(document.getElementById("chart"), CHART_OPTS);
    candleS = chart.addCandlestickSeries({ upColor: "#34d399", downColor: "#f87171", borderVisible: false, wickUpColor: "#34d399", wickDownColor: "#f87171" });
    ema50S = chart.addLineSeries({ color: "#7dd3fc", lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
    ema200S = chart.addLineSeries({ color: "#fbbf24", lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
  }
  const data = candles.map((c) => ({ time: c.t, open: c.open, high: c.high, low: c.low, close: c.close }));
  candleS.setData(data);
  const closes = data.map((d) => ({ time: d.time, value: d.close }));
  ema50S.setData(emaSeries(closes, 50).slice(50));
  ema200S.setData(emaSeries(closes, 200).slice(150));
  priceLines.forEach((l) => candleS.removePriceLine(l));
  priceLines = [];
  const line = (price, color, title, style = 0) => price && priceLines.push(candleS.createPriceLine({ price, color, lineWidth: 1, lineStyle: style, axisLabelVisible: true, title }));
  const pos = acc.positions.find((p) => p.symbol === coin);
  if (pos) { line(pos.entry_price, "#e8eeeb", "entrada"); line(pos.stop_price, "#f87171", "stop"); line(pos.tp_price, "#34d399", "alvo"); }
  const r = (acc.market_temp?.watch || []).find((x) => x.symbol === coin);
  if (r && !pos) { if (r.up) line(r.dip_at, "#a3b1ab", "Quedas compra abaixo", 2); if (r.strong) line(r.break_at, "#67e8f9", "Tendência compra acima", 2); }
  document.getElementById("chart-legend").innerHTML = `<span><span style="color:#7dd3fc">━</span> EMA50</span><span><span style="color:#fbbf24">━</span> EMA200 (preço acima = tendência de alta)</span>` +
    (pos ? `<span>posição aberta: entrada ${fmtPx(pos.entry_price)} · stop ${fmtPx(pos.stop_price)} · alvo ${fmtPx(pos.tp_price)}</span>`
         : r ? `<span>linhas tracejadas = onde os bots comprariam · ${esc(STATUS_PT[r.status] || r.status)}</span>` : "");
}
async function loadEquityChart() {
  if (!window.LightweightCharts) return;
  const { rows } = await api(`/account/equity?kind=${KIND}`);
  if (!rows.length) return;
  if (!eqChart) {
    eqChart = LightweightCharts.createChart(document.getElementById("equity-chart"), CHART_OPTS);
    eqS = eqChart.addLineSeries({ color: "#34d399", lineWidth: 2, priceLineVisible: false });
    btcS = eqChart.addLineSeries({ color: "#fbbf24", lineWidth: 1, priceLineVisible: false, lastValueVisible: false });
  }
  const pts = rows.map((r) => ({ ...r, time: Math.floor(new Date(r.ts) / 1000) })).filter((r, i, a) => !i || r.time > a[i - 1].time);
  const b0 = pts.find((r) => r.btc)?.btc, e0 = pts[0].equity;
  eqS.setData(pts.map((r) => ({ time: r.time, value: r.equity })));
  btcS.setData(b0 ? pts.filter((r) => r.btc).map((r) => ({ time: r.time, value: e0 * r.btc / b0 })) : []);
}
setTimeout(() => { safe(loadChart); safe(loadEquityChart); }, 2500);
setInterval(() => safe(loadChart), 60000);
setInterval(() => safe(loadEquityChart), 60000);

// ─── news: crypto outlets, tagged by coin ───
async function loadNews() {
  const sel = document.getElementById("news-coin");
  if (sel.options.length === 1 && acc) sel.innerHTML += acc.limits.markets.map((m) => `<option>${esc(m)}</option>`).join("");
  const r = await api(`/news${sel.value ? `?coin=${encodeURIComponent(sel.value)}` : ""}`);
  const tag = document.getElementById("news-tag");
  tag.textContent = r.status.ok ? `${r.items.length} nas últimas 24h` : "sem dados";
  setHTML("news", r.items.length ? r.items.map((i) => `<div class="ni"><span class="w">há ${ago(new Date(i.ts * 1000).toISOString())}</span>
    <span><a href="${esc(i.url)}" target="_blank" rel="noopener">${esc(i.title)}</a>${i.coins.map((c) => `<span class="coin-tag">${esc(c)}</span>`).join("")}
    <br><span class="src">${esc(i.source)}</span></span></div>`).join("") : '<div class="empty">Sem notícias para este filtro nas últimas 24h.</div>');
}
setTimeout(() => safe(loadNews), 1500);
setInterval(() => safe(loadNews), 120000);

// ─── connections: exchange, data, AI and RPC providers ───
async function loadHealth() {
  const h = await api("/health");
  const bad = h.checks.filter((c) => c.ok === false).length;
  healthBad = bad;
  const tag = document.getElementById("conn-tag");
  tag.textContent = bad ? `${bad} com problema` : "tudo conectado";
  tag.style.color = bad ? "var(--red)" : "var(--green)";
  document.getElementById("conns").innerHTML = h.checks.map((c) => `<div class="conn ${c.ok === false ? "bad" : ""}">
    ${c.ok ? "🟢" : c.ok === false ? "🔴" : "⚪"} <b>${esc(c.name)}</b> <span>· ${c.ms} ms</span>
    <div class="d">${esc(c.detail)}</div><div>${esc(c.role)}</div></div>`).join("") +
    `<div class="conn">🟢 <b>CoinMarketCap · TradingView</b> <span>· MCP via Claude</span><div class="d">contexto de mercado nas rotinas das 11h e 22h</div><div>medo/ganância, dominância, notícias, análise técnica</div></div>`;
}
safe(loadHealth);
setInterval(() => safe(loadHealth), 60000);

