# Backtest walk-forward — Hyperliquid perps 1h (1x long) vs buy-and-hold (2026-10-21)

> PAPER research only. Backtests overfit; a strategy must also beat hold in weeks of live paper trading.

- Data: Hyperliquid candleSnapshot 1h, last 180 days, BTC, ETH, SOL, xyz:GOLD, xyz:SILVER
- Costs per side: taker fee per market (BTC 4.50, ETH 4.50, SOL 4.50, xyz:GOLD 9.00, xyz:SILVER 0.90 bps) + 2.0 bps spread/slippage; hourly funding from fundingHistory charged on open longs
- Stops/TPs x1.5 (market profile); sizing 20% of strategy capital; max hold 72h; benchmarks = buy-and-hold of each asset (perp, no funding in hold)
- Out-of-sample span: 2026-06-08T01:00:00+00:00 → 2026-10-21T01:00:00+00:00 (3241 bars, 3 OOS folds)
- Walk-forward: each fold trades the parameter set (incl. regime filter SMA200 on/off) with the best in-sample Sharpe on the previous fold; next-bar-open fills; stop wins if stop and TP touch in one bar

| Strategy / benchmark | Net return | Max DD | Sharpe | Trades | Win rate |
|---|---:|---:|---:|---:|---:|
| momentum | -3.73% | 13.13% | -0.39 | 87 | 36.8% |
| mean_reversion | 11.18% | 5.93% | 2.14 | 64 | 53.1% |
| hold_BTC | 29.97% | 13.42% | 2.09 | 0 | – |
| hold_ETH | 43.87% | 17.45% | 2.21 | 0 | – |
| hold_SOL | 59.25% | 19.06% | 2.48 | 0 | – |
| hold_xyz:GOLD | -5.59% | 12.87% | -0.56 | 0 | – |
| hold_xyz:SILVER | -16.01% | 22.98% | -0.87 | 0 | – |

## Per fold (parameters chosen in-sample on the previous fold)

- momentum fold 1: params {'lookback': 48, 'vol_mult': 1.5, 'regime_sma': 200} → -4.27% (DD 9.79%, Sharpe -1.65, 26 trades)
- momentum fold 2: params {'lookback': 24, 'vol_mult': 2.0, 'regime_sma': 200} → 8.74% (DD 7.4%, Sharpe 2.82, 34 trades)
- momentum fold 3: params {'lookback': 48, 'vol_mult': 1.5, 'regime_sma': 200} → -7.52% (DD 7.77%, Sharpe -3.9, 27 trades)
- mean_reversion fold 1: params {'k': 2.5, 'rsi_max': 25, 'regime_sma': 200} → 2.61% (DD 3.45%, Sharpe 2.39, 8 trades)
- mean_reversion fold 2: params {'k': 2.0, 'rsi_max': 25, 'regime_sma': None} → 6.1% (DD 2.13%, Sharpe 3.33, 29 trades)
- mean_reversion fold 3: params {'k': 2.5, 'rsi_max': 25, 'regime_sma': None} → 2.13% (DD 5.93%, Sharpe 1.11, 27 trades)

## Verdict

Best strategy OOS: **mean_reversion** (11.18%). Beats: hold_xyz:GOLD, hold_xyz:SILVER; loses to: hold_BTC, hold_ETH, hold_SOL.

Do not go live on this basis alone: the phase-3 gate is 4–8 weeks of 24/7 paper trading with real quotes that beats buy-and-hold after costs, with drawdown inside limits.
