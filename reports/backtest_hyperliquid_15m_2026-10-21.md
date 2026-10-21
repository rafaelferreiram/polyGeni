# Backtest walk-forward — Hyperliquid perps 15m (1x long) vs buy-and-hold (2026-10-21)

> PAPER research only. Backtests overfit; a strategy must also beat hold in weeks of live paper trading.

- Data: Hyperliquid candleSnapshot 15m, last 52 days, BTC, ETH, SOL, xyz:GOLD, xyz:SILVER
- Costs per side: taker fee per market (BTC 4.50, ETH 4.50, SOL 4.50, xyz:GOLD 9.00, xyz:SILVER 0.90 bps) + 2.0 bps spread/slippage; hourly funding from fundingHistory charged on open longs
- Stops/TPs x1.5 (market profile); sizing 20% of strategy capital; max hold 72h; benchmarks = buy-and-hold of each asset (perp, no funding in hold)
- Out-of-sample span: 2026-09-12T01:15:00+00:00 → 2026-10-21T01:15:00+00:00 (3745 bars, 3 OOS folds)
- Walk-forward: each fold trades the parameter set (incl. regime filter SMA200 on/off) with the best in-sample Sharpe on the previous fold; next-bar-open fills; stop wins if stop and TP touch in one bar

| Strategy / benchmark | Net return | Max DD | Sharpe | Trades | Win rate |
|---|---:|---:|---:|---:|---:|
| momentum | -7.05% | 12.7% | -1.38 | 44 | 43.2% |
| mean_reversion | -6.57% | 9.78% | -1.61 | 30 | 33.3% |
| hold_BTC | 6.91% | 8.2% | 1.02 | 0 | – |
| hold_ETH | -2.9% | 16.92% | -0.19 | 0 | – |
| hold_SOL | 3.93% | 19.25% | 0.46 | 0 | – |
| hold_xyz:GOLD | -5.76% | 7.3% | -1.37 | 0 | – |
| hold_xyz:SILVER | -10.83% | 17.71% | -1.48 | 0 | – |

## Per fold (parameters chosen in-sample on the previous fold)

- momentum fold 1: params {'lookback': 48, 'vol_mult': 2.0, 'regime_sma': 200} → 0.88% (DD 4.09%, Sharpe 0.54, 15 trades)
- momentum fold 2: params {'lookback': 24, 'vol_mult': 2.0, 'regime_sma': None} → -6.34% (DD 7.86%, Sharpe -3.55, 17 trades)
- momentum fold 3: params {'lookback': 24, 'vol_mult': 1.5, 'regime_sma': 200} → -1.63% (DD 4.92%, Sharpe -1.11, 12 trades)
- mean_reversion fold 1: params {'k': 2.5, 'rsi_max': 25, 'regime_sma': 200} → 0.16% (DD 1.66%, Sharpe 0.3, 5 trades)
- mean_reversion fold 2: params {'k': 2.5, 'rsi_max': 30, 'regime_sma': None} → -3.9% (DD 5.15%, Sharpe -3.0, 9 trades)
- mean_reversion fold 3: params {'k': 2.0, 'rsi_max': 30, 'regime_sma': None} → -2.94% (DD 5.67%, Sharpe -1.51, 16 trades)

## Verdict

Best strategy OOS: **mean_reversion** (-6.57%). Beats: hold_xyz:SILVER; loses to: hold_BTC, hold_ETH, hold_SOL, hold_xyz:GOLD.

Do not go live on this basis alone: the phase-3 gate is 4–8 weeks of 24/7 paper trading with real quotes that beats buy-and-hold after costs, with drawdown inside limits.
