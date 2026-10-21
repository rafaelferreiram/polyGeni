// Kairos admin: read-only metrics from GET /api/paper/admin (works from the Mac and, after login, from anywhere)
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const usd = (v, d = 2) => (v === null || v === undefined ? "–" : `$${Number(v).toFixed(d)}`);
const pnl = (v, d = 2) => {
  if (v === null || v === undefined) return "–";
  const n = Number(v);
  return `<span class="${n >= 0 ? "green" : "red"}">${n >= 0 ? "+" : "−"}$${Math.abs(n).toFixed(d)}</span>`;
};
const pct = (v, d = 0) => (v === null || v === undefined ? "–" : `${(v * 100).toFixed(d)}%`);
const set = (id, html) => { document.getElementById(id).innerHTML = html; };
const kpi = (l, v, s = "") => `<div class="kpi"><div class="l">${l}</div><div class="v">${v}</div><div class="s">${s}</div></div>`;
const NAMES = { rules: "São Marcos", trend: "Endrick", trend4h: "Gabriel Jesus", whales: "Evair", whales2: "Dudu", copy: "Veiga",
  whales_15m: "Evair 15m", copy_15m: "Veiga 15m", "claude-haiku-5-5": "Valdivia", trend4h_15m: "Gabriel Jesus 15m", short: "Estêvão",
  manual: "Manual", coordinator: "Coordenador" };
const WHO = { debate: "Revisor (comitê)", llm: "IA da conta", "?": "Rotinas / outros", "llm:claude-haiku-5-5": "Valdivia (bot de IA)" };
const name = (m) => NAMES[m] || NAMES[String(m).replace(/^llm:/, "")] || m;

function accountTable(a) {
  const s = a.stats || {};
  const rows = [["Saldo", usd(a.equity)], ["Aportado", usd(a.contributed)], ["Lucro (com posições abertas)", pnl(a.profit)],
    ["Em posições", `${usd(a.exposure)} · ${a.positions} abertas`], ["Trades fechados", `${s.trades || 0} · acerto ${pct(s.win_rate)}`],
    ["Lucro dos trades fechados", pnl(s.pnl_usd)], ["Média por trade", pnl(s.expectancy, 3)],
    ["Ganho médio / perda média", `${pnl(s.avg_win, 3)} / ${pnl(s.avg_loss, 3)}`], ["Fator de lucro", s.profit_factor ?? "–"],
    ["Taxas + funding pagos", usd(s.fees_usd, 3)], ["Tempo médio por trade", s.avg_hold_h != null ? `${s.avg_hold_h}h` : "–"],
    ["Compras por dia (7d)", a.entries_per_day_7d]];
  const byBot = Object.entries(s.by_bot || {}).map(([b, r]) => [`↳ ${name(b)}`, `${r.trades} trades · ${r.wins} vitórias · ${pnl(r.pnl_usd)}`]);
  return [...rows, ...byBot].map(([l, v]) => `<tr><td class="muted">${l}</td><td class="n">${v}</td></tr>`).join("");
}

