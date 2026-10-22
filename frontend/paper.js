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
// coin icons (CoinMarketCap static images by id); unknown coins get a letter badge
const CMC_ID = { BTC: 1, ETH: 1027, XRP: 52, BNB: 1839, SOL: 5426, DOGE: 74, ADA: 2010, TRX: 1958, HBAR: 4642, LINK: 1975,
  AVAX: 5805, XLM: 512, SUI: 20947, BCH: 1831, LTC: 2, DOT: 6636, UNI: 7083, NEAR: 6535, AAVE: 7278, ZEC: 1437, XMR: 328,
  HYPE: 32196, TAO: 22974, ONDO: 21159, ENA: 30171, WLD: 13502, ARB: 11841, ZRO: 26997, kPEPE: 24478, PEPE: 24478,
  USDC: 3408, OP: 11840, APT: 21794, INJ: 7226, SEI: 23149, TIA: 22861, JUP: 29210, FIL: 2280, ATOM: 3794, ETC: 1321, ICP: 8916,
  POL: 28321, RENDER: 5690, STX: 4847, CRV: 6538, PUMP: 36507, FARTCOIN: 33597, TRUMP: 35336, WIF: 28752, BONK: 23095,
  PENGU: 34466, VIRTUAL: 29420, KAITO: 35509, BERA: 24647, S: 32684, MKR: 1518, LDO: 8000, PENDLE: 9481, JTO: 28541 };
const coinIcon = (sym) => {
  const id = CMC_ID[sym];
  return id ? `<img class="ci" src="https://s2.coinmarketcap.com/static/img/coins/64x64/${id}.png" alt="" loading="lazy" onerror="this.replaceWith(Object.assign(document.createElement('span'),{className:'ci ci-l',textContent:'${esc(String(sym)[0] || "?")}'}))">`
            : `<span class="ci ci-l">${esc(String(sym || "?").replace(/^k/, "")[0] || "?")}</span>`;
};
const coin = (sym) => `<span class="coin">${coinIcon(sym)}<b>${esc(sym)}</b></span>`;
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
const pct0 = (v) => (v === null || v === undefined ? "–" : `${Math.round(v * 100)}%`);
const pct = (v) => (v === null || v === undefined ? "–" : `${(v * 100).toFixed(2)}%`);
// every time on the dashboard is Lisbon time (where the user is), with the date when it is not today
const TZ = "Europe/Lisbon";
const lisDay = (d) => d.toLocaleDateString("pt-BR", { timeZone: TZ });
const time = (iso) => (iso ? new Date(iso).toLocaleString("pt-BR", { timeZone: TZ, day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "–");
const short = (m) => (m ? `${m.slice(0, 4)}…${m.slice(-4)}` : "");
const ago = (iso) => {
  const s = Math.max(0, (Date.now() - new Date(iso)) / 1000);
  return s < 60 ? `${Math.round(s)}s` : s < 3600 ? `${Math.round(s / 60)} min` : `${Math.round(s / 3600)} h`;
};
function table(id, head, rows) {
  const el = document.getElementById(id);
  if (!el) return;
  el.innerHTML =
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
let KIND = "real";
try { KIND = localStorage.getItem("acct") || "real"; } catch (e) { /* storage blocked */ }

// Lite / Pro: phones start in Lite; the choice is remembered per device
function setView(v) {
  document.body.classList.toggle("lite", v === "lite");
  document.querySelectorAll("#view-seg button").forEach((b) => b.classList.toggle("on", b.dataset.v === v));
  try { localStorage.setItem("view", v); } catch (e) { /* ignore */ }
}
let VIEW = matchMedia("(max-width: 700px)").matches ? "lite" : "pro";
try { VIEW = localStorage.getItem("view") || VIEW; } catch (e) { /* storage blocked */ }
// opened from the internet (tunnel/Vercel): the server allows reading only, so hide the controls
const REMOTE = !["127.0.0.1", "localhost", "[::1]"].includes(location.hostname);
document.addEventListener("DOMContentLoaded", () => {
  setView(VIEW);
  document.body.classList.toggle("ro", REMOTE);
  if (REMOTE) document.getElementById("logout").hidden = false;
});
let acc = null;

function setKind(k) {
  KIND = k;
  setTimeout(() => { safe(loadEquityChart); safe(loadHistory); }, 800);
  setTimeout(() => safe(loadDaily), 0);
  try { localStorage.setItem("acct", k); } catch (e) { /* ignore */ }
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
      (r) => `☕ Titulares com cafeína até ${hhmm(r.coffee_until)} nas contas simulada e real — sem pausas, atentos a cada sinal`);
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
const hhmm = (iso) => {
  if (!iso) return "–";
  const d = new Date(iso), now = new Date();
  const hm = d.toLocaleTimeString("pt-BR", { timeZone: TZ, hour: "2-digit", minute: "2-digit" });
  if (lisDay(d) === lisDay(now)) return hm;
  if (lisDay(d) === lisDay(new Date(now.getTime() + 864e5))) return `amanhã ${hm}`;
  if (lisDay(d) === lisDay(new Date(now.getTime() - 864e5))) return `ontem ${hm}`;
  return `${d.toLocaleDateString("pt-BR", { timeZone: TZ, day: "2-digit", month: "2-digit" })} ${hm}`;
};

// ─── main refresh ───
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
  renderWallets();
  // KPIs
  document.getElementById("k-eq-l").textContent = KIND === "sim" ? "Saldo simulado" : "Saldo real na Hyperliquid";
  document.getElementById("k-eq").textContent = usd(KIND === "real" && a.exchange_balance !== undefined ? a.exchange_balance : a.equity);
  const avail = availableOf(a);
  document.getElementById("k-eq-s").innerHTML = `<b class="green">${usd(avail.available)} disponível</b> · ${usd(avail.margin)} em margem · <a href="#fundos">➕ fundos</a><br>`
    + `${usd(a.exposure)} em posições (${a.positions.length}) · pode abrir mais ${usd(a.buying_power, 0)} a ${a.limits.leverage}x`;
  const P = a.profit || {};
  document.getElementById("k-pnl").innerHTML = pnl(netOf(P, "all"));
  const all = a.performance?.all;
  document.getElementById("k-pnl-s").textContent = `trades ${usd(P.trading_all)} · IA −${usd(P.ai_all)}${all?.hold_btc != null ? ` · segurar BTC ${pct(all.hold_btc)}` : ""}`;
  renderProfit(P);
  const ps = a.positions, cost = ps.reduce((s, x) => s + x.cost_usd, 0), now = ps.reduce((s, x) => s + (x.mark_value ?? x.cost_usd), 0);
  const kp = document.getElementById("k-pos");
  kp.textContent = ps.length ? `${ps.length} ${ps.length === 1 ? "posição" : "posições"}` : "Nenhuma";
  kp.classList.toggle("soft", !ps.length);
  document.getElementById("k-pos-s").innerHTML = ps.length ? `${ps.map((x) => esc(x.symbol)).join(", ")} · ${usd(cost)} → ${usd(now)} ${pnl(now - cost)}` : "esperando uma oportunidade";
  const kt = document.getElementById("k-tr");
  kt.textContent = a.trades.length ? `${a.trades.length} · ${pct0(a.win_rate)}` : "Nenhum";
  kt.classList.toggle("soft", !a.trades.length);
  document.getElementById("k-tr-s").innerHTML = a.trades.length ? `total ${pnl(a.trades.reduce((x, t) => x + t.pnl_usd, 0))}` : "sem trades fechados";
  // scan bar + room
  nextCycleAt = a.running && a.heartbeat ? new Date(a.heartbeat.ts).getTime() + a.cycle_interval_sec * 1000 : null;
  const scanning = a.scanning || (a.live?.running && Date.now() - new Date(a.live.ts) < 60000);
  document.getElementById("scanbar").hidden = !scanning;
  if (scanning) document.getElementById("scan-text").textContent = `Analisando o mercado (ciclo #${a.cycle})…`;
  renderTeam();
  renderThermo(a);
  renderPositions(a);
  fillManualCoins(a);
  renderGoals(a);
  renderRadar(a);
  // trades
  document.getElementById("trades-total").innerHTML = a.trades.length ? pnl(a.trades.reduce((x, t) => x + t.pnl_usd, 0)) : "";
  table("trades", ["Ativo", "Entrada → saída", "Lucro", "Motivo"], a.trades.map((t) => `
    <tr><td>${coin(t.symbol)}<br><span class="muted">${time(t.exit_ts)}</span></td>
    <td>${Number(t.entry_price).toPrecision(6)} → ${Number(t.exit_price).toPrecision(6)}</td>
    <td>${pnl(t.pnl_usd)}<br><span class="muted">${pct(t.pnl_pct)}</span></td><td class="muted">${esc(t.reason)}</td></tr>`));
  const L = a.limits;
  document.getElementById("limits").textContent = `Limites (iguais na simulação e no real): capital ${usd(L.max_capital_usd, 0)}, posição ${usd(L.max_position_usd, 0)}, ${L.max_positions || "auto"} de cada vez, ${L.leverage}x isolada só compra, perda diária ${pct(L.max_daily_loss_pct)}, mercados ${L.markets.join(", ")}.`;
}

// ─── open positions: how each trade is doing between its stop and its target ───
const fmtPx = (v) => (v == null ? "–" : `$${Number(v) >= 100 ? Number(v).toFixed(2) : Number(v).toPrecision(4)}`);
const STRAT_MODEL = { trend: "trend", trend4h: "trend4h", whales: "whales", copy: "copy", short: "short", rules: "rules", mean_reversion_atr: "rules",
  trend4h_15m: "trend4h_15m", whales_15m: "whales_15m", copy_15m: "copy_15m" };
const botName = (s) => (s === "manual" ? "Manual" : look(s?.startsWith("llm:") ? s.slice(4) : STRAT_MODEL[s] || "rules").label);
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
    return `<div class="pos ${pl >= 0 ? "win" : "lose"} ${openMint === p.mint ? "sel" : ""}" tabindex="0" role="button"
      aria-label="Detalhes de ${esc(p.symbol)}" onclick="openPos('${esc(p.mint)}')" onkeydown="if(event.key==='Enter')openPos('${esc(p.mint)}')">
      <div class="top"><span class="sym">${coin(p.symbol)}</span><span class="pl">${pnl(pl)} <small class="muted">${(plPct * 100).toFixed(2)}%</small></span></div>
      <div class="sub"><span>bot ${esc(botName(p.strategy))} · aberta há ${ago(p.opened_at)}</span><span>${usd(p.cost_usd)}</span></div>
      <div class="track" title="entre o stop e o alvo"><span class="entry" style="left:${at(p.entry_price)}"></span><span class="now" style="left:${at(now)}"></span></div>
      <div class="ends"><span>🛑 stop ${fmtPx(p.stop_price)}</span><span>entrada ${fmtPx(p.entry_price)} · agora ${fmtPx(now)}</span><span>🎯 ${fmtPx(p.tp_price)}</span></div>
    </div>`;
  }).join("") : `<div class="empty">Nenhuma posição aberta agora — os bots estão procurando a próxima entrada (veja o radar abaixo).</div>`);
}

// will this coin become a trade? Real-account rules applied to what the radar sees right now
function radarVerdict(r) {
  const a = acc, held = a.positions.some((p) => p.symbol === r.symbol);
  const slots = (a.limits.max_positions || 3) - a.positions.length;
  const whale = lastWhales?.coins?.[r.symbol]?.pressure;
  if (held) return `📦 já em carteira`;
  if (a.kill_switch?.active) return "🛑 não: kill switch";
  if (a.goals?.hit && a.goals?.rest) return "😴 não hoje: meta batida";
  if (r.status === "pronto") return slots > 0 ? `✅ <b>sim</b>, no próximo ciclo (${esc(r.ready_by === "quedas" ? "São Marcos" : "Endrick")})${whale === "vendendo" ? " · ⚠️ baleias vendendo" : ""}`
                                              : `⏸ pronto, mas sem vaga (${a.positions.length} posições)`;
  if (r.status === "perto") return `👀 se ${r.up && r.dip_pct > -1.5 ? `cair até ${fmtPx(r.dip_at)}` : `romper ${fmtPx(r.break_at)}`}${whale === "comprando" ? " · 🐋 baleias comprando" : ""}`;
  if (r.status === "aguardando") return `⏳ ainda não (longe da linha)`;
  return `🚫 não: em baixa`;
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
         pctOf(g.trades.done, g.trades.target), `${g.trades.closed} fechados · ${g.trades.wins} com lucro · sem limite diário de trades: compra sempre que houver sinal e vaga`),
    card(g.analyses.done >= g.analyses.target_so_far, "🔍 Análises (ciclos) hoje", `${g.analyses.done} <small>de ${g.analyses.target_so_far} até agora</small>`,
         pctOf(g.analyses.done, g.analyses.target_so_far), "1 ciclo a cada 5 min, 30 moedas por ciclo"),
  ].join(""));
}

// ─── manual buy: the user's own decision, executed with the same protections as the bots ───
function manualBuy(btn) {
  const coin = document.getElementById("mb-coin").value, usd = +document.getElementById("mb-usd").value;
  if (KIND === "real" && !confirm(`Comprar ${coin} por ~$${usd.toFixed(2)} com DINHEIRO REAL (alavancagem ${acc.limits.leverage}x isolada)?\n\nO sistema coloca stop (2 ATR) e alvo (3 ATR) na Hyperliquid. Perda máxima ≈ o stop.`)) return;
  run(btn, "Comprando…", () => api("/account/manual-buy", "POST", { kind: KIND, coin, usd }),
      (r) => `🛒 Comprou ${r.symbol} por $${r.usd.toFixed(2)} a ${fmtPx(r.price)} · stop ${fmtPx(r.stop)} · alvo ${fmtPx(r.tp)}`);
}
function fillManualCoins(a) {
  const sel = document.getElementById("mb-coin");
  if (!sel.options.length) { sel.innerHTML = a.limits.markets.map((m) => `<option>${esc(m)}</option>`).join(""); sel.value = "BTC"; }
}

