const API = "/api";

async function api(path, method = "GET", body = null) {
  const opts = { method, headers: { "Content-Type": "application/json" } };
  if (body) opts.body = JSON.stringify(body);
  const res = await fetch(API + path, opts);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

function fmt(val, decimals = 2) {
  if (val === null || val === undefined) return "–";
  return Number(val).toFixed(decimals);
}

function fmtUSDC(val) {
  if (val === null || val === undefined) return "–";
  const n = Number(val);
  const cls = n >= 0 ? "green" : "red";
  return `<span class="${cls}">$${n >= 0 ? "+" : ""}${n.toFixed(2)}</span>`;
}

const SPORT_ICONS = {
  bitcoin:  "₿",
  sports:   "🏆",
  events:   "🌐",
  soccer:   "⚽",
  nba:      "🏀",
  ncaab:    "🏀",
  nfl:      "🏈",
  nhl:      "🏒",
  mlb:      "⚾",
  mma:      "🥊",
  esports:  "🎮",
  tennis:   "🎾",
  politics: "🗳️",
};

function categoryTag(cat) {
  const key = cat ? cat.toLowerCase() : "";
  const icon = SPORT_ICONS[key] || "📊";
  return `<span class="tag tag-${key}">${icon} ${cat}</span>`;
}

// ─── Portfolio ────────────────────────────────────────────────────────────────
async function loadPortfolio() {
  try {
    const p = await api("/portfolio");
    document.getElementById("portfolio-value").textContent = `$${fmt(p.portfolio_value ?? p.balance_usdc)}`;
    document.getElementById("balance").textContent = `$${fmt(p.balance_usdc)}`;
    document.getElementById("invested").textContent = `$${fmt(p.currently_invested)}`;
    document.getElementById("total-pnl").innerHTML = fmtUSDC(p.total_pnl);
    document.getElementById("unrealized-pnl").innerHTML = fmtUSDC(p.unrealized_pnl);
    document.getElementById("win-rate").textContent =
      p.win_rate !== null ? `${(p.win_rate * 100).toFixed(0)}%` : "–";
  } catch (e) {
    console.error("portfolio", e);
  }
}

// ─── Status ───────────────────────────────────────────────────────────────────
async function loadStatus() {
  try {
    const s = await api("/status");
    const badge = document.getElementById("bot-status-badge");
    if (s.running && s.auto_trade) {
      badge.textContent = "AUTO-TRADE ON";
      badge.className = "badge badge-auto";
    } else if (s.running) {
      badge.textContent = "BOT ON";
      badge.className = "badge badge-on";
    } else {
      badge.textContent = "BOT OFF";
      badge.className = "badge badge-off";
    }
  } catch (e) {}
}

// ─── Opportunities ────────────────────────────────────────────────────────────
async function loadOpportunities() {
  try {
    const opps = await api("/opportunities/live");
    const el = document.getElementById("opportunities-list");
    document.getElementById("opp-count").textContent = opps.length;

    if (!opps.length) {
      el.innerHTML = '<div class="muted" style="padding:12px">No opportunities found yet. Run a scan.</div>';
      return;
    }

    el.innerHTML = opps.slice(0, 15).map(o => `
      <div class="item">
        <div class="item-header">
          <div class="item-question">${o.question}</div>
          <span class="tag tag-${o.recommended_side.toLowerCase()}">${o.recommended_side}</span>
        </div>
        <div class="item-meta">
          ${categoryTag(o.category)}
          <span class="tag">Edge ${(o.edge * 100).toFixed(1)}%</span>
          <span class="tag">Market ${(o.market_prob * 100).toFixed(1)}%</span>
          <span class="tag">Model ${(o.estimated_prob * 100).toFixed(1)}%</span>
          <span class="tag">Kelly $${fmt(o.kelly_size_usdc)}</span>
        </div>
        <div class="edge-bar"><div class="edge-fill" style="width:${Math.min(o.edge * 500, 100)}%"></div></div>
        <div class="reasoning">${o.reasoning}</div>
      </div>
    `).join("");
  } catch (e) {
    console.error("opportunities", e);
  }
}

// ─── Positions ────────────────────────────────────────────────────────────────
async function loadPositions() {
  try {
    const positions = await api("/positions");
    const el = document.getElementById("positions-list");

    if (!positions.length) {
      el.innerHTML = '<div class="muted" style="padding:12px">No open positions.</div>';
      return;
    }

    el.innerHTML = positions.map(p => `
      <div class="item">
        <div class="item-header">
          <div class="item-question">${p.question}</div>
          <span class="tag tag-${p.side.toLowerCase()}">${p.side}</span>
        </div>
        <div class="item-meta">
          ${categoryTag(p.category)}
          <span class="tag">${fmt(p.shares)} shares @ $${fmt(p.avg_price)}</span>
          <span class="tag">Cost $${fmt(p.cost_basis)}</span>
          <span class="tag">Value $${fmt(p.current_value)}</span>
        </div>
        <div style="margin-top:6px">PnL: ${fmtUSDC(p.unrealized_pnl)}</div>
      </div>
    `).join("");
  } catch (e) {
    console.error("positions", e);
  }
}

// ─── Trades ───────────────────────────────────────────────────────────────────
async function loadTrades() {
  try {
    const trades = await api("/trades?limit=30");
    const el = document.getElementById("trades-list");

    if (!trades.length) {
      el.innerHTML = '<div class="muted" style="padding:12px">No trades yet.</div>';
      return;
    }

    el.innerHTML = trades.map(t => `
      <div class="item">
        <div class="item-header">
          <div class="item-question">${t.question}</div>
          <span class="tag tag-${t.status}">${t.status.toUpperCase()}</span>
        </div>
        <div class="item-meta">
          ${categoryTag(t.category)}
          <span class="tag tag-${t.side.toLowerCase()}">${t.side}</span>
          <span class="tag">$${fmt(t.usdc_spent)} @ ${(t.price * 100).toFixed(1)}¢</span>
          <span class="tag">Edge ${(t.edge * 100).toFixed(1)}%</span>
          ${t.pnl !== 0 ? `<span class="tag">${fmtUSDC(t.pnl)}</span>` : ""}
        </div>
        <div class="muted" style="margin-top:4px;font-size:11px">${new Date(t.created_at).toLocaleString()}</div>
      </div>
    `).join("");
  } catch (e) {
    console.error("trades", e);
  }
}

// ─── Bot Controls ─────────────────────────────────────────────────────────────
async function startBot(autoTrade) {
  try {
    await api(`/bot/start?auto_trade=${autoTrade}`, "POST");
    await refresh();
  } catch (e) {
    alert("Error starting bot: " + e.message);
  }
}

async function stopBot() {
  try {
    await api("/bot/stop", "POST");
    await loadStatus();
  } catch (e) {
    alert("Error stopping bot: " + e.message);
  }
}

async function triggerScan() {
  const btn = document.getElementById("btn-scan");
  btn.disabled = true;
  btn.textContent = "Scanning…";
  try {
    await api("/bot/scan", "POST");
    await refresh();
  } catch (e) {
    alert("Scan error: " + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "Scan Now";
  }
}

// ─── Thinking log ─────────────────────────────────────────────────────────────
async function loadThinking() {
  try {
    const data = await api("/thinking");
    const { goal, balance_usdc, progress, log } = data;

    // Update cycle badge
    document.getElementById("cycle-badge").textContent = `Cycle #${goal.cycle}`;

    // Update goal progress (uses full portfolio value)
    const pct = progress !== null ? Math.round(progress * 100) : 0;
    const portfolio_value = data.portfolio_value ?? balance_usdc;
    document.getElementById("goal-fill").style.width = `${pct}%`;
    document.getElementById("goal-pct").textContent = `${pct}% — $${fmt(portfolio_value)} of $${fmt(goal.target_usdc)}`;

    // Render log entries
    const el = document.getElementById("thinking-log");
    if (!log.length) {
      el.innerHTML = '<div class="muted" style="padding:8px;font-size:11px">Waiting for first scan...</div>';
      return;
    }
    el.innerHTML = log.slice(0, 50).map(entry => {
      const t = new Date(entry.timestamp + "Z").toLocaleTimeString();
      return `<div class="thought thought-${entry.type}">
        <div class="thought-time">${t} · cycle ${entry.cycle}</div>
        ${entry.message}
      </div>`;
    }).join("");
  } catch (e) {
    console.error("thinking", e);
  }
}

// ─── Refresh loop ─────────────────────────────────────────────────────────────
async function refresh() {
  await Promise.all([
    loadStatus(),
    loadPortfolio(),
    loadOpportunities(),
    loadPositions(),
    loadTrades(),
    loadThinking(),
  ]);
}

refresh();
setInterval(refresh, 30000); // auto-refresh every 30s
