# Backtest walk-forward — Hyperliquid perps 1h (1x long) vs buy-and-hold (2026-10-22)

> PAPER research only. Backtests overfit; a strategy must also beat hold in weeks of live paper trading.

- Data: Hyperliquid candleSnapshot 1h, last 180 days, BTC, ETH, SOL, HYPE, XRP, BNB, DOGE, ADA, TRX, AVAX, LINK, LTC, BCH, XLM, HBAR, DOT, SUI, NEAR, AAVE, UNI, ARB, TAO, ZEC, XMR, WLD, ENA, ZRO, ONDO, kPEPE, PUMP
- Costs per side: taker fee per market (BTC 4.50, ETH 4.50, SOL 4.50, HYPE 4.50, XRP 4.50, BNB 4.50, DOGE 4.50, ADA 4.50, TRX 4.50, AVAX 4.50, LINK 4.50, LTC 4.50, BCH 4.50, XLM 4.50, HBAR 4.50, DOT 4.50, SUI 4.50, NEAR 4.50, AAVE 4.50, UNI 4.50, ARB 4.50, TAO 4.50, ZEC 4.50, XMR 4.50, WLD 4.50, ENA 4.50, ZRO 4.50, ONDO 4.50, kPEPE 4.50, PUMP 4.50 bps) + 2.0 bps spread/slippage; hourly funding from fundingHistory charged on open longs
- Stops/TPs x1.0 (market profile); sizing 20% of strategy capital; max hold 72h; benchmarks = buy-and-hold of each asset (perp, no funding in hold)
- Out-of-sample span: 2026-06-09T23:00:00+00:00 → 2026-10-22T23:00:00+00:00 (3241 bars, 3 OOS folds)
- Walk-forward: each fold trades the parameter set (incl. regime filter SMA200 on/off) with the best in-sample Sharpe on the previous fold; next-bar-open fills; stop wins if stop and TP touch in one bar

| Strategy / benchmark | Net return | Max DD | Sharpe | Trades | Win rate |
|---|---:|---:|---:|---:|---:|
| momentum | -4.51% | 27.18% | 0.01 | 292 | 41.4% |
| mean_reversion | 7.92% | 13.37% | 0.92 | 120 | 53.3% |
| hold_BTC | 31.59% | 13.42% | 2.19 | 0 | – |
| hold_ETH | 45.09% | 17.45% | 2.27 | 0 | – |
| hold_SOL | 59.1% | 19.06% | 2.48 | 0 | – |
| hold_HYPE | 33.82% | 33.44% | 1.4 | 0 | – |
| hold_XRP | 14.92% | 24.4% | 0.91 | 0 | – |
| hold_BNB | 23.07% | 13.77% | 1.7 | 0 | – |
| hold_DOGE | -7.15% | 26.7% | -0.01 | 0 | – |
| hold_ADA | 27.95% | 26.76% | 1.24 | 0 | – |
| hold_TRX | 1.7% | 8.5% | 0.35 | 0 | – |
| hold_AVAX | 37.58% | 23.27% | 1.53 | 0 | – |
| hold_LINK | 52.11% | 25.26% | 2.05 | 0 | – |
| hold_LTC | 36.31% | 23.59% | 1.74 | 0 | – |
| hold_BCH | 34.99% | 29.98% | 1.45 | 0 | – |
| hold_XLM | -8.93% | 38.42% | 0.06 | 0 | – |
| hold_HBAR | 0.3% | 39.32% | 0.36 | 0 | – |
| hold_DOT | 16.03% | 30.2% | 0.9 | 0 | – |
| hold_SUI | 30.1% | 28.3% | 1.28 | 0 | – |
| hold_NEAR | 124.95% | 39.14% | 2.51 | 0 | – |
| hold_AAVE | 135.66% | 22.1% | 3.17 | 0 | – |
| hold_UNI | 182.42% | 36.56% | 3.18 | 0 | – |
| hold_ARB | 122.47% | 34.55% | 2.47 | 0 | – |
| hold_TAO | 25.08% | 33.9% | 1.14 | 0 | – |
| hold_ZEC | 185.68% | 37.33% | 3.23 | 0 | – |
| hold_XMR | 95.12% | 27.42% | 2.59 | 0 | – |
| hold_WLD | 6.61% | 57.85% | 0.73 | 0 | – |
| hold_ENA | 146.26% | 40.19% | 2.64 | 0 | – |
| hold_ZRO | 111.66% | 41.42% | 2.23 | 0 | – |
| hold_ONDO | 19.83% | 30.57% | 0.99 | 0 | – |
| hold_kPEPE | 58.32% | 30.54% | 1.84 | 0 | – |
| hold_PUMP | 198.15% | 42.24% | 2.86 | 0 | – |

## Per fold (parameters chosen in-sample on the previous fold)

- momentum fold 1: params {'lookback': 24, 'vol_mult': 2.0, 'regime_sma': None} → -6.13% (DD 18.1%, Sharpe -0.88, 83 trades)
- momentum fold 2: params {'lookback': 48, 'vol_mult': 1.5, 'regime_sma': 200} → 30.36% (DD 15.07%, Sharpe 4.56, 99 trades)
- momentum fold 3: params {'lookback': 48, 'vol_mult': 2.0, 'regime_sma': 200} → -21.97% (DD 27.18%, Sharpe -3.3, 110 trades)
- mean_reversion fold 1: params {'k': 2.5, 'rsi_max': 25, 'regime_sma': 200} → 2.65% (DD 4.08%, Sharpe 1.33, 20 trades)
- mean_reversion fold 2: params {'k': 2.0, 'rsi_max': 25, 'regime_sma': 200} → -2.14% (DD 7.48%, Sharpe -0.71, 32 trades)
- mean_reversion fold 3: params {'k': 2.5, 'rsi_max': 25, 'regime_sma': None} → 7.43% (DD 13.37%, Sharpe 1.8, 68 trades)

## Verdict

Best strategy OOS: **mean_reversion** (7.92%). Beats: hold_DOGE, hold_TRX, hold_XLM, hold_HBAR, hold_WLD; loses to: hold_BTC, hold_ETH, hold_SOL, hold_HYPE, hold_XRP, hold_BNB, hold_ADA, hold_AVAX, hold_LINK, hold_LTC, hold_BCH, hold_DOT, hold_SUI, hold_NEAR, hold_AAVE, hold_UNI, hold_ARB, hold_TAO, hold_ZEC, hold_XMR, hold_ENA, hold_ZRO, hold_ONDO, hold_kPEPE, hold_PUMP.

Do not go live on this basis alone: the phase-3 gate is 4–8 weeks of 24/7 paper trading with real quotes that beats buy-and-hold after costs, with drawdown inside limits.