// ─── one open position: details and the user's own sell ───
let openMint = null;
async function openPos(mint) {
  openMint = mint;
  const box = document.getElementById("pos-detail");
  let p;
  try { p = await api(`/account/position?kind=${KIND}&mint=${encodeURIComponent(mint)}`); }
  catch (e) { box.hidden = true; openMint = null; return; }
  const now = p.value / p.qty, held = p.held_h < 1 ? `${Math.round(p.held_h * 60)} min` : `${p.held_h.toFixed(1)} h`;
  const row = (k, v) => `<div><span>${k}</span><b>${v}</b></div>`;
  box.innerHTML = `<div class="posd-head">${coin(p.symbol)} <span class="muted">· bot ${esc(botName(p.strategy))} · ${KIND === "real" ? "💰 conta real" : "🧪 simulação"}</span>
      <button class="x" onclick="closePos()" aria-label="Fechar detalhes">✕</button></div>
    <div class="posd-grid">
      ${row("Comprada em", time(p.opened_at))}${row("Ativa há", held)}
      ${row("Valor investido", usd(p.cost_usd))}${row("Valor agora", usd(p.value))}
      ${row("Resultado", `${pnl(p.pnl_usd)} (${p.pnl_pct >= 0 ? "+" : ""}${(p.pnl_pct * 100).toFixed(2)}%)`)}${row("Preço entrada → agora", `${fmtPx(p.entry_price)} → ${fmtPx(now)}`)}
      ${row("🛑 Stop", fmtPx(p.stop_price))}${row("🎯 Alvo", fmtPx(p.tp_price))}
      ${row("Quantidade", `${Number(p.qty).toPrecision(6)} ${esc(p.symbol)}`)}${row("Funding pago", usd(p.funding_usd, 4))}
    </div>
    ${p.entry_reason ? `<div class="note" style="margin-top:8px">Por que comprou: ${esc(p.entry_reason)}</div>` : ""}
    <div class="posd-actions"><button class="sell" onclick="sellPos('${esc(mint)}', this)">🔴 Vender agora (preço de mercado)</button></div>`;
  box.hidden = false;
  if (acc) renderPositions(acc);
}
function closePos() { openMint = null; document.getElementById("pos-detail").hidden = true; if (acc) renderPositions(acc); }
function sellPos(mint, btn) {
  const p = acc.positions.find((x) => x.mint === mint);
  const res = p ? (p.mark_value ?? p.cost_usd) - p.cost_usd : 0;
  if (!confirm(`Vender ${p?.symbol || mint} agora a preço de mercado${KIND === "real" ? " com DINHEIRO REAL" : " (simulação)"}?\n\nResultado atual ≈ ${res >= 0 ? "+" : "−"}$${Math.abs(res).toFixed(2)}. O stop e o alvo na corretora são cancelados.`)) return;
  run(btn, "Vendendo… (os bots pausam a análise, alguns segundos)", () => api("/account/close", "POST", { kind: KIND, mint }),
      (r) => { closePos(); return `✓ Vendeu ${r.symbol} a ${fmtPx(r.price)} · resultado ${r.pnl_usd >= 0 ? "+" : "−"}$${Math.abs(r.pnl_usd).toFixed(2)}`; });
}
setInterval(() => { if (openMint) safe(() => openPos(openMint)); }, 15000);

