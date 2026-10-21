from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from src.api.routes import router
from src.api.paper_routes import router as paper_router
from src.solana import config as solana_cfg, engine as paper_engine
from src.database import init_db
import logging
import os

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

app = FastAPI(title="Kairos", version="1.0.0")

from src.api import auth  # noqa: E402
app.middleware("http")(auth.middleware)   # remote access: login + read-only; this Mac is unaffected

app.include_router(router, prefix="/api")
app.include_router(paper_router, prefix="/api/paper")

# Serve frontend
frontend_path = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(frontend_path):
    app.mount("/static", StaticFiles(directory=frontend_path), name="static")

    @app.get("/")
    @app.get("/paper")          # old link, same page
    def serve_ui():
        return FileResponse(os.path.join(frontend_path, "paper.html"))


@app.on_event("startup")
def startup():
    init_db()
    logging.getLogger("main").info("Kairos started. DB initialized.")
    from src.solana import logbuf
    logbuf.install()     # live log console on the dashboard
    if solana_cfg.PAPER_AUTOSTART:
        paper_engine.start()
    from src.solana import hl_live
    hl_live.schedule()   # the live job only trades while armed (state persists in live.db)
    from src.solana import arena
    arena.resume_if_running()
    from src.solana import whales, news
    whales.start()      # read-only WebSocket of big trades on the coins the bots watch
    news.start()        # crypto news RSS, tagged by coin, every 5 min
    from src.solana import toptraders
    toptraders.start()  # top 10 Hyperliquid traders: leaderboard every 6h, their positions every 2 min
