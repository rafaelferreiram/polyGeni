---
name: trading-routine
description: Morning and evening review of the polyGeni trading bots (Hyperliquid real account + simulation + AI lab) — what traded, what went well or badly, wins/losses and net profit after AI costs, today's projection, market setups and concrete improvement proposals, written in Portuguese to reports/routines/. Use whenever the user asks for the "rotina", "relatório da manhã/noite", "como foram os trades", "balanço do dia", "projeção de hoje", or a scheduled morning/evening routine fires — even if they don't name the skill.
---

# Rotina de trading (manhã / noite)

Goal: in 2 minutes of reading, the user knows whether the bots are healthy, whether they are making or losing
money after every cost, why, and what (if anything) should change. Plain Portuguese, no jargon without a gloss.
The user is proving with ~$18 of real money that the system works before scaling — honesty beats optimism.

## 1. Get the data (one command)

From the repo root (the worktree that runs the dashboard):

```bash
.venv/bin/python -m src.solana.routine > /tmp/routine.json
```

It reads the running dashboard's read-only endpoints plus public Hyperliquid candles. Keys:
`server` (online / OFFLINE), `real` and `sim` (equity, exchange_balance, kill_switch, sleep, open_positions,
`trades_window` = trades closed in the last 24h, `decision_kinds_window`, `top_reasons_window`, `team`,
`daily` = last 8 days, `week` totals, `profit` = trades − AI − server), `lab` (AI bots' paper ledgers),
`setups` (per market: uptrend, RSI, dip below band, ATR%), `projection`, `limits`.

If `server` is OFFLINE, that is the headline: the bots are not trading. Tell the user to run `make run`
(or start the app) and stop there — do not invent numbers.

For a single trade or decision that needs more detail you can also read
`GET http://127.0.0.1:8000/api/paper/account?kind=real` directly. Do not call POST endpoints.

## 2. Analyse

- **Saúde**: server online, real account armed (`running`), heartbeat recent (< 15 min unless sleeping),
  kill switch off, `exchange_balance` ≈ `equity` (a gap > $0.10 means the ledger and exchange disagree —
  flag it), no `config_problems`, no errors in the daily rows.
- **Trades** (real first, then sim): for each trade in the window — market, bot, entry → exit, P&L after fees,
  exit reason. Classify *good* (followed the plan: dip in uptrend, ATR stop/target, exit by rule) vs *bad*
  (stopped out fast, entered against trend, LLM exit cut a winner, slippage). A losing trade can be a good
  trade; say so when the process was right.
- **Ganhos e perdas**: today, last 7 days, all-time, and **net after AI cost** (`profit.net_all`). AI spend vs
  monthly budget. If an AI bot costs more than it earns, say it plainly.
- **Sem trades?** Usually correct: explain with `setups` (e.g. "BTC está em tendência de alta mas RSI 66 —
  não há queda para comprar") and the sleep/veto reasons. Not trading in a market without edge is the strategy
  working, not failing.
- **Real vs simulação vs laboratório**: same decisions should give similar results; a divergence points to
  execution problems (fills, slippage, funding).

## 3. Projection — be honest

Use `projection` (from the out-of-sample backtest) scaled to the real position size: expected trades in 24h,
expected $ per trade and per day, and the most likely outcome (often "0 trades"). Combine with `setups`: which
markets are closest to a buy signal (uptrend + RSI < 40 or below the lower band). Give a range, never a promise.
At this capital the expected daily gain is cents — say that, and that the point now is proof, not income.

## 4. Improvements — propose, never apply

List at most 3 concrete, evidence-backed suggestions (e.g. "HYPE parou 3 vezes no stop em 1h: testar stop 1.2×ATR
no laboratório"). Each says what data supports it and how to test it in the simulation/lab first.
Do not edit strategy, risk limits, `.env`, or arm/disarm/flatten anything — those are the user's decisions.
If the evidence is thin (few trades), say "ainda sem amostra suficiente" instead of inventing a tweak.

## 5. Write the report

Save to `reports/routines/YYYY-MM-DD-manha.md` or `-noite.md` (local date; morning before 14:00) with this shape:

```markdown
# Rotina da manhã|noite — DD/MM/AAAA HH:MM
**Resumo:** one or two sentences — healthy?, net result, what to expect.

## Estado dos bots
## Trades das últimas 24h  (o que foi bom / o que foi ruim)
## Ganhos e perdas  (hoje · 7 dias · total · líquido após IA)
## Mercado agora e projeção do dia
## Melhorias propostas  (precisam da tua aprovação)
## Atenção  (only if something needs action now)
```

Morning emphasis: overnight trades, health, today's projection and setups to watch.
Evening emphasis: the day's closing balance, good/bad trades, lessons, and what the bots will do overnight.

Then reply to the user with the **Resumo** plus anything under **Atenção**, and the path of the report.
Never print API keys or the contents of `.env`.