// ─── radar: every coin the bots watch, and the exact level that would trigger a buy ───
let radarAll = false;
const STATUS_PT = { pronto: "✅ pronto", perto: "👀 perto", aguardando: "⏳ aguardando", "sem tendência": "⬇ sem tendência" };
function renderRadar(a) {
  const w = a.market_temp?.watch || [];
  document.getElementById("radar-all").textContent = radarAll ? "Ver só as melhores" : `Ver todas (${w.length})`;
  const near = w.filter((r) => r.status === "pronto" || r.status === "perto").length;
  document.getElementById("radar-head").textContent = a.market_temp?.ts ? `${near} perto de compra · atualizado há ${ago(a.market_temp.ts)}` : "aguardando a primeira análise";
  const u = lastArena?.universe;
  const inUni = (s) => !u?.coins?.length || u.coins.includes(s) || (u.focus || []).includes(s.toUpperCase().replace(/^K/, ""));
  document.getElementById("radar-universe").textContent = u?.coins?.length
    ? `🎯 Universo desta hora (as ${u.size} moedas com mais volume × volatilidade): ${u.coins.join(", ")} · os bots só entram nelas, nas moedas-foco (${(u.focus || []).join(", ")}) e nas que já têm posição`
    : "";
  const rows = (radarAll ? w : w.slice(0, 8)).map((r) => `<tr${inUni(r.symbol) ? "" : ' style="opacity:.55"'}>
    <td>${coin(r.symbol)}<br><span class="muted">${r.ch24 >= 0 ? "+" : ""}${r.ch24}% 24h</span></td>
    <td>${fmtPx(r.price)}</td>
    <td>${r.strong ? "⬆⬆ forte" : r.up ? "⬆ alta" : "⬇ baixa"}<br><span class="muted">RSI ${r.rsi}</span></td>
    <td>${r.up ? `abaixo de <b>${fmtPx(r.dip_at)}</b><br><span class="muted">${r.dip_pct}% de distância${r.rsi >= 30 ? " · RSI precisa cair" : ""}</span>` : '<span class="muted">só em alta</span>'}</td>
    <td>${r.strong ? `acima de <b>${fmtPx(r.break_at)}</b><br><span class="muted">+${Math.max(r.break_pct, 0).toFixed(2)}% de distância</span>` : '<span class="muted">precisa de tendência forte</span>'}</td>
    <td>${(() => { const s = lastWhales?.coins?.[r.symbol]; return s && s["1h"].n ? `${WHALE_PT[s.pressure]}<br><span class="muted">${kusd(s["1h"].net_usd)} líquido 1h</span>` : '<span class="muted">—</span>'; })()}</td>
    <td>${(() => { const c = lastTop?.consensus?.[r.symbol]; return c && (c.long || c.short) ? `${c.long ? `<span class="green">▲ ${c.long}</span>` : ""} ${c.short ? `<span class="red">▼ ${c.short}</span>` : ""}${c.new_longs_1h ? `<br><span class="muted">${c.new_longs_1h} entraram 1h</span>` : ""}` : '<span class="muted">—</span>'; })()}</td>
    <td>${radarVerdict(r)}</td>
    <td><span class="st-chip ${r.status === "pronto" ? "pronto" : r.status === "perto" ? "perto" : ""}">${STATUS_PT[r.status] || esc(r.status)}</span>${inUni(r.symbol) ? "" : '<br><span class="muted">fora do universo da hora</span>'}${r.ready_by ? `<br><span class="muted">sinal do bot ${esc(r.ready_by === "quedas" ? "São Marcos" : "Endrick")}</span>` : ""}</td></tr>`);
  table("radar-table", ["Moeda", "Preço", "Tendência", "📐 Compra na queda", "📈 Compra no rompimento", "🐋 Baleias 1h", "👑 Top 10", "Vai comprar?", "Status"], rows);
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

// ─── advanced actions (real account) ───
function flatten(btn) {
  if (!confirm("Fechar TODAS as posições reais a preço de mercado e desligar os bots da conta real?")) return;
  run(btn, "Fechando…", () => api("/live/flatten", "POST"),
      (r) => (r.errors.length ? `Fechadas: ${r.closed.join(", ") || "nenhuma"} · erros: ${r.errors.join("; ")}` : `✓ Fechadas: ${r.closed.join(", ") || "nenhuma posição aberta"}`));
}
function killReset(btn) {
  if (KIND === "real" && !confirm("Rearmar o kill switch da conta REAL? Os bots voltam a poder abrir posições com dinheiro real.")) return;
  run(btn, "Rearmando…", () => api("/account/kill-switch/reset", "POST", { kind: KIND }), "✓ Kill switch rearmado — os bots voltam a operar");
}

let teamFilterBy = "";
function teamFilter(btn) {
  teamFilterBy = btn.dataset.f;
  document.querySelectorAll("#team-filter button").forEach((b) => b.classList.toggle("on", b === btn));
  renderTeam();
}

// ─── team of bots (one shared wallet) + lab results ───
const MODEL_LOOK = {
  // the team is named after Palmeiras players
  "rules": { label: "São Marcos", color: "#a3b1ab", ico: "🧤", role: "paciente: compra quedas numa alta" },
  "trend": { label: "Endrick", color: "#67e8f9", ico: "⚡", role: "rompimentos rápidos, day trade de 24h" },
  "trend4h": { label: "Gabriel Jesus", color: "#fbbf24", ico: "🎯", role: "tendências maiores, velas de 4h" },
  "whales": { label: "Evair", color: "#60a5fa", ico: "🐋", role: "segue as baleias quando compram forte" },
  "meme_grad": { label: "Breno Lopes", color: "#a3e635", ico: "🎓", role: "trincheira: tokens recém-formados na pump.fun" },
  "meme_momo": { label: "Deyverson", color: "#34d399", ico: "🐸", role: "trincheira: momentum de meme coins" },
  "meme_whale": { label: "Rony", color: "#22d3ee", ico: "🦈", role: "trincheira: segue as baleias das meme coins" },
  "whales2": { label: "Dudu", color: "#38bdf8", ico: "🐳", role: "clone do Evair: mesmas regras, segue as baleias" },
  "copy": { label: "Veiga", color: "#f472b6", ico: "👑", role: "copia os 10 melhores traders da Hyperliquid" },
  "short": { label: "Gustavo Gómez", color: "#f87171", ico: "📉", role: "zagueiro do short: lucra quando o preço cai (laboratório)" },
  "trend4h_15m": { label: "Gabriel Jesus 15m", color: "#fbbf24", ico: "⏱", role: "trades rápidos: tendências em velas de 15 min" },
  "whales_15m": { label: "Evair 15m", color: "#60a5fa", ico: "⏱", role: "trades rápidos: segue baleias em velas de 15 min" },
  "copy_15m": { label: "Veiga 15m", color: "#f472b6", ico: "⏱", role: "trades rápidos: copia top traders em velas de 15 min" },
  "claude-fable-5-1": { label: "Fable 5.1", color: "#f5c451", ico: "✨" },
  "claude-opus-5-5": { label: "Opus 5.5", color: "#fb923c", ico: "🎼" },
  "claude-opus-5": { label: "Opus 5", color: "#f97362", ico: "🎻" },
  "claude-sonnet-5-5": { label: "Sonnet 5.5", color: "#60a5fa", ico: "📜" },
  "claude-haiku-5-5": { label: "Valdivia", color: "#34d399", ico: "🧠", role: "o cérebro: IA (Haiku) lê indicadores, baleias e notícias" },
};
const ACTION_PT = { BUY: "🟢 comprar", HOLD: "⏸ esperar", SELL: "🔵 vender", sleep: "😴 dormindo", wake: "⏰ acordou",
                    entry: "🟢 comprou", exit: "🔵 vendeu", veto: "🟡 bloqueado" };
const FREE = new Set(["rules", "trend", "trend4h", "whales", "copy", "short", "trend4h_15m", "whales_15m", "copy_15m"]);   // deterministic bots: no AI cost
const look = (m) => {
  let lin = null;
  try { lin = lastArena?.leaderboard?.find((r) => r.model === m)?.lineage; } catch (e) { /* arena not loaded yet */ }
  if (lin && !MODEL_LOOK[m]) {                  // evolved reserve: champion name, its family's look
    const b = MODEL_LOOK[lin.base] || {};
    return { ...b, label: lin.label, ico: b.ico || "🧬", role: `${b.role || "bot"} · geração ${lin.gen}` };
  }
  const mb = !MODEL_LOOK[m] && memeBots[m];     // evolved meme bot: champion name, its family's look
  if (mb) return { ...(MODEL_LOOK[mb.base] || {}), label: mb.name, role: `${mb.role} · geração ${mb.gen}` };
  return { role: "IA analisa e decide", ...(MODEL_LOOK[m] || { label: m, color: "#9fe8c7", ico: "🤖" }) };
};
let lastArena = null;
let memeBots = {};                               // meme team roster (from /meme), for names of evolved meme bots

async function loadArena() {
  const a = await api("/arena");
  lastArena = a;
  renderTeam();
  renderBoost();
  renderRanking();
  renderChat(false);          // names for @mentions come with the arena
}

// ─── ranking: the bots compete in the lab (same rules, same fake money) ───
let RANK = "net";
function rankBy(k) {
  RANK = k;
  document.querySelectorAll("#rank-sort button").forEach((b) => b.classList.toggle("on", b.dataset.r === k));
  renderRanking();
}
let RANK_WHO = "all";
function rankWho(w) {
  RANK_WHO = w;
  document.querySelectorAll("#rank-who button").forEach((b) => b.classList.toggle("on", b.dataset.w === w));
  renderRanking();
}
const isTitular = (r) => !!(r.green_light?.trusted || r.green_light?.ok);
function renderRanking() {
  const rows = (lastArena?.leaderboard || []).filter((r) => RANK_WHO === "all" || (RANK_WHO === "tit") === isTitular(r)).map((r) => ({ ...r, wins: Math.round((r.trades || 0) * (r.win_rate || 0)), net: r.net_usd ?? r.pnl_usd ?? 0 }));
  rows.sort((a, b) => RANK === "wins" ? (b.wins - a.wins) || (b.net - a.net) : (b.net - a.net) || (b.wins - a.wins));
  const medal = (i) => ["🥇", "🥈", "🥉"][i] || `${i + 1}º`;
  setHTML("rank-table", `<tr><th></th><th>Bot</th><th>Lucro</th><th>Lucrativos</th><th>Trades</th><th>Acerto</th></tr>` +
    rows.map((r, i) => { const L = look(r.model); return `<tr class="${i === 0 ? "me1" : ""}"><td class="pos">${medal(i)}</td>
      <td>${esc(L.ico || "")} <b>${esc(L.label)}</b>${isTitular(r) ? " ⚽" : " 🪑"}${r.turbo_until && new Date(r.turbo_until) > new Date() ? " ⚡" : ""}</td>
      <td>${pnl(r.net)}</td><td>${r.wins}</td><td>${r.trades || 0}</td><td>${r.trades ? pct(r.win_rate) : "–"}</td></tr>`; }).join(""));
}

function renderTeam() {
  if (!acc || !lastArena) return;
  const lab = Object.fromEntries(lastArena.leaderboard.map((r) => [r.model, r]));
  const team = acc.team && acc.team.length ? acc.team : [];
  // lab-only bots (e.g. Estêvão, short) are not in the account's team but are shown with their lab record
  const members = [...team, ...lastArena.leaderboard.filter((r) => !team.some((m) => m.model === r.model)).map((r) => ({ model: r.model, name: r.model }))];
  const liveName = acc.live?.running && Date.now() - new Date(acc.live.ts) < 30000 ? acc.live.agent : null;
  const awake = members.filter((m) => lab[m.model]?.running && !lab[m.model]?.needs_key).length;
  const isTitular = (m) => !!lab[m.model]?.green_light?.ok;
  const nTit = members.filter(isTitular).length;
  document.getElementById("team-head").innerHTML = acc.running
    ? `⚽ <b>${nTit}</b> titulares · 🪑 <b>${members.length - nTit}</b> na reserva`
    : "desligados — clique em ▶ Start";
  const coffee = acc.running && acc.coffee_until;
  const cb = document.getElementById("btn-coffee");
  cb.classList.toggle("on", !!coffee);
  cb.textContent = coffee ? `☕ até ${hhmm(coffee)}` : "☕ Café";
  const rankOf = Object.fromEntries([...members].sort((x, y) => ((lab[y.model]?.reward?.points) ?? 0) - ((lab[x.model]?.reward?.points) ?? 0)).map((m, i) => [m.model, i + 1]));
  const shown = members.filter((m) => !teamFilterBy || (teamFilterBy === "titular") === isTitular(m));
  const card = (m) => {
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
    return `<div class="tb ${cls} ${isTitular(m) ? "tit" : "res"}" tabindex="0" style="--c:${L.color}" aria-label="${esc(L.label)}: ${esc(state)}"
      onclick="openBot('${esc(m.model)}')" onkeydown="if (event.key === 'Enter') openBot('${esc(m.model)}')" title="clique para ver tudo sobre o bot">
      <span class="shirt">${isTitular(m) ? "⚽ TITULAR" : "🪑 RESERVA"}</span>
      ${coffee && cls !== "off" ? '<span class="cup" title="com cafeína: sem pausas">☕</span>' : ""}
      <div class="head">${botFace(L.color, L.ico)}
        <div><div class="nm">${esc(L.label)}${r.model_now && r.model_now !== m.model ? ` <small class="green">🚀 ${esc(look(r.model_now).label)}</small>` : reviewOn(r) ? ` <small class="green">🚀 ${esc(look(reviewOn(r).model).label)}</small>` : ""}</div><div class="rl">${esc(L.role)}</div></div></div>
      <span class="chip">${esc(chip)}</span>
      <div class="now">Última decisão: ${now}</div>
      ${rewardLine(r.reward, rankOf[m.model])}${adviceLine(r.advice)}${greenLine(r.green_light)}${quotaLine(r.quota)}${learnedLine(r.learned, r.handoffs)}${trainButton(m.model, r)}${coffeeButton(m.model, r)}
      <div class="stats"><span><b>${pnl(m.pnl_usd ?? 0)}</b>resultado</span><span><b>${m.trades ?? 0}</b>trades</span>
        <span><b>${isRules ? "grátis" : usd(r.ai_cost_usd ?? 0)}</b>custo IA</span></div>
      <div class="tip" role="tooltip">${tip}<div class="note" style="margin-top:6px">${esc(bubble)}</div></div>
    </div>`;
  };
  const byRank = (a, b) => (rankOf[a.model] || 99) - (rankOf[b.model] || 99);
  const tit = shown.filter(isTitular).sort(byRank), res = shown.filter((m) => !isTitular(m)).sort(byRank);
  const head = (cls, ico, title, sub) => `<div class="squad ${cls}"><span class="ico">${ico}</span><b>${title}</b><span>${sub}</span></div>`;
  setHTML("team-grid",
    (tit.length ? `<div class="formation">${head("pitch", "⚽", `Em campo · ${tit.length} titular${tit.length === 1 ? "" : "es"}`, "operam dinheiro real")}<div class="plates">${tit.map(card).join("")}</div></div>` : "") +
    (res.length ? `<div class="formation bench">${head("bench", "🪑", `Banco de reservas · ${res.length}`, "treinam no laboratório até ganhar a vaga")}<div class="plates">${res.map(card).join("")}</div></div>` : ""));
}

// ─── AI turbo: a stronger model for N cycles, then back to Haiku ───
function startBoost(btn) {
  const bots = [...document.querySelectorAll("#boost-bots input[data-bot]:checked")].map((i) => i.dataset.bot);
  const model = document.getElementById("boost-model").value, cycles = +document.getElementById("boost-cycles").value;
  if (!bots.length) return toast("Marque pelo menos um titular", true);
  const names = bots.map((b) => look(b).label).join(", ");
  run(btn, "Ativando…", async () => { for (const bot of bots) await api("/arena/boost", "POST", { bot, model, cycles }); },
      `🚀 Sinais de ${names} revisados por ${look(model).label} por ${cycles} ciclo(s) (~${Math.round(cycles * 5)} min); depois volta ao Haiku. Só gasta quando há sinal.`);
}
const reviewOn = (r) => r?.review_boost?.until && new Date(r.review_boost.until) > new Date() ? r.review_boost : null;
function renderBoost() {
  const tit = (lastArena?.leaderboard || []).filter((r) => r.green_light?.ok);
  const box = document.getElementById("boost-bots"), key = tit.map((r) => r.model).join(",");
  if (box.dataset.key !== key) {               // rebuild only when the titulares change, so ticks survive refreshes
    const keep = new Set([...box.querySelectorAll("input[data-bot]:checked")].map((i) => i.dataset.bot));
    box.dataset.key = key;
    box.innerHTML = `<label><input type="checkbox" id="boost-all" onchange="document.querySelectorAll('#boost-bots input[data-bot]').forEach((i) => i.checked = this.checked)"> Todos</label>` +
      tit.map((r) => `<label><input type="checkbox" data-bot="${esc(r.model)}" ${keep.has(r.model) ? "checked" : ""}
        onchange="document.getElementById('boost-all').checked = [...document.querySelectorAll('#boost-bots input[data-bot]')].every((i) => i.checked)"> ${esc(look(r.model).label)}</label>`).join("");
  }
  const on = tit.filter(reviewOn);
  document.getElementById("boost-status").innerHTML = on.length
    ? on.map((r) => `<b>${esc(look(r.model).label)}</b>: ${esc(look(r.review_boost.model).label)} até ${hhmm(r.review_boost.until)}`).join(" · ")
    : "todos os titulares com o modelo padrão (Haiku 5.5)";
}
// ☕ per bot: titulares any time (wakes the accounts too); reserves once per Lisbon day, in the lab
const lisbonDay = () => new Date().toLocaleDateString("en-CA", { timeZone: "Europe/Lisbon" });
function coffeeButton(model, r) {
  if (!r) return "";
  const until = r.coffee_until && new Date(r.coffee_until) > new Date() ? r.coffee_until : null;
  if (until) return `<button class="tcoffee on" disabled title="com café: sem pausas">☕ até ${new Date(until).toLocaleTimeString("pt-BR", { timeZone: "Europe/Lisbon", hour: "2-digit", minute: "2-digit" })}</button>`;
  if (!r.green_light?.ok && r.coffee_day === lisbonDay()) return `<button class="tcoffee" disabled>☕ café de hoje já usado</button>`;
  return `<button class="tcoffee" onclick="event.stopPropagation(); botCoffee('${esc(model)}', this)">☕ ${r.green_light?.ok ? "Café" : "Café (1 por dia)"}</button>`;
}
function botCoffee(model, btn) {
  if (REMOTE) return;
  run(btn, "Preparando…", () => api("/arena/coffee", "POST", { bot: model, hours: 4 }),
      (r) => `☕ ${look(model).label} com café até ${hhmm(r.coffee_until)}${r.accounts.length ? " — contas simulada e real acordadas" : " no laboratório"}`);
}

// reward: points = net profit of the last 30 days after AI cost; it buys priority and position size
const TIER_PT = { positivo: "✅ positivo", "em observação": "⚠️ em observação", "em avaliação": "⏳ em avaliação" };
function rewardLine(rw, rank) {
  if (!rw) return "";
  const medal = rw.points > 0 ? ["🥇", "🥈", "🥉"][rank - 1] || "" : "";
  return `<div class="reward ${rw.points > 0 ? "up" : rw.points < 0 ? "down" : ""}" title="${TIER_PT[rw.tier] || esc(rw.tier)} · 1 ponto = 1 centavo de lucro líquido (30 dias, já sem o custo da IA). Mais pontos = prioridade e posição cheia; recorde negativo = posição mínima.">
    <span>${medal}<b>${rw.points >= 0 ? "+" : ""}${rw.points} pts</b>${rw.streak >= 2 ? ` 🔥${rw.streak}` : ""}</span><span>posição ${usd(rw.size_usd, 0)}</span></div>`;
}
// lab → real money gate, per bot
function greenLine(g) {
  if (!g) return "";
  if (g.trusted) return `<div class="gl ok" title="validado no backtest — opera na conta real">✅ conta real</div>`;
  if (g.ok) return `<div class="gl ok" title="passou no laboratório: ${g.trades} trades, acerto ${pct(g.win_rate)}, fator ${g.profit_factor}">🟢 luz verde · conta real</div>`;
  const w = Math.min(clamp01(g.wins / g.needed), clamp01(g.trades / (g.needed_trades || 1))) * 100;
  return `<div class="gl" title="Para a conta real: ${g.needed_trades} trades no laboratório, ${g.needed} vencedores, acerto ≥ 60%, fator de lucro ≥ 1,2, lucro líquido > 0. Falta: ${(g.missing || []).join(", ")}">
    <span>🚦 ${g.trades}/${g.needed_trades} · ✅ ${g.wins}/${g.needed} · ❌ ${g.trades - g.wins}${g.trades ? ` · ${pct0(g.win_rate)}` : ""}</span><i style="width:${w.toFixed(0)}%"></i></div>`;
}
// ─── bot modal: everything about one bot ───
let modalBot = null;
function closeBot() { modalBot = null; document.getElementById("bot-modal").hidden = true; document.body.style.overflow = ""; }
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && modalBot) closeBot(); });
const dt = (iso) => new Date(iso).toLocaleString("pt-BR", { timeZone: "Europe/Lisbon", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
let modalTab = "resumo", modalData = null, modalKind = "crypto";
async function openBot(model) {
  modalBot = model;
  modalKind = "crypto";
  modalTab = "resumo";
  modalData = null;
  document.getElementById("bot-modal").hidden = false;
  document.body.style.overflow = "hidden";
  document.getElementById("bm-body").innerHTML = '<div class="bm-body"><div class="empty">Carregando…</div></div>';
  await renderBotModal();
}
function botTab(t) { modalTab = t; modalKind === "meme" ? paintMemeModal() : paintBotModal(); }
async function renderBotModal() {
  if (!modalBot) return;
  if (modalKind === "meme") return renderMemeModal();
  const d = await api(`/arena/bot/${encodeURIComponent(modalBot)}`);
  if (d.model !== modalBot) return;
  modalData = d;
  paintBotModal();
}
function paintBotModal() {
  const d = modalData;
  if (!d) return;
  const L = look(d.model), r = d.board || {}, s = d.stats || {}, rs = d.real_stats || {}, g = r.green_light || {}, p = d.projection;
  const scrollTop = document.querySelector(".bm-body")?.scrollTop || 0;
  const chips = [d.titular ? "⚽ Titular · opera dinheiro real" : "🪑 Reserva · treina no laboratório",
    r.turbo_until && new Date(r.turbo_until) > new Date() ? "⚡ Turbo até a meia-noite" : "",
    r.coffee_until && new Date(r.coffee_until) > new Date() ? `☕ Café até ${hhmm(r.coffee_until)}` : "",
    reviewOn(r) ? `🚀 ${esc(look(reviewOn(r).model).label)} revisa até ${hhmm(reviewOn(r).until)}` : ""].filter(Boolean).map((t) => `<span>${t}</span>`).join("") + `<span class="mood">${d.mood.emoji} ${esc(d.mood.label)}</span>`;
  const narrow = matchMedia("(max-width: 760px)").matches;
  const tabs = [["resumo", "Resumo"], ["proximas", `${narrow ? "Próximas" : "Próximas trades"}${d.opportunities.length ? ` (${d.opportunities.length})` : ""}`],
    ["trades", `Trades (${d.trades.length})`], ["aprendizado", narrow ? "Aprendeu" : "Aprendizado"]];
  const hero = (l, v, sub = "") => `<div><small>${l}</small><b>${v}</b>${sub ? `<em>${sub}</em>` : ""}</div>`;
  const rows = (pairs) => `<div class="bm-rows">${pairs.filter(Boolean).map(([l, v]) => `<div><span>${l}</span><b>${v}</b></div>`).join("")}</div>`;
  let body = "";
  if (modalTab === "resumo") {
    const realOn = d.titular || rs.trades || d.positions_real.length;
    const prog = g.ok ? 1 : Math.min((g.trades || 0) / (g.needed_trades || 20), (g.wins || 0) / (g.needed || 12));
    body = `<h4>${realOn ? "Conta real (dinheiro de verdade)" : "Laboratório"}</h4>
      <div class="bm-hero">${realOn
        ? hero("Resultado fechado", pnl(rs.pnl_usd ?? 0), `${rs.trades || 0} trades${r.quota ? ` · hoje ${r.quota.done}/${r.quota.target}` : ""}`) + hero("Acerto", pct0(rs.win_rate), rs.trades ? `${rs.wins} de ${rs.trades}` : "sem trades ainda") +
          hero("Em aberto agora", pnl(d.open_pnl_real), plural(d.positions_real.length, "posição", "posições")) + hero("Média por trade", pnl(rs.expectancy ?? 0))
        : hero("Resultado fechado", pnl(s.pnl_usd ?? 0), `${s.trades || 0} trades`) + hero("Acerto", pct0(s.win_rate), s.trades ? `${s.wins} de ${s.trades}` : "sem trades ainda") +
          hero("Em aberto agora", pnl(d.open_pnl), plural(d.positions_lab.length, "posição", "posições")) + hero("Média por trade", pnl(s.expectancy ?? 0))}</div>
      <div class="bm-two" style="margin-top:12px">
        <div class="bm-card"><div class="t">🧪 Laboratório <span class="muted">dinheiro falso, mesmas regras</span></div>${rows([
          ["Trades fechados", `${s.trades || 0} · acerto ${pct0(s.win_rate)}`], ["Resultado fechado", pnl(s.pnl_usd ?? 0)], ["Em aberto", pnl(d.open_pnl)],
          ["Ganho médio / perda média", `${pnl(s.avg_win)} / ${pnl(s.avg_loss)}`], ["Fator de lucro", s.profit_factor ?? "–"],
          ["Tempo médio por trade", s.avg_hold_h != null ? `${s.avg_hold_h}h` : "–"], ["Trades por dia", p.trades_per_day]])}</div>
        <div class="bm-card"><div class="t">🔮 Projeção <span class="muted">pelo histórico dele</span></div>${rows([
          ["Próximas 24h", `~${p["24h"].trades} trades · ${pnl(p["24h"].expected)}`], ["faixa provável", `${pnl(p["24h"].low)} a ${pnl(p["24h"].high)}`],
          ["Próximas 48h", `~${p["48h"].trades} trades · ${pnl(p["48h"].expected)}`], ["faixa provável", `${pnl(p["48h"].low)} a ${pnl(p["48h"].high)}`]])}
          <p class="note" style="margin:10px 0 0">${p.sample < 10 ? `⚠️ Só ${p.sample} trades de base — estimativa fraca.` : `Base: ${p.sample} trades no laboratório.`} Não é promessa.</p></div>
      </div>
      ${g.ok ? "" : `<h4>Rumo à conta real</h4><div class="bm-card"><div class="t"><span>${g.trades || 0} de ${g.needed_trades || 20} trades · ${g.wins || 0} de ${g.needed || 12} vitórias</span><span>${pct0(prog)}</span></div>
        <div class="bm-prog"><i style="width:${prog * 100}%"></i></div><span class="muted">Falta: ${esc((g.missing || []).join(", ") || "—")}</span></div>`}
      ${adviceCard(r)}
      ${promoteCard(d, r)}
      ${evolutionCard(d, r)}
      <h4>Resultado por dia</h4>${daysChart(d.days)}
      ${REMOTE ? "" : `<div class="bm-actions">${trainButton(d.model, r)}${coffeeButton(d.model, r)}</div>`}`;
  } else if (modalTab === "proximas") {
    body = `<p class="note" style="margin:0 0 12px">Moedas mais perto da regra de compra do ${esc(L.label)}. <b>Pronto</b> = a regra bateu; a compra ainda passa pelo revisor e pelo Risk (vagas, moeda já em carteira, aprendizado).</p>` +
      (d.opportunities.length ? `<div class="bm-opps">${d.opportunities.map((o) => `<div class="bm-opp"><div class="c">${coinIcon(o.symbol)}<span><b>${esc(o.symbol)}</b><br><span class="muted">${fmtPx(o.price)}</span></span></div>
        <div class="d">${esc(o.detail)}${o.held ? ' · <span class="yellow">já tem posição</span>' : ""}<div class="bm-prog ${o.status}"><i style="width:${o.progress * 100}%"></i></div></div>
        <span class="st ${o.status}">${o.status === "pronto" ? "✅ pronto" : o.status === "perto" ? "🟡 perto" : "⚪ longe"}</span></div>`).join("")}</div>`
        : `<div class="empty">Nenhuma moeda perto da regra dele agora. ${d.model.startsWith("rules") ? "Ele espera uma queda forte numa moeda em alta." : ""}</div>`);
  } else if (modalTab === "trades") {
    const open = [...d.positions_real.map((x) => ({ ...x, w: "💰 Real" })), ...d.positions_lab.map((x) => ({ ...x, w: "🧪 Lab" }))];
    body = `<h4>Posições abertas (${open.length})</h4>` + (open.length ? `<div class="tbl"><table><tr><th>Onde</th><th>Moeda</th><th>Desde</th><th class="n">Investido</th><th class="n">Agora</th><th class="n">Resultado</th></tr>` +
        open.map((x) => `<tr><td>${x.w}</td><td>${coinIcon(x.symbol)}<b>${esc(x.symbol)}</b></td><td>${dt(x.opened_at)}</td><td class="n">${usd(x.cost)}</td><td class="n">${usd(x.now)}</td><td class="n">${pnl(x.pnl)}</td></tr>`).join("") + "</table></div>"
        : '<div class="empty">Nenhuma posição aberta.</div>') +
      `<h4>Trades fechados (${d.trades.length} mais recentes)</h4>` + (d.trades.length ? `<div class="tbl"><table><tr><th>Fechou</th><th>Onde</th><th>Moeda</th><th class="n">Investido</th><th class="n">Resultado</th><th class="n">%</th><th>Como saiu</th></tr>` +
        d.trades.map((t) => `<tr><td>${dt(t.exit_ts)}</td><td>${t.where === "real" ? "💰 Real" : "🧪 Lab"}</td><td>${coinIcon(t.symbol)}${esc(t.symbol)}</td>
          <td class="n">${usd(t.cost)}</td><td class="n">${pnl(t.pnl)}</td><td class="n ${t.pct >= 0 ? "green" : "red"}">${t.pct > 0 ? "+" : ""}${t.pct.toFixed(2)}%</td><td class="muted">${esc(exitPt(t.reason))}</td></tr>`).join("") + "</table></div>"
        : '<div class="empty">Sem trades fechados ainda.</div>');
  } else {
    const item = (x) => `<div><span>${esc(x.tag)}</span><b>${x.wins}/${x.n} ganhos · ${pnl(x.pnl)}</b></div>`;
    body = `<p class="note" style="margin:0 0 12px">Cada compra guarda a situação em que foi feita. Quando uma situação junta 6+ trades com acerto abaixo de 35% e prejuízo, o bot passa a evitá-la — até o histórico melhorar.</p>
      <div class="bm-two"><div class="bm-card"><div class="t">🚫 Evita</div>${d.learned.avoid.length ? `<div class="bm-rows">${d.learned.avoid.map(item).join("")}</div>` : '<span class="muted">Nada ainda.</span>'}</div>
      <div class="bm-card"><div class="t">✅ Vai melhor em</div>${d.learned.best.length ? `<div class="bm-rows">${d.learned.best.map(item).join("")}</div>` : '<span class="muted">Nada ainda.</span>'}</div></div>
      <p class="note">${d.learned.seen} trades analisados nos últimos 30 dias.</p>
      <h4>📡 Rede do time</h4>
      <p class="note" style="margin:0 0 10px">Os bots compartilham o que aprendem: uma situação em que o time todo perde é evitada por todos (a menos que o próprio bot vá bem nela), e quando um bot vê uma oportunidade que combina mais com o perfil de outro, passa para ele. Base: ${d.team.seen} trades do time.</p>
      <div class="bm-two"><div class="bm-card"><div class="t">🚫 O time evita</div>${d.team.avoid.length ? `<div class="bm-rows">${d.team.avoid.map(item).join("")}</div>` : '<span class="muted">Nada ainda — o time precisa de 6+ trades na mesma situação.</span>'}</div>
      <div class="bm-card"><div class="t">📨 Passes</div><div class="bm-rows">${[...d.team.sent.map((x) => `<div><span>↗ ${dt(x.ts)} ${esc(x.reason)}</span></div>`),
        ...d.team.received.map((x) => `<div><span>↙ ${dt(x.ts)} ${esc(x.symbol)}: ${esc(x.reason)}</span></div>`)].join("") || '<span class="muted">Nenhum passe ainda.</span>'}</div></div></div>`;
  }
  document.getElementById("bm-body").innerHTML = `
    <div class="bm-top" style="--c:${L.color}"><div class="bm-head">${botFace(L.color, L.ico)}
      <div style="min-width:0"><div class="bm-name" id="bm-title">${esc(L.label)}</div><div class="bm-role">${esc(L.role)}</div><div class="bm-chips">${chips}</div></div>
      <div class="bm-mood" title="${esc(d.mood.why)}"><div class="e">${d.mood.emoji}</div><b>${esc(d.mood.label)}</b></div></div>
      <div class="bm-tabs" role="tablist">${tabs.map(([k, l]) => `<button role="tab" aria-selected="${k === modalTab}" class="${k === modalTab ? "on" : ""}" onclick="botTab('${k}')">${l}</button>`).join("")}</div></div>
    <div class="bm-body">${body}</div>`;
  document.querySelector(".bm-body").scrollTop = scrollTop;
}
// my read on each reserve: ⭐ promote, 🌱 promising, 👀 watching, 🔻 likely cut (replaced by a better-tuned successor)
function adviceLine(a) {
  if (!a) return "";
  return `<div class="advice ${a.flag}" title="${esc(a.reasons.join(" · "))}">${esc(a.label)}</div>`;
}
function adviceCard(r) {
  const a = r.advice;
  if (!a) return "";
  const p = a.period;
  return `<h4>🧑‍🏫 Minha análise</h4><div class="bm-card advice-card ${a.flag}"><div class="t"><span>${esc(a.label)}</span>
    ${p ? `<span class="muted">período atual: ${p.wins}/${p.trades} · corte ${dt(p.ends)}</span>` : ""}</div>
    <div class="bm-rows">${a.reasons.map((x) => `<div><span>• ${esc(x)}</span></div>`).join("")}</div></div>`;
}

// ─── 💬 Vestiário: the bots' group chat ───
let chatMsgs = [], chatSince = 0, chatBy = "";
const CHAT_KINDS = { trade: ["entry", "exit"], handoff: ["handoff", "tip", "chat"], learn: ["lesson", "network", "evolution", "quota"] };
function chatFilter(btn) {
  chatBy = btn.dataset.c;
  document.querySelectorAll("#chat-filter button").forEach((b) => b.classList.toggle("on", b === btn));
  renderChat(true);
}
async function loadChat() {
  const r = await api(`/team/chat?limit=120&since=${chatSince}`);
  if (!r.messages.length) return;
  chatMsgs = [...chatMsgs, ...r.messages].slice(-200);
  chatSince = chatMsgs[chatMsgs.length - 1].id;
  renderChat(false);
}
// @Name -> highlighted (names come from the team, so "@São Marcos" works with its space)
function mentions(html) {
  const names = (lastArena?.leaderboard || []).map((r) => look(r.model).label).sort((a, b) => b.length - a.length);
  return names.reduce((h, n) => h.split(`@${esc(n)}`).join(`<b class="mention">@${esc(n)}</b>`), html);
}
function renderChat(force) {
  const box = document.getElementById("chat");
  const atBottom = force || box.scrollHeight - box.scrollTop - box.clientHeight < 60;
  const shown = chatMsgs.filter((m) => !chatBy || CHAT_KINDS[chatBy].includes(m.kind));
  box.innerHTML = shown.length ? shown.map((m) => {
    const net = m.author === "rede", L = net ? { label: "Rede do time", color: "#34d399", ico: "📡" } : look(m.author);
    const res = m.kind === "exit" ? (/\+\$/.test(m.text) ? " win" : /−\$/.test(m.text) ? " loss" : "") : "";
    return `<div class="msg c-${m.kind}${res}" style="--c:${L.color}">${net ? "" : `<div class="av">${botFace(L.color, L.ico)}</div>`}
      <div class="bub"><div class="who">${esc(L.ico || "")} ${esc(L.label)}<small>${hhmm(m.ts)}</small></div>${mentions(esc(m.text))}</div></div>`;
  }).join("") : '<div class="empty">Ninguém falou ainda — as mensagens aparecem quando os bots comprarem, venderem, passarem oportunidades ou aprenderem algo.</div>';
  if (atBottom) box.scrollTop = box.scrollHeight;
}
safe(loadChat);
setInterval(() => safe(loadChat), 6000);

// ─── 🐸 Trincheira: meme-coin lab bots ───
// ─── real meme account (Solana): the bot wallet signs its own trades; the user's Phantom deposits and receives withdrawals ───
// the Phantom browser extension injects window.phantom.solana (older builds: window.solana), sometimes a moment after load
const phantom = () => (window.phantom?.solana?.isPhantom ? window.phantom.solana : window.solana?.isPhantom ? window.solana : null);
async function phantomKey() {
  let p = phantom();
  for (let i = 0; !p && i < 10; i++) { await new Promise((r) => setTimeout(r, 200)); p = phantom(); }
  if (!p) throw new Error("Phantom não encontrada neste navegador. Abra http://127.0.0.1:8000 no Chrome/Brave/Firefox com a extensão Phantom instalada e desbloqueada.");
  return (await p.connect()).publicKey.toString();
}
const phantomLine = () => (phantom() ? '<span class="green">👻 extensão Phantom detectada</span>'
  : REMOTE ? '<span class="muted">Conectar a Phantom só funciona no Mac, em http://127.0.0.1:8000</span>'
  : '<span class="red">extensão Phantom não detectada neste navegador</span> — <a href="https://phantom.app/download" target="_blank" rel="noopener">instalar</a> e recarregar');
async function connectPhantom(btn) {
  run(btn, "Conectando…", async () => {
    const addr = await phantomKey();
    const { message } = await api(`/meme/wallet/owner-message?address=${encodeURIComponent(addr)}`);
    const { signature } = await phantom().signMessage(new TextEncoder().encode(message), "utf8");
    return api("/meme/wallet/owner", "POST", { address: addr, signature: btoa(String.fromCharCode(...signature)) });
  }, (r) => `👻 Phantom conectada: ${short(r.owner)}. Saques só vão para ela.`).then(() => safe(loadMemeReal));
}
function web3lib() {
  return window.solanaWeb3 ? Promise.resolve(window.solanaWeb3) : new Promise((ok, bad) => {
    const s = Object.assign(document.createElement("script"), { src: "https://cdn.jsdelivr.net/npm/@solana/web3.js@1.95.3/lib/index.iife.min.js" });
    s.onload = () => ok(window.solanaWeb3); s.onerror = () => bad(new Error("não carregou a biblioteca da Solana"));
    document.head.appendChild(s);
  });
}
async function depositPhantom(btn) {
  const sol = Number((prompt("Quanto SOL depositar na carteira de memes? (ex.: 0.15 ≈ $25)") || "").replace(",", "."));
  if (!(sol > 0)) return;
  run(btn, "Aprove na Phantom…", async () => {
    const W = await web3lib(), from = await phantomKey();
    const { blockhash, to } = await api("/meme/wallet/blockhash");
    const tx = new W.Transaction({ feePayer: new W.PublicKey(from), recentBlockhash: blockhash })
      .add(W.SystemProgram.transfer({ fromPubkey: new W.PublicKey(from), toPubkey: new W.PublicKey(to), lamports: Math.round(sol * 1e9) }));
    const { signature } = await phantom().signAndSendTransaction(tx);
    for (let i = 0; i < 20; i++) {                // wait for Solana to confirm, then the server checks it on-chain
      await new Promise((r) => setTimeout(r, 3000));
      try { return await api("/meme/wallet/deposit", "POST", { signature }); } catch (e) { if (i === 19) throw e; }
    }
  }, (r) => `✅ Depósito confirmado (${usd(r.usd)})`).then(() => safe(loadMemeReal));
}
function withdrawMeme(btn) {
  if (!confirm("Sacar TUDO da carteira de memes?\n\nVende as meme coins abertas e manda todo o SOL para a sua Phantom conectada.")) return;
  run(btn, "Sacando…", () => api("/meme/wallet/withdraw", "POST"), (r) => `✅ Saque enviado (${short(r.signature)})`).then(() => safe(loadMemeReal));
}
function promoteMeme(bot, btn) {
  if (!confirm(`Promover ${look(bot).label} a TITULAR do time de memes?\n\nEle passa a operar a conta real de meme coins (Solana), com os limites pequenos.`)) return;
  run(btn, "Promovendo…", () => api("/meme/promote", "POST", { bot }), `⚽ ${look(bot).label} é titular do time de memes`).then(() => safe(loadMeme));
}
// ─── tabs below the fold: one band at a time; anchors (#g-bots, #ecg, …) open the right tab ───
function showTab(name, el = null) {
  document.querySelectorAll(".tabpane").forEach((p) => p.classList.toggle("on", p.dataset.tab === name));
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === name));
  try { localStorage.setItem("tab", name); } catch (e) { /* private mode */ }
  if (el) el.scrollIntoView({ block: "start" });
  else if (window.scrollY > document.getElementById("tabs").offsetTop) document.getElementById("tabs").scrollIntoView({ block: "start" });
}
function openAnchor(hash) {
  const el = hash && document.querySelector(hash);
  if (!el) return false;
  const pane = el.closest(".tabpane");
  if (pane) { showTab(pane.dataset.tab, el); return true; }
  return false;
}
window.addEventListener("hashchange", () => openAnchor(location.hash));
(() => { let t = null; try { t = localStorage.getItem("tab"); } catch (e) { /* private mode */ }
  if (!openAnchor(location.hash) && t && document.querySelector(`.tabpane[data-tab="${t}"]`)) showTab(t); })();