function render(d) {
  document.getElementById("ts").textContent = `atualizado ${new Date(d.ts).toLocaleTimeString("pt-BR", { timeZone: "Europe/Lisbon" })} (Lisboa)`;
  const tradeReal = d.real.stats.pnl_usd || 0;
  set("money", kpi("Conta real", usd(d.real.equity), `aportado ${usd(d.real.contributed)}`) +
    kpi("Lucro real", pnl(d.real.profit), `trades fechados ${pnl(tradeReal)}`) +
    kpi("Custo de IA (total)", usd(d.ai.total), `${d.ai.calls_30d} chamadas em 30 dias`) +
    kpi("Lucro líquido", pnl(tradeReal - d.ai.total), "trades reais fechados − IA"));

  const sc = d.scale;
  set("advice", sc.advice.length ? sc.advice.map((a) => `<li>${esc(a)}</li>`).join("") : "<li>Nada a mudar agora.</li>");
  set("checks", (sc.confidence.checks || []).map((c) => `<div>${c.ok ? "✅" : "⬜"} ${esc(c.name)}<br><span class="muted">${esc(c.value)}</span></div>`).join(""));
  set("scale-note", `· base: ${esc(sc.source)} (${sc.sample} trades), ${pct(sc.ret_per_trade, 2)} por trade, ${sc.trades_per_day} trades/dia, IA ≈ ${usd(sc.ai_month)}/mês · uso do capital hoje ${pct(sc.utilization)}`);
  set("scen", `<tr><th>Capital</th><th class="n">Posição</th><th class="n">Trades/mês</th><th class="n">Lucro líquido/mês</th></tr>` +
    sc.scenarios.map((s, i) => `<tr><td>${usd(s.capital, 0)}${i === 0 ? ' <span class="muted">(hoje)</span>' : ""}</td><td class="n">${usd(s.position_usd)}</td>
      <td class="n">${pnl(s.month_trading)}</td><td class="n">${pnl(s.month_net)}</td></tr>`).join(""));

  const ai = d.ai;
  const used = ai.allowance ? ai.month / ai.allowance : 0;
  set("ai-kpis", kpi("Hoje (24h)", usd(ai.d1)) + kpi("7 dias", usd(ai.d7)) + kpi("Este mês", usd(ai.month),
    `<div class="bar ${used > 0.9 ? "bad" : used > 0.7 ? "warn" : ""}" style="margin-top:6px"><i style="width:${Math.min(used, 1) * 100}%"></i></div>${pct(used)} do limite`) +
    kpi("Limite do mês", usd(ai.allowance), "base + 25% do lucro real"));
  const costRows = (rows, label) => `<tr><th>${label}</th><th class="n">Chamadas</th><th class="n">Custo</th><th class="n">Por chamada</th><th class="n">Tokens (in/out)</th></tr>` +
    (rows.length ? rows.map((r) => `<tr><td>${esc(r.label)}</td><td class="n">${r.calls}</td><td class="n">${usd(r.usd, 3)}</td>
      <td class="n">${usd(r.calls ? r.usd / r.calls : 0, 4)}</td><td class="n">${(r.in_tokens / 1000).toFixed(0)}k / ${(r.out_tokens / 1000).toFixed(0)}k</td></tr>`).join("")
      : `<tr><td class="empty" colspan="5">Sem gastos ainda.</td></tr>`);
  set("ai-model", costRows(ai.by_model.map((r) => ({ ...r, label: r.key })), "Modelo"));
  set("ai-who", costRows(ai.by_who.map((r) => ({ ...r, label: WHO[r.key] || name(r.key) })), "Uso"));
  const days = [...ai.by_day].reverse(), max = Math.max(...days.map((x) => x.usd), 0.0001);
  set("ai-days", days.length ? days.map((x) => `<div title="${x.key}: ${usd(x.usd, 3)} · ${x.calls} chamadas"><small>${usd(x.usd)}</small>
    <span style="height:${(x.usd / max) * 80}px"></span><small>${x.key.slice(8, 10)}/${x.key.slice(5, 7)}</small></div>`).join("") : '<div class="empty">Sem gastos.</div>');

  set("acc-real", accountTable(d.real));
  set("acc-sim", accountTable(d.sim));
  const lab = [...d.lab].sort((a, b) => b.net - a.net);
  set("lab", `<tr><th>Bot</th><th>Status</th><th class="n">Trades</th><th class="n">Acerto</th><th class="n">Média/trade</th><th class="n">Retorno/trade</th>
    <th class="n">Fator</th><th class="n">Trades/dia</th><th class="n">IA</th><th class="n">Lucro líquido</th><th>Rumo à conta real</th></tr>` +
    lab.map((b) => {
      const g = b.green_light || {};
      const prog = g.trusted || g.ok ? 1 : Math.min((g.trades || 0) / (g.needed_trades || 20), (g.wins || 0) / (g.needed || 12));
      return `<tr><td><b>${esc(b.label || name(b.model))}</b></td><td>${g.trusted || g.ok ? "⚽ titular" : "🪑 reserva"}</td>
        <td class="n">${b.trades || 0}</td><td class="n">${pct(b.win_rate)}</td><td class="n">${pnl(b.expectancy, 3)}</td>
        <td class="n">${pct(b.ret_per_trade, 2)}</td><td class="n">${b.profit_factor ?? "–"}</td><td class="n">${b.trades_per_day}</td>
        <td class="n">${usd(b.ai_usd, 3)}</td><td class="n">${pnl(b.net)}</td>
        <td><div class="bar"><i style="width:${prog * 100}%"></i></div></td></tr>`;
    }).join(""));
}

