# polyGeni — PAPER trading only. The API binds to 127.0.0.1.
PY := .venv/bin/python
UV := .venv/bin/uvicorn

install:
	uv venv --python 3.11 .venv && uv pip install --python $(PY) -r requirements.txt

run:            ## 24/7 loop + dashboard on http://127.0.0.1:8000
	PAPER_AUTOSTART=1 $(UV) src.main:app --host 127.0.0.1 --port 8000

service:        ## run Kairos 24/7 on this Mac (launchd: starts at login, restarts on crash, no idle sleep)
	scripts/kairos_service.sh install

service-stop:   ## remove the launchd service
	scripts/kairos_service.sh uninstall

test:
	$(PY) -m pytest -q

backtest:
	$(PY) -m src.solana.backtest --days 180
	$(PY) -m src.solana.backtest --venue hyperliquid --interval 1h
	$(PY) -m src.solana.backtest --venue hyperliquid --interval 15m

.PHONY: install run test backtest