// ─── wallets: the two real accounts, side by side ───
let memeWallet = null;
function copyText(t) { navigator.clipboard.writeText(t).then(() => toast("Endereço copiado")); }
function renderWallets() {
  const a = acc, c = a?.config || {}, w = memeWallet;
  const real = a && a.kind === "real";
  const mm = `<div class="wallet mm"><div class="wh"><span class="ico">🦊</span><div><b>MetaMask · Hyperliquid</b><small>cripto · rede Arbitrum One · ${esc(c.network || "mainnet")}</small></div></div>
    <div class="big">${a ? usd(real ? a.exchange_balance : a.equity) : "–"}<small>${real ? "saldo na Hyperliquid" : "saldo (simulação)"}</small></div>
    <div class="rows"><span>Disponível <b>${a ? usd(a.available) : "–"}</b></span><span>Em margem <b>${a ? usd(a.margin_used) : "–"}</b></span>
      <span>Posições abertas <b>${a?.positions?.length ?? a?.open_positions ?? "–"}</b></span><span>Resultado fechado <b>${a ? pnl(a.pnl_usd) : "–"}</b></span></div>
    <div class="addr">Endereço: ${c.account ? `<code title="${esc(c.account)}">${esc(short(c.account))}</code><button class="link" onclick="copyText('${esc(c.account)}')">copiar</button>` : '<span class="muted">conta real não configurada</span>'}</div>
    <div class="acts"><a class="btn" href="#fundos" onclick="openAnchor('#fundos'); return false;"><button class="primary">➕ Depositar USDC</button></a>
      <a class="btn" href="https://app.hyperliquid.xyz/portfolio" target="_blank" rel="noopener"><button>Hyperliquid ↗</button></a>
      ${c.account ? `<a class="btn" href="https://arbiscan.io/address/${esc(c.account)}" target="_blank" rel="noopener"><button>Arbiscan ↗</button></a>` : ""}</div></div>`;
  const mode = w ? (w.mode === "real" ? '<span class="mode real">💵 REAL</span>' : '<span class="mode fake">🧪 ENSAIO</span>') : "";
  const ph = `<div class="wallet ph"><div class="wh"><span class="ico">👻</span><div><b>Phantom · meme coins ${mode}</b><small>Solana · carteira do bot separada; a Phantom deposita e recebe os saques</small></div></div>
    <div class="big">${w && w.usd != null ? usd(w.usd) : "–"}<small>${w && w.sol != null ? `${w.sol.toFixed(4)} SOL na carteira do bot` : "saldo da carteira do bot"}</small></div>
    <div class="rows"><span>Dona (Phantom) <b>${w?.owner ? "👻 " + esc(short(w.owner)) : "não conectada"}</b></span><span>Resultado ${w?.mode === "real" ? "" : "do ensaio "}<b>${w ? pnl(w.pnl) : "–"}</b></span>
      <span>Posições abertas <b>${w ? w.positions.length : "–"}</b></span><span>Compras hoje <b>${w ? `${w.buys_today}/${w.limits.max_buys_day}` : "–"}</b></span></div>
    <div class="addr">Carteira do bot: ${w ? `<code title="${esc(w.address)}">${esc(short(w.address))}</code><button class="link" onclick="copyText('${esc(w.address)}')">copiar</button>` : "–"}</div>
    <div class="addr">${phantomLine()}</div>
    ${REMOTE ? "" : `<div class="acts"><button class="primary" onclick="connectPhantom(this)">👻 ${w?.owner ? "Trocar Phantom" : "Conectar Phantom"}</button>
      <button onclick="depositPhantom(this)" ${w?.owner ? "" : "disabled title='Conecte a Phantom primeiro'"}>⬇️ Depositar SOL</button>
      <button onclick="withdrawMeme(this)" ${w?.owner ? "" : "disabled title='Conecte a Phantom primeiro'"}>⬆️ Sacar tudo</button>
      ${w?.address ? `<a class="btn" href="https://solscan.io/account/${esc(w.address)}" target="_blank" rel="noopener"><button>Solscan ↗</button></a>` : ""}</div>`}</div>`;
  setHTML("wallets", mm + ph);
}

