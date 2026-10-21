# polyGeni — PAPER trading only. The API binds to 127.0.0.1.
PY := .venv/bin/python
UV := .venv/bin/uvicorn

install:
	uv venv --python 3.11 .venv && uv pip install --python $(PY) -r requirements.txt

run:            ## 24/7 loop + dashboard on http://127.0.0.1:8000
	PAPER_AUTOSTART=1 $(UV) src.main:app --host 127.0.0.1 --port 8000

demo:           ## dummy account: real quotes, forced trades on both venues, then dashboard on :8001
	rm -f demo.db && $(PY) -m src.solana.demo --db demo.db --cycles 4
	PAPER_DB_PATH=./demo.db $(UV) src.main:app --host 127.0.0.1 --port 8001

test:
	$(PY) -m pytest -q

backtest:
	$(PY) -m src.solana.backtest --days 180
	$(PY) -m src.solana.backtest --venue hyperliquid --interval 1h
	$(PY) -m src.solana.backtest --venue hyperliquid --interval 15m

.PHONY: install run demo test backtest
