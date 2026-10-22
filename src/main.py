from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from src.api.paper_routes import router as paper_router
import logging
import os

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

app = FastAPI(title="Kairos", version="1.0.0")

from src.api import auth  # noqa: E402
app.middleware("http")(auth.middleware)   # remote access: login + read-only; this Mac is unaffected

app.include_router(paper_router, prefix="/api/paper")

# Serve frontend
frontend_path = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(frontend_path):
    app.mount("/static", StaticFiles(directory=frontend_path), name="static")

    @app.get("/")
    @app.get("/paper")          # old link, same page
    def serve_ui():
        return FileResponse(os.path.join(frontend_path, "paper.html"))

    @app.get("/admin")
    def serve_admin():
        return FileResponse(os.path.join(frontend_path, "admin.html"))


@app.on_event("shutdown")
def shutdown():
    """A restart must never cut a real order in half (it did once: bought on the exchange, not in the books, no stop):
    wait for any running account cycle or manual order to finish, then keep the accounts locked until exit."""
    from src.solana import hl_live
    for get in (hl_live.get_live_engine, hl_live.get_shadow_engine):
        try:
            eng = get()
            eng._yield = True                     # a running cycle stops at the next coin
            eng._busy.acquire(timeout=90)
        except Exception as e:
            logging.getLogger("main").warning(f"shutdown wait: {e}")


@app.on_event("startup")
def startup():
    logging.getLogger("main").info("Kairos started.")
    from src.solana import logbuf
    logbuf.install()     # live log console on the dashboard
    from src.solana import hl_live
    hl_live.schedule()   # the live job only trades while armed (state persists in live.db)
    from src.solana import arena
    arena.resume_if_running()
    from src.solana import whales, news
    whales.start()      # read-only WebSocket of big trades on the coins the bots watch
    news.start()        # crypto news RSS, tagged by coin, every 5 min
    from src.solana import toptraders
    toptraders.start()
    from src.solana import meme
    meme.start()        # the meme-coin trench: lab bots on Solana meme coins (paper money, real prices)  # top 10 Hyperliquid traders: leaderboard every 6h, their positions every 2 min