async function loadMemeReal() {
  const w = await api("/meme/wallet"), L = w.limits;
  memeWallet = w;
  renderWallets();
  const real = w.mode === "real";
  setHTML("meme-real", `<div class="t"><b>💵 Conta real de meme coins <span class="muted">(Solana · separada da conta de cripto)</span></b>
      <span class="mode ${real ? "real" : "fake"}">${real ? "💵 REAL" : "🧪 ENSAIO · nada é enviado"}</span>${w.halted ? ' <span class="mode real" style="color:var(--red)">🛑 parada (kill switch)</span>' : ""}</div>
    <div class="nums"><span><b>${w.usd == null ? "–" : usd(w.usd)}</b>na carteira${w.sol != null ? ` (${w.sol.toFixed(4)} SOL)` : ""}</span>
      <span><b>${pnl(w.pnl)}</b>${real ? "resultado" : "resultado do ensaio"}</span><span><b>${w.trades}</b>trades (${w.wins} ganhos)</span>
      <span><b>${w.buys_today}/${L.max_buys_day}</b>compras hoje</span></div>
    <div class="note">👛 Carteira do bot, Phantom, depósito e saque: na seção <a href="#carteiras">Carteiras</a>, no topo.</div>
    <div class="chips">${w.positions.map((p) => `<span>${esc(p.symbol)} · ${esc(look(p.bot).label)} ${pnl(p.now - p.cost)}</span>`).join("") || '<span class="muted">sem posições</span>'}</div>
    <p class="note">Limites: $${L.trade_usd} por trade · ${L.max_positions} posições · ${L.max_buys_day} compras/dia · para no dia com −$${L.daily_loss_usd} ·
      impacto ≤ ${L.max_impact_pct}% · slippage ≤ ${L.slippage_pct}% · abaixo de ${pct0(L.kill_pct)} do depositado vende tudo e para.
      ${real ? "Só os titulares do time de memes operam aqui." : "No ensaio, toda compra e venda do laboratório é cotada no Jupiter e simulada na Solana, sem enviar."}</p>
    <details><summary>Últimos passos</summary><div class="bm-rows">${w.log.slice(0, 8).map((d) => `<div><span>${hhmm(d.ts)} ${d.symbol ? esc(d.symbol) + " · " : ""}${esc(d.reason)}</span></div>`).join("") || '<div class="muted">nada ainda</div>'}</div></details>`);
}
safe(loadMemeReal);
setInterval(() => safe(loadMemeReal), 30000);

