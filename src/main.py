from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from src.api.routes import router
from src.database import init_db
import logging
import os

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

app = FastAPI(title="polyGeni", version="1.0.0")

app.include_router(router, prefix="/api")

# Serve frontend
frontend_path = os.path.join(os.path.dirname(__file__), "..", "frontend")
if os.path.isdir(frontend_path):
    app.mount("/static", StaticFiles(directory=frontend_path), name="static")

    @app.get("/")
    def serve_ui():
        return FileResponse(os.path.join(frontend_path, "index.html"))


@app.on_event("startup")
def startup():
    init_db()
    logging.getLogger("main").info("polyGeni started. DB initialized.")