// ─── live: the real account straight from Hyperliquid, every 10s ───
const fmtPx = (v) => (v == null ? "–" : v >= 1000 ? v.toLocaleString("pt-BR", { maximumFractionDigits: 1 }) : v >= 1 ? v.toFixed(3) : v.toPrecision(4));
let lastPrices = {};
function renderLive(d) {
  const dot = document.getElementById("live-dot");
  dot.className = "live pulse";
  void dot.offsetWidth;                       // restart the pulse
  document.getElementById("live-ts").textContent = `${d.network} · ${new Date(d.ts * 1000).toLocaleTimeString("pt-BR", { timeZone: "Europe/Lisbon" })}`;
  set("live-kpis", kpi("Saldo na corretora", usd(d.collateral), `livre ${usd(d.available)}`) +
    kpi("Margem em uso", usd(d.margin_used), `${d.positions.length} posições`) +
    kpi("Resultado aberto", pnl(d.upnl), "lucro/prejuízo não realizado") +
    kpi("Valor das posições", usd(d.positions.reduce((s, p) => s + p.value, 0)), "a preço de agora"));
  set("live-pos", `<tr><th>Moeda</th><th class="n">Qtd</th><th class="n">Entrada</th><th class="n">Agora</th><th class="n">Valor</th>
    <th class="n">Resultado</th><th class="n">%</th><th class="n">Liquidação</th><th class="n">Funding</th></tr>` +
    (d.positions.length ? d.positions.map((p) => `<tr><td><b>${esc(p.coin)}</b> <span class="muted">${p.lev}x</span></td><td class="n">${p.size}</td>
      <td class="n">${fmtPx(p.entry)}</td><td class="n">${fmtPx(p.mark)}</td><td class="n">${usd(p.value)}</td><td class="n">${pnl(p.upnl, 3)}</td>
      <td class="n ${p.roe >= 0 ? "green" : "red"}">${(p.roe * 100).toFixed(2)}%</td><td class="n muted">${fmtPx(p.liq)}</td><td class="n">${pnl(p.funding, 4)}</td></tr>`).join("")
      : `<tr><td class="empty" colspan="9">Nenhuma posição aberta.</td></tr>`));
  set("live-fills", `<tr><th>Quando</th><th>Moeda</th><th>Lado</th><th class="n">Preço</th><th class="n">Valor</th><th class="n">Taxa</th><th class="n">Lucro</th></tr>` +
    d.fills.map((f) => `<tr><td class="muted">${new Date(f.ts * 1000).toLocaleString("pt-BR", { timeZone: "Europe/Lisbon", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })}</td>
      <td><b>${esc(f.coin)}</b></td><td class="${f.side === "compra" ? "" : "yellow"}">${f.side}</td><td class="n">${fmtPx(f.px)}</td><td class="n">${usd(f.usd)}</td>
      <td class="n muted">${usd(f.fee, 4)}</td><td class="n">${f.pnl ? pnl(f.pnl, 3) : "–"}</td></tr>`).join(""));
  set("live-prices", Object.entries(d.prices).map(([c, v]) => {
    const was = lastPrices[c], cls = was == null || was === v ? "" : v > was ? "up" : "down";
    return `<div class="${cls}"><b>${esc(c)}</b><span>${fmtPx(v)}</span></div>`;
  }).join(""));
  lastPrices = d.prices;
}
async function loadLive() {
  const res = await fetch("/api/paper/admin/live");
  if (res.status === 401) { location.href = "/login"; return; }
  if (!res.ok) { document.getElementById("live-dot").className = "live off"; document.getElementById("live-ts").textContent = "sem conexão com a Hyperliquid"; return; }
  renderLive(await res.json());
}
loadLive().catch(() => {});
setInterval(() => loadLive().catch(() => {}), 10000);

async function load() {
  const res = await fetch("/api/paper/admin");
  if (res.status === 401) { location.href = "/login"; return; }
  render(await res.json());
}
load().catch((e) => { document.getElementById("ts").textContent = `erro: ${e.message}`; });
setInterval(() => load().catch(() => {}), 30000);