// ─── meme bot modal: the same dialog as the crypto bots, with the trench's own facts ───
let memeModalData = null;
async function openMemeBot(model) {
  modalBot = model;
  modalKind = "meme";
  modalTab = "resumo";
  memeModalData = null;
  document.getElementById("bot-modal").hidden = false;
  document.body.style.overflow = "hidden";
  document.getElementById("bm-body").innerHTML = '<div class="bm-body"><div class="empty">Carregando…</div></div>';
  await renderMemeModal();
}
async function renderMemeModal() {
  if (!modalBot || modalKind !== "meme") return;
  const d = await api(`/meme/bot/${encodeURIComponent(modalBot)}`);
  if (d.model !== modalBot) return;
  memeModalData = d;
  paintMemeModal();
}
function paintMemeModal() {
  const d = memeModalData;
  if (!d) return;
  const L = look(d.model), s = d.stats, P = d.params;
  const scrollTop = document.querySelector(".bm-body")?.scrollTop || 0;
  const chips = [d.titular ? "⚽ Titular do time de memes · conta real de memes" : "🪑 Reserva · laboratório ($100 de mentira)",
    d.gen > 1 ? `🧬 Geração ${d.gen} · filho de ${esc(d.parent || "")}` : "", d.can_promote && !d.titular ? "✅ Pronto para subir" : ""]
    .filter(Boolean).map((t) => `<span>${t}</span>`).join("");
  const tabs = [["resumo", "Resumo"], ["trades", `Trades (${s.trades})`], ["decisoes", "Decisões"], ["aprendizado", "Aprendizado e evolução"]];
  const hero = (l, v, sub = "") => `<div><small>${l}</small><b>${v}</b>${sub ? `<em>${sub}</em>` : ""}</div>`;
  const rows = (pairs) => `<div class="bm-rows">${pairs.filter(Boolean).map(([l, v]) => `<div><span>${l}</span><b>${v}</b></div>`).join("")}</div>`;
  const ex = s.exits || {};
  const exitRow = (k, label) => ex[k] ? [label, `${ex[k].n} · ${pnl(ex[k].pnl)}`] : null;
  let body = "";
  if (modalTab === "resumo") {
    body = `<h4>${d.titular ? "Conta real de meme coins" : "Laboratório de memes"}</h4>
      <div class="bm-hero">${hero("Resultado fechado", pnl(s.pnl_usd), `${s.trades} trades`)}${hero("Acerto", pct0(s.win_rate), s.trades ? `${s.wins} de ${s.trades}` : "sem trades ainda")}
        ${hero("Em aberto agora", pnl(d.positions.reduce((a, p) => a + p.now - p.cost, 0)), plural(d.positions.length, "posição", "posições"))}${hero("Patrimônio", usd(s.equity))}</div>
      <div class="bm-two" style="margin-top:12px">
        <div class="bm-card"><div class="t">📊 Como ele sai <span class="muted">por tipo de saída</span></div>${rows([
          exitRow("ganho rápido", "💨 Ganho rápido"), exitRow("alvo", "🎯 Alvo"), exitRow("stop", "🛑 Stop"), exitRow("tempo", "⏱ Tempo máximo"), exitRow("rug", "🚨 Rug"),
          ["Ganho médio / perda média", `${pnl(s.avg_win)} / ${pnl(s.avg_loss)}`], ["Fator de lucro", s.profit_factor ?? "–"]])}</div>
        <div class="bm-card"><div class="t">⚙️ Regras dele <span class="muted">saídas desta geração</span></div>${rows([
          ["Stop", `−${(P.stop_pct * 100).toFixed(0)}%`], ["Alvo", `+${(P.tp_pct * 100).toFixed(0)}%`], ["Tempo máximo", `${P.max_hold_h}h`],
          ["Liquidez mínima", `$${Number(P.min_liq).toLocaleString("pt-BR")}`],
          ["Ganhos rápidos", P.quick_wins.map(([a, k]) => `+${(a * 100).toFixed(0)}% → guarda ${(k * 100).toFixed(0)}%`).join(" · ")]])}
          <p class="note" style="margin:10px 0 0">Meme sobe e cai em minutos: qualquer alta repentina já vira ganho garantido, sempre acima das taxas (0,5% por lado + impacto).</p></div></div>
      <h4>Posições abertas</h4>${d.positions.length ? `<div class="bm-rows">${d.positions.map((p) => `<div><span>${esc(p.symbol)} · desde ${dt(p.opened_at)}${p.peak_pct != null ? ` · pico ${p.peak_pct > 0 ? "+" : ""}${p.peak_pct}%` : ""}</span><b>${pnl(p.now - p.cost)}</b></div>`).join("")}</div>` : '<div class="empty">Sem posições agora.</div>'}
      <h4>⬆️ Caminho para titular</h4><div class="bm-card"><div class="t"><span>${d.titular ? "⚽ Já é titular do time de memes" : d.can_promote ? "✅ Pode subir para titular" : "Ainda não pode subir"}</span></div>
        <span class="muted">${s.wins} de ${d.need_wins} vitórias · acerto ${pct0(s.win_rate)} (mínimo ${pct0(d.need_win_rate)}). Titular de meme opera a conta real de memes (Solana), com limites pequenos.</span>
        ${d.can_promote && !d.titular && !REMOTE ? `<div class="bm-actions"><button class="promote" onclick="promoteMeme('${esc(d.model)}', this)">⬆️ Promover ${esc(L.label)} a titular</button></div>` : ""}</div>`;
  } else if (modalTab === "trades") {
    body = d.trades.length ? `<div class="tbl"><table><tr><th>Token</th><th>Entrada</th><th>Saída</th><th class="n">Resultado</th><th>Como saiu</th></tr>${d.trades.map((t) =>
      `<tr><td><b>${esc(t.symbol)}</b></td><td>${dt(t.entry_ts)}</td><td>${dt(t.exit_ts)}</td><td class="n">${pnl(t.pnl)} <span class="muted">(${t.pct > 0 ? "+" : ""}${t.pct}%)</span></td><td>${esc(t.reason)}</td></tr>`).join("")}</table></div>`
      : '<div class="empty">Nenhum trade fechado ainda.</div>';
  } else if (modalTab === "decisoes") {
    body = `<div class="bm-rows">${d.decisions.map((x) => `<div><span>${hhmm(x.ts)} <b>${esc(x.kind)}</b>${x.symbol ? ` ${esc(x.symbol)}` : ""} · ${esc(x.reason)}</span></div>`).join("") || '<div class="empty">Nada ainda.</div>'}</div>`;
  } else {
    const l = d.learned || { avoid: [], best: [], seen: 0 };
    const fmt = (x) => `${esc(x.tag)} (${x.wins}/${x.n}, ${pnl(x.pnl)})`;
    body = `<h4>🧠 O que ele aprendeu <span class="muted">${l.seen} trades analisados</span></h4><div class="bm-two">
      <div class="bm-card"><div class="t">Evita</div>${l.avoid.length ? `<div class="bm-rows">${l.avoid.map((x) => `<div><span>${fmt(x)}</span></div>`).join("")}</div>` : '<span class="muted">ainda sem padrão claro</span>'}</div>
      <div class="bm-card"><div class="t">Vai melhor em</div>${l.best.length ? `<div class="bm-rows">${l.best.map((x) => `<div><span>${fmt(x)}</span></div>`).join("")}</div>` : '<span class="muted">ainda sem padrão claro</span>'}</div></div>
      <h4>🧬 Evolução</h4><div class="bm-card">${d.gen > 1 ? `<div class="t"><span>Geração ${d.gen} · filho de ${esc(d.parent || "")}</span><span class="muted">nasceu ${dt(d.born_at)}</span></div>
        <div class="bm-rows">${d.why.map((w) => `<div><span>✨ ${esc(w)}</span></div>`).join("")}${d.inherited_avoid.length ? `<div><span>📚 Já nasceu evitando: ${d.inherited_avoid.map(esc).join(", ")}</span></div>` : ""}</div>` : `<div class="t"><span>Geração 1 · fundador</span><span class="muted">nasceu ${dt(d.born_at)}</span></div>`}
        ${!d.titular ? `<p class="note" style="margin:10px 0 0">A cada 12h de vida, se não deu lucro no período (ou não operou), ele é aposentado e nasce um sucessor melhorado com nome de campeão da Libertadores. ${d.next_cut ? `Próximo corte: <b>${dt(d.next_cut)}</b>.` : ""}</p>` : ""}</div>`;
  }
  document.getElementById("bm-body").innerHTML = `
    <div class="bm-top" style="--c:${L.color}"><div class="bm-head">${botFace(L.color, L.ico)}
      <div style="min-width:0"><div class="bm-name" id="bm-title">${esc(L.label)}</div><div class="bm-role">${esc(d.role)}</div><div class="bm-chips">${chips}</div></div></div>
      <div class="bm-tabs" role="tablist">${tabs.map(([k, l]) => `<button role="tab" aria-selected="${k === modalTab}" class="${k === modalTab ? "on" : ""}" onclick="botTab('${k}')">${l}</button>`).join("")}</div></div>
    <div class="bm-body">${body}</div>`;
  document.querySelector(".bm-body").scrollTop = scrollTop;
}

async function loadMeme() {
  const d = await api("/meme");
  memeBots = Object.fromEntries(d.bots.map((b) => [b.model, b]));
  const st = d.status || {};
  document.getElementById("meme-head").textContent = st.at ? `última varredura ${hhmm(st.at)} · ${st.candidates} tokens analisados${st.error ? " · erro: " + st.error : ""}` : "aguardando a primeira varredura…";
  setHTML("meme-bots", d.bots.map((b) => {
    const L = look(b.model);
    const m = b.mode || {};
    const badge = m.real ? '<span class="mode real">💵 REAL · titular</span>' : m.titular ? '<span class="mode real">⚽ titular · ensaio</span>' : '<span class="mode fake">🧪 FAKE · reserva</span>';
    const prog = m.titular ? "Titular do time de memes: opera a conta real" : `Titular: ${m.wins}/${m.need_wins} vitórias · acerto ${b.trades ? pct0(m.win_rate) : "–"} (mín. ${pct0(m.need_win_rate)})`;
    const cut = m.next_cut ? `<div class="gl" title="${esc(["Se não der lucro no período (ou não operar), é aposentado e nasce um sucessor melhorado.", ...(m.why || [])].join(" · "))}">🧬 ${m.gen > 1 ? `geração ${m.gen}, filho de ${esc(m.parent || "")} · ` : ""}próximo corte ${time(m.next_cut)}: precisa estar no lucro</div>` : "";
    const promo = m.ready && !m.titular && !REMOTE ? `<div class="bm-actions"><button class="promote" onclick="event.stopPropagation(); promoteMeme('${esc(b.model)}', this)">⬆️ Promover a titular</button></div>` : "";
    return `<div class="meme-bot click" style="--c:${L.color}" onclick="openMemeBot('${esc(b.model)}')" title="Clique para ver tudo sobre ${esc(L.label)}"><div class="top">${botFace(L.color, L.ico)}<div><b>${esc(L.label)}</b> ${badge}<div class="muted" style="font-size:12px">${esc(b.role)}</div></div></div>
      <div class="nums"><span><b>${pnl(b.pnl)}</b>resultado</span><span><b>${b.trades}</b>trades</span><span><b>${b.win_rate == null ? "–" : pct0(b.win_rate)}</b>acerto</span><span><b>${b.positions.length}</b>abertas</span></div>
      <div class="chips">${b.positions.map((p) => `<span>${esc(p.symbol)} ${pnl(p.now - p.cost)}</span>`).join("") || '<span class="muted">sem posições</span>'}
        ${b.recent.slice(0, 4).map((t) => `<span title="${esc(t.reason)}">${esc(t.symbol)} ${t.pct > 0 ? "+" : ""}${t.pct}%</span>`).join("")}</div>${learnedLine(b.learned)}
      <div class="gl${m.ready || m.titular ? " ok" : ""}" title="${m.titular ? "Opera a conta real de memes (Solana), ao mesmo tempo que os titulares de cripto operam a Hyperliquid." : "Treinando no laboratório com $" + "100 de mentira."}">🎓 ${prog}${m.ready && !m.titular ? " · ✅ pronto" : ""}</div>${cut}${promo}</div>`;
  }).join("") + (d.retired?.length ? `<p class="note" style="grid-column:1/-1;margin:0">🪦 Saíram por não fazer dinheiro: ${d.retired.map((r) => `${esc(r.name)} (${esc(r.verdict)}) → ${esc(r.successor_name)}`).join(" · ")}</p>` : ""));
  const r = d.radar || [];
  document.getElementById("meme-radar-note").textContent = r.length ? `· ${r.filter((x) => x.safe).length} de ${r.length} passam na segurança` : "";
  const f = (v, d2 = 0) => (v == null ? "–" : Number(v).toLocaleString("pt-BR", { maximumFractionDigits: d2 }));
  const hw = d.hot_wallets || [];
  document.getElementById("meme-whales").innerHTML = hw.length
    ? `🐋 <b>Carteiras quentes</b> (baleias comprando 2+ dos nossos tokens em 24h): ` + hw.slice(0, 5).map((w) => `<span title="${esc(w.wallet)}">${esc(w.wallet.slice(0, 4))}…${esc(w.wallet.slice(-4))} → ${w.tokens.map(esc).join(", ")} ($${f(w.usd)})</span>`).join(" · ")
    : '🐋 <span class="muted">Nenhuma baleia repetida nos nossos tokens ainda (lê as compras ≥ $1k de cada token seguro a cada minuto).</span>';
  setHTML("meme-radar", `<tr><th>Token</th><th class="n">Liquidez</th><th class="n">Holders</th><th class="n">Orgânico</th><th class="n">1h</th><th class="n" title="Compras menos vendas grandes (≥ $1k ou 1% da liquidez) na última hora">🐋 Baleias 1h</th><th>Segurança</th><th>Sinal</th></tr>` +
    (r.length ? r.slice(0, 15).map((x) => {
      const bad = Object.entries(x.checks).filter(([, c]) => !c.ok).map(([k, c]) => `${k}: ${c.txt}`);
      const sig = Object.entries(x.signals).filter(([, s]) => s).map(([b]) => look(b).label);
      return `<tr><td>${x.icon ? `<img class="ti" src="${esc(x.icon)}" alt="" loading="lazy" onerror="this.remove()">` : ""}<b>${esc(x.symbol)}</b>${x.graduated_h != null && x.graduated_h < 24 ? ' <span class="muted">🎓</span>' : ""}</td>
        <td class="n">$${f(x.liq)}</td><td class="n">${f(x.holders)}</td><td class="n">${f(x.organic)}</td>
        <td class="n ${(x.ch1h || 0) >= 0 ? "green" : "red"}">${x.ch1h == null ? "–" : `${x.ch1h > 0 ? "+" : ""}${f(x.ch1h, 1)}%`}</td>
        <td class="n ${x.whale ? (x.whale.net_1h >= 0 ? "green" : "red") : ""}" title="${x.whale ? `compras $${f(x.whale.buy_1h)} (${x.whale.buyers} carteiras) · vendas $${f(x.whale.sell_1h)} (${x.whale.sellers})` : "não lido nesta rodada"}">${x.whale ? `${x.whale.net_1h >= 0 ? "+" : "−"}$${f(Math.abs(x.whale.net_1h))}` : "–"}</td>
        <td title="${esc(bad.join(" · ") || "passou em tudo")}">${x.safe ? "✅ seguro" : `⛔ ${esc(bad[0] || "")}`}</td>
        <td>${sig.length ? `🟢 ${esc(sig.join(", "))}` : '<span class="muted">—</span>'}</td></tr>`;
    }).join("") : '<tr><td colspan="8" class="empty">Aguardando a primeira varredura (a cada minuto).</td></tr>'));
}
safe(loadMeme);
setInterval(() => safe(loadMeme), 30000);

// manual promotion (the user's rule: 12+ lab wins and win rate ≥ 35%)
function promoteCard(d, r) {
  const c = r.can_promote;
  if (d.titular || !c) return "";
  const why = `${c.wins} de ${c.need_wins} vitórias · acerto ${pct0(c.win_rate)} (mínimo ${pct0(c.need_win_rate)})`;
  return `<h4>⬆️ Subir para titular</h4><div class="bm-card"><div class="t"><span>${c.ok ? "✅ Pode subir para o time titular" : "Ainda não pode subir"}</span></div>
    <span class="muted">${why}. Titular opera dinheiro real (posição de $10, mesmos limites).</span>
    ${c.ok && (d.stats?.pnl_usd ?? 0) < 0 ? `<div class="bm-card" style="margin-top:10px;border-color:rgba(251,191,36,.45)">⚠️ <b>Atenção:</b> no laboratório ele está no prejuízo
      (${pnl(d.stats.pnl_usd)}, fator de lucro ${d.stats.profit_factor ?? "–"}): perde mais nas derrotas do que ganha nas vitórias. Promovido, deve perder dinheiro real.</div>` : ""}
    ${c.ok && !REMOTE ? `<div class="bm-actions"><button class="promote" onclick="promoteBot('${esc(d.model)}', this)">⬆️ Promover ${esc(look(d.model).label)} a titular</button></div>` : ""}</div>`;
}
function promoteBot(model, btn) {
  if (REMOTE) return;
  if (!confirm(`Promover ${look(model).label} a TITULAR?\n\nEle passa a operar DINHEIRO REAL na Hyperliquid, com posições de $10 e os mesmos limites dos outros titulares.`)) return;
  run(btn, "Promovendo…", () => api("/arena/promote", "POST", { bot: model }), `⚽ ${look(model).label} subiu para o time titular!`)
    .then(() => { safe(loadArena); setTimeout(() => safe(renderBotModal), 600); });
}

// evolution: reserves are judged every 12h of life; a weak one is replaced by an improved successor
function evolutionCard(d, r) {
  const lin = r.lineage, v = r.last_verdict;
  if (d.titular && !lin) return "";
  const next = r.advice?.period?.ends ? new Date(r.advice.period.ends) : null;
  return `<h4>🧬 Evolução</h4><div class="bm-card">${lin ? `<div class="t"><span>Geração ${lin.gen} · filho de ${esc(lin.parent_label || lin.parent)}</span><span class="muted">nasceu ${dt(lin.born_at)}</span></div>
      <div class="bm-rows">${lin.why.map((w) => `<div><span>✨ ${esc(w)}</span></div>`).join("")}
      ${lin.inherited_avoid.length ? `<div><span>📚 Já nasceu evitando: ${lin.inherited_avoid.map(esc).join(", ")}</span></div>` : ""}</div>` : ""}
    ${!d.titular ? `<p class="note" style="margin:${lin ? "10px" : "0"} 0 0">A cada 12h de vida, se o acerto do período ficar abaixo de 60% (ou sem trades), ele é aposentado e nasce um sucessor melhorado com nome de campeão da Libertadores.
      ${next ? `Próximo corte: <b>${dt(next.toISOString())}</b>.` : ""}${v ? ` Último corte: ${v.wins}/${v.trades} (${pct0(v.win_rate)}) ${v.pass ? "✅ passou" : "❌"}` : ""}</p>` : ""}</div>`;
}
function daysChart(days) {
  if (!days.length) return '<div class="empty">Sem trades fechados ainda.</div>';
  const max = Math.max(...days.map((x) => Math.abs(x.lab + x.real)), 0.01);
  return `<div class="bm-days">${[...days].reverse().map((x) => { const v = x.lab + x.real;
    return `<div title="${x.day}: lab ${usd(x.lab)} · real ${usd(x.real)} · ${x.trades} trades"><small>${v >= 0 ? "+" : "−"}$${Math.abs(v).toFixed(2)}</small>
      <span style="height:${Math.max(3, (Math.abs(v) / max) * 70)}px;background:${v >= 0 ? "var(--green)" : "var(--red)"}"></span><small>${x.day.slice(8)}/${x.day.slice(5, 7)}</small></div>`; }).join("")}</div>`;
}
const exitPt = (r) => /take-profit/.test(r) ? "🎯 alvo" : /stop-loss/.test(r) ? "🛑 stop" : /max holding/.test(r) ? "⏱ tempo máximo" : /manualmente/.test(r) ? "✋ você vendeu" : r;
setInterval(() => { if (modalBot) safe(renderBotModal); }, 15000);

// daily quota (titulares): completed buy+sell round trips today on the real account
function quotaLine(q) {
  if (!q) return "";
  const w = Math.min(q.done / q.target, 1) * 100;
  return `<div class="gl${q.done >= q.target ? " ok" : ""}" title="Mínimo de ${q.target} operações completas (compra e venda) por dia na conta real. Atrasado, o titular solta um pouco a entrada e mira um alvo mais perto.">
    <span>⏱ ${q.done}/${q.target} operações hoje</span><i style="width:${w.toFixed(0)}%"></i></div>`;
}

// self-learning: what the bot learned from its own closed trades (it skips situations it keeps losing in)
function learnedLine(l, h) {
  const passes = h && (h.sent || h.received) ? ` · 📨 ${h.sent}↗ ${h.received}↙` : "";
  if (!l || !l.seen) return passes ? `<div class="gl learn">🧠 aprendendo${passes}</div>` : "";
  const fmt = (x) => `${esc(x.tag)} (${x.wins}/${x.n}, ${x.pnl >= 0 ? "+" : "−"}$${Math.abs(x.pnl).toFixed(2)})`;
  const tip = [l.avoid.length ? `Evita: ${l.avoid.map(fmt).join(", ")}` : "", l.best.length ? `Vai melhor em: ${l.best.map(fmt).join(", ")}` : ""]
    .filter(Boolean).join(" · ") || "ainda sem padrão claro — precisa de 6+ trades na mesma situação";
  const txt = l.avoid.length ? `evita ${l.avoid.slice(0, 2).map((x) => esc(x.tag)).join(", ")}`
    : l.best.length ? `melhor em ${l.best.slice(0, 2).map((x) => esc(x.tag)).join(", ")}` : `aprendendo · ${l.seen} trades analisados`;
  return `<div class="gl learn" title="${esc(tip)}">🧠 ${txt}${passes}</div>`;
}
function trainButton(model, r) {
  if (!r?.green_light || r.green_light.ok) return "";
  const until = r.train_boost_until && new Date(r.train_boost_until) > new Date() ? r.train_boost_until : null;
  const scan = r.ai_full_scan_until && new Date(r.ai_full_scan_until) > new Date() ? `<div class="gl ok">🧠 analisa as 30 moedas a cada hora até ${hhmm(r.ai_full_scan_until)}</div>` : "";
  const turbo = r.turbo_until && new Date(r.turbo_until) > new Date() ? r.turbo_until : null;
  return scan + (turbo ? `<button class="tboost on" disabled title="analisa sem parar até a meia-noite">⚡ turbo até 00:00</button>`
    : `<button class="tboost" onclick="event.stopPropagation(); trainBoost('${esc(model)}', this)">⚡ Turbo hoje</button>`);
}
function trainBoost(model, btn) {
  if (REMOTE || !btn || btn.disabled) return;
  if (!confirm(`Turbinar o ${look(model).label} até a meia-noite? No laboratório (dinheiro falso) ele passa a ler o mercado a cada 2 minutos, com todas as vagas e sem limite diário,. As regras de compra, stops, alvos e o tempo de cada posição não mudam.`)) return;
  run(btn, "Turbinando…", () => api("/arena/turbo", "POST", { bot: model }),
      `⚡ ${look(model).label} turbinado até a meia-noite: analisa o mercado a cada minuto`);
}

safe(refresh); safe(loadArena);
setInterval(() => safe(loadArena), 5000);
setInterval(() => safe(refresh), 2000);

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

// free collateral: the exchange's own numbers on the real account, the ledger's on the simulation
const availableOf = (a) => (KIND === "real" && a.exchange
  ? { available: a.exchange.available, margin: a.exchange.margin_used }
  : { available: a.available, margin: a.margin_used });

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
    ["Disponível", `<span class="green">${usd(availableOf(a).available)}</span>`],
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
  term.innerHTML = rows.length ? rows.map((l) => `<div class="l ${l.level}"><span class="t">${new Date(l.ts).toLocaleTimeString("pt-BR", { timeZone: TZ })}</span> <span class="s ${esc(l.source.split(":")[0])}">[${esc(l.source)}]</span> ${l.level.padEnd(5)} ${esc(l.msg)}</div>`).join("")
    : '<div class="l DEBUG">aguardando logs… (os bots escrevem aqui a cada passo de cada ciclo)</div>';
  if (atBottom || force) term.scrollTop = term.scrollHeight;
}
async function loadLogs() {
  const r = await api(`/logs?after=${logLast}&limit=1000`);
  if (!r.lines.length) return;
  logLast = r.last;
  logLines = logLines.concat(r.lines).slice(-2000);
  ecgFeed(r.lines);
  renderLogs(false);
}

// ─── vital signs: an ECG of the bots' real activity (one beat per log event, shaped by its level) ───
const ecg = { queue: [], events: [], y: [], x: 0, lastBeat: 0, live: false };
const ECG_KIND = { TRADE: { amp: 1.0, color: "#34d399" }, WARN: { amp: 0.55, color: "#fbbf24" }, ERROR: { amp: -0.7, color: "#f87171" },
                   INFO: { amp: 0.32, color: "#7dd3fc" }, DEBUG: { amp: 0.14, color: "#3f5f55" } };
function ecgFeed(lines) {
  const now = Date.now();
  for (const l of lines) {
    if (l.level === "DEBUG") continue;                            // per-coin checks are noise, not actions
    const t = new Date(l.ts).getTime() || now;
    ecg.events.push({ t, level: l.level });
    if (now - t < 60e3 && ecg.queue.length < 40) ecg.queue.push(l.level);   // only fresh actions beat (no backlog)
  }
  ecg.events = ecg.events.filter((e) => now - e.t < 10 * 60e3);
}
function ecgStats() {
  const now = Date.now(), last = ecg.events.length ? Math.max(...ecg.events.map((e) => e.t)) : 0;
  const perMin = ecg.events.filter((e) => now - e.t < 60e3).length;
  const age = last ? Math.round((now - last) / 1000) : null;
  ecg.live = age !== null && age < 300;
  document.getElementById("ecg-bpm").textContent = perMin;
  const st = document.getElementById("ecg-state");
  st.textContent = ecg.live ? "● VIVO — bots trabalhando" : "■ SEM SINAL há mais de 5 min";
  st.className = `tag ecg-state ${ecg.live ? "live" : "dead"}`;
  const n = (lvl) => ecg.events.filter((e) => e.level === lvl && now - e.t < 10 * 60e3).length;
  const lc = acc?.last_cycle;
  setHTML("ecg-stats", [
    ["Última ação", age === null ? "–" : age < 60 ? `há ${age}s` : `há ${Math.round(age / 60)} min`],
    ["Ações (10 min)", ecg.events.length], ["Compras/vendas (10 min)", n("TRADE")], ["Vetos/avisos (10 min)", n("WARN")],
    ["Erros (10 min)", n("ERROR")], ["Último ciclo da conta", lc ? `#${lc.cycle} · ${hhmm(lc.finished)}` : "–"],
  ].map(([k, v]) => `<span>${k}<b>${v}</b></span>`).join(""));
}
function ecgDraw() {
  const c = document.getElementById("ecg-canvas");
  if (!c || document.getElementById("ecg").classList.contains("collapsed")) return requestAnimationFrame(ecgDraw);
  const w = c.clientWidth, h = c.clientHeight, dpr = window.devicePixelRatio || 1;
  if (c.width !== w * dpr) { c.width = w * dpr; c.height = h * dpr; ecg.y = new Array(w).fill(null); ecg.x = 0; }
  const g = c.getContext("2d"); g.setTransform(dpr, 0, 0, dpr, 0, 0);
  const mid = h * 0.6, now = performance.now();
  // advance 2px per frame; start a beat when one is queued and the previous finished
  for (let s = 0; s < 2; s++) {
    let v = 0;
    if (ecg.beat) {
      const k = ecg.beat.k++, amp = ECG_KIND[ecg.beat.level]?.amp ?? 0.2;
      // P-QRS-T shape over ~34px
      v = k < 6 ? Math.sin(k / 6 * Math.PI) * 0.08 : k < 9 ? -0.12 * (k - 5) / 3 : k < 12 ? amp * (k - 8) / 3 : k < 15 ? amp * (15 - k) / 3 - 0.1 : k < 22 ? 0 : k < 32 ? Math.sin((k - 22) / 10 * Math.PI) * 0.12 : 0;
      ecg.color = ECG_KIND[ecg.beat.level]?.color || "#7dd3fc";
      if (k > 34) ecg.beat = null;
    } else if (ecg.queue.length && now - ecg.lastBeat > 120) {
      ecg.beat = { level: ecg.queue.shift(), k: 0 }; ecg.lastBeat = now;
    }
    const noise = ecg.live ? (Math.random() - 0.5) * 0.02 : 0;
    ecg.y[ecg.x] = { v: v + noise, color: ecg.beat ? ecg.color : (ecg.live ? "#2f6b55" : "#5b2b2b") };
    ecg.x = (ecg.x + 1) % w;
  }
  g.fillStyle = "#050706"; g.fillRect(0, 0, w, h);
  g.strokeStyle = "rgba(52,211,153,.06)"; g.lineWidth = 1;
  for (let gx = 0; gx < w; gx += 20) { g.beginPath(); g.moveTo(gx, 0); g.lineTo(gx, h); g.stroke(); }
  for (let gy = 0; gy < h; gy += 20) { g.beginPath(); g.moveTo(0, gy); g.lineTo(w, gy); g.stroke(); }
  g.lineWidth = 2; g.lineJoin = "round";
  for (let i = 1; i < w; i++) {
    const a = ecg.y[i - 1], b = ecg.y[i];
    if (!a || !b || i === ecg.x) continue;
    g.strokeStyle = b.color; g.beginPath(); g.moveTo(i - 1, mid - a.v * h * 0.55); g.lineTo(i, mid - b.v * h * 0.55); g.stroke();
  }
  g.fillStyle = "#e8eeeb"; g.beginPath(); g.arc(ecg.x, mid - ((ecg.y[(ecg.x - 1 + w) % w]?.v) || 0) * h * 0.55, 3, 0, 7); g.fill();   // the "pen"
  requestAnimationFrame(ecgDraw);
}
if (window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) setInterval(() => safe(ecgStats), 3000);
else { requestAnimationFrame(ecgDraw); setInterval(() => safe(ecgStats), 2000); }
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
      ${coin(c)} ${WHALE_PT[s.pressure] || ""}
      <div class="bar2"><i class="b" style="width:${(h.buy_usd / tot * 100).toFixed(0)}%"></i><i class="s" style="width:${(h.sell_usd / tot * 100).toFixed(0)}%"></i></div>
      ${kusd(h.buy_usd)} / ${kusd(h.sell_usd)} · ${h.n} ordens</div>`;
  };
  setHTML("whale-flows", active.length ? active.slice(0, 12).map(flowCard).join("") : '<div class="empty">Ainda sem ordens de baleia nas nossas moedas desde que o feed ligou.</div>');
  setHTML("whale-market", w.market.length ? w.market.map((m) => flowCard([m.coin, m])).join("") : '<div class="empty">Sem baleias no resto do mercado na última hora.</div>');
  table("whale-recent", ["Quando", "Moeda", "Lado", "Valor", "Carteira"], w.recent.map((t) => `<tr>
    <td>${hhmm(new Date(t.ts * 1000).toISOString())}</td><td>${coin(t.coin)}</td>
    <td class="${t.side === "compra" ? "green" : "red"}">${t.side}</td><td>${kusd(t.usd)}</td>
    <td class="muted">${t.wallet ? `<a href="https://hypurrscan.io/address/${esc(t.wallet)}" target="_blank" rel="noopener">${short(t.wallet)}</a>` : "–"}</td></tr>`));
  table("whale-wallets", ["Carteira", "Comprou", "Vendeu", "Líquido", "Moedas", "Ordens"], (w.wallets || []).map((x) => `<tr>
    <td><a href="https://hypurrscan.io/address/${esc(x.wallet)}" target="_blank" rel="noopener">${short(x.wallet)}</a></td>
    <td class="green">${kusd(x.buy_usd)}</td><td class="red">${kusd(x.sell_usd)}</td><td>${x.net_usd >= 0 ? "🟢" : "🔴"} ${kusd(x.net_usd)}</td>
    <td class="muted">${x.coins.map(([c, v]) => `${esc(c)} ${v >= 0 ? "+" : "−"}${kusd(Math.abs(v))}`).join(" · ")}</td><td>${x.n}</td></tr>`));
  if (acc) renderRadar(acc);
}
safe(loadWhales);
setInterval(() => safe(loadWhales), 10000);

// ─── charts (TradingView lightweight-charts): candles + EMAs + entry/stop/target + the bots' buy levels ───
let eqChart = null, eqS = null, btcS = null;
const CHART_OPTS = { layout: { background: { color: "transparent" }, textColor: "#8a9690" },
  grid: { vertLines: { color: "rgba(255,255,255,.04)" }, horzLines: { color: "rgba(255,255,255,.04)" } },
  rightPriceScale: { borderColor: "#222a26" }, timeScale: { borderColor: "#222a26", timeVisible: true }, autoSize: true };
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
setTimeout(() => safe(loadEquityChart), 2500);
setInterval(() => safe(loadEquityChart), 60000);


// ─── collapsible sections (remembered in this browser) ───
// secondary panels start minimized on a first visit; after that the user's own choice is remembered
let folded = ["baleias", "toptraders", "transacoes", "relatorio", "fundos", "conexoes"];
try { const saved = localStorage.getItem("folded2"); if (saved) folded = JSON.parse(saved); } catch (e) { /* storage blocked */ }
document.querySelectorAll("main section.panel").forEach((sec, i) => {
  const h2 = sec.querySelector("h2");
  if (!h2) return;
  const key = sec.id || `sec${i}`;
  const btn = document.createElement("button");
  btn.className = "fold";
  btn.textContent = "▾";
  btn.setAttribute("aria-label", "Minimizar ou expandir");
  btn.setAttribute("aria-expanded", "true");
  btn.onclick = () => {
    const on = sec.classList.toggle("collapsed");
    btn.setAttribute("aria-expanded", String(!on));
    folded = on ? [...new Set([...folded, key])] : folded.filter((k) => k !== key);
    try { localStorage.setItem("folded2", JSON.stringify(folded)); } catch (e) { /* ignore */ }
  };
  h2.appendChild(btn);
  if (folded.includes(key)) { sec.classList.add("collapsed"); btn.setAttribute("aria-expanded", "false"); }
});

// ─── history analysis + transactions ───
const BOT_PT = (s) => (s === "manual" ? "Manual" : botName(s));
async function loadHistory() {
  const [s, tx] = await Promise.all([api(`/account/stats?kind=${KIND}`), api(`/account/transactions?kind=${KIND}`)]);
  document.getElementById("stats-tag").textContent = s.trades ? `${s.trades} trades fechados · ${KIND === "sim" ? "simulação" : "real"}` : "ainda sem trades fechados";
  const k = (label, v, sub) => `<div class="goal"><div class="k">${label}</div><div class="v">${v}</div><div class="s">${sub}</div></div>`;
  setHTML("stats-kpis", s.trades ? [
    k("Resultado", pnl(s.pnl_usd), `${s.wins} ganhos · ${s.losses} perdas · acerto ${pct(s.win_rate)}`),
    k("Média por trade", pnl(s.expectancy), `ganho médio ${s.avg_win == null ? "–" : pnl(s.avg_win)} · perda média ${s.avg_loss == null ? "–" : pnl(s.avg_loss)}`),
    k("Fator de lucro", s.profit_factor ?? "∞", "ganhos ÷ perdas (acima de 1 = lucrativo)"),
    k("Duração média", `${s.avg_hold_h}h`, `taxas + funding pagos ${usd(s.fees_usd, 3)}`),
    k("Melhor / pior", `${pnl(s.best.pnl_usd)} / ${pnl(s.worst.pnl_usd)}`, `${esc(s.best.symbol)} / ${esc(s.worst.symbol)}`),
  ].join("") : '<div class="empty">Quando os primeiros trades fecharem, aparecem aqui o acerto, o fator de lucro e o resultado por bot, moeda e tipo de saída.</div>');
  const grp = (id, g, name) => table(id, ["", "Trades", "Acerto", "Resultado"], Object.entries(g || {}).map(([key, r]) =>
    `<tr><td>${name ? `<b>${esc(name(key))}</b>` : coin(key)}</td><td>${r.trades}</td><td>${pct(r.wins / r.trades)}</td><td>${pnl(r.pnl_usd)}</td></tr>`));
  grp("stats-bot", s.by_bot, BOT_PT); grp("stats-coin", s.by_coin, null); grp("stats-exit", s.by_exit, (x) => x);
  document.getElementById("tx-tag").textContent = `${tx.rows.length} movimentos`;
  table("tx-table", ["Quando", "Tipo", "Ativo", "Valor", "Preço", "Taxa", "Bot / nota"], tx.rows.map((r) => `<tr>
    <td>${time(r.ts)}</td><td class="${r.type === "compra" || r.type === "depósito" ? "green" : "red"}">${r.type}</td><td>${r.symbol === "USDC" ? coin("USDC") : coin(r.symbol)}</td>
    <td>${usd(r.usd)}</td><td>${r.price ? fmtPx(r.price) : "–"}</td><td>${r.fee_usd ? usd(r.fee_usd, 4) : "–"}</td>
    <td class="muted">${esc(r.bot ? BOT_PT(r.bot) : r.note || "")}</td></tr>`));
}
safe(loadHistory);
setInterval(() => safe(loadHistory), 30000);

// ─── training center: virtual trades and what they teach ───

// ─── confidence: is the real account proven enough to add money? ───
async function loadConfidence() {
  const c = await api("/account/confidence?kind=real");
  const tag = document.getElementById("conf-tag");
  tag.textContent = c.ready ? "✅ sim — pode recarregar" : `${c.passed} de ${c.total} critérios · ainda não`;
  tag.style.color = c.ready ? "var(--green)" : "";
  setHTML("conf", c.checks.map((x) => `<div class="${x.ok ? "ok" : ""}">${x.ok ? "✅" : "⬜"} ${esc(x.name)}<small>${esc(x.value)}</small></div>`).join(""));
}
safe(loadConfidence);
setInterval(() => safe(loadConfidence), 60000);

// ─── add funds: address, QR, balances ───
async function loadFunding() {
  const f = await api("/funding");
  document.getElementById("fund-addr").textContent = f.address;
  if (window.qrcode && !document.getElementById("fund-qr").innerHTML) {
    const q = qrcode(0, "M"); q.addData(f.address); q.make();
    document.getElementById("fund-qr").innerHTML = q.createSvgTag({ cellSize: 4, margin: 0, scalable: true });
  }
  document.getElementById("fund-bal").innerHTML = `<div>USDC na MetaMask (Arbitrum)<b>${f.arbitrum_usdc == null ? "–" : Number(f.arbitrum_usdc).toFixed(2)}</b></div>
    <div>ETH para taxas<b>${f.arbitrum_eth == null ? "–" : Number(f.arbitrum_eth).toFixed(5)}</b></div>
    <div>Na Hyperliquid<b>${usd(f.hyperliquid)}</b></div>`;
}
function copyAddr(btn) {
  navigator.clipboard.writeText(document.getElementById("fund-addr").textContent)
    .then(() => toast("📋 Endereço copiado — envie só USDC na rede Arbitrum One"), () => toast("✖ não foi possível copiar", true));
}
safe(loadFunding);
// only while the "Adicionar fundos" panel is open: it reads the MetaMask side on Arbitrum
setInterval(() => { if (!document.getElementById("fundos")?.classList.contains("collapsed")) safe(loadFunding); }, 120000);

// ─── top 10 traders of Hyperliquid: who, what they hold, what they just did ───
let lastTop = null;
async function loadTop() {
  const t = await api("/toptraders");
  lastTop = t;
  const st = t.status || {};
  document.getElementById("top-tag").textContent = t.traders.length ? `${t.traders.length} seguidos · posições a cada 2 min` : (st.error || "carregando ranking…");
  table("top-table", ["#", "Trader", "Conta", "Lucro 30d", "ROI 30d / 7d", "Maiores posições agora"], t.traders.map((x, i) => `<tr>
    <td>${i + 1}</td><td><a href="https://hypurrscan.io/address/${esc(x.address)}" target="_blank" rel="noopener">${esc(x.name || short(x.address))}</a></td>
    <td>${kusd(x.account_usd)}</td><td class="green">+${kusd(x.pnl_30d)}</td><td>${pct(x.roi_30d)} / ${pct(x.roi_7d)}</td>
    <td>${x.positions.length ? x.positions.map((p) => `<span class="pos-chip ${p.szi > 0 ? "long" : "short"}">${coinIcon(p.coin)}${esc(p.coin)} ${p.szi > 0 ? "▲" : "▼"} ${kusd(p.value)}${p.lev ? ` ${p.lev}x` : ""}</span>`).join("") : '<span class="muted">sem posições</span>'}</td></tr>`));
  table("top-moves", ["Quando", "Trader", "Moeda", "Movimento", "Tamanho"], t.events.map((e) => `<tr>
    <td>${hhmm(new Date(e.ts * 1000).toISOString())}</td><td>${short(e.address)}</td><td>${coin(e.coin)}</td>
    <td class="${e.what.includes("compra") && !e.what.includes("fechou") ? "green" : "red"}">${esc(e.what)}</td><td>${kusd(e.usd)}</td></tr>`));
  if (acc) renderRadar(acc);
}
safe(loadTop);
setInterval(() => safe(loadTop), 30000);

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

