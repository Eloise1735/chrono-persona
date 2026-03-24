"""Kelsey State Machine — FastAPI + MCP Server entry point."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from starlette.requests import Request

from server.config import load_config
from server.database import Database
from server.llm_client import LLMClient
from server.environment import TemplateEnvironmentGenerator
from server.memory_store import KeywordMemoryStore
from server.vector_memory_store import VectorMemoryStore
from server.prompts import PromptManager, DEFAULT_SETTINGS
from server.state_machine import StateMachine
from server.evolution import EvolutionEngine
from server.automation_engine import AutomationEngine
from server.mcp_tools import mcp, set_state_machine, set_evolution_engine
from server.api_routes import router as api_router, set_dependencies

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

config = load_config()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = Database(config.database.path)
    await db.initialize()
    logger.info("Database initialized at %s", config.database.path)

    llm = LLMClient(config.llm, db=db)
    prompt_manager = PromptManager(db)
    await db.initialize_default_settings(DEFAULT_SETTINGS)
    env_gen = TemplateEnvironmentGenerator(prompt_manager=prompt_manager, llm=llm)
    if config.memory_store.type == "vector":
        memory = VectorMemoryStore(db)
        logger.info("Memory store initialized with vector mode.")
    else:
        memory = KeywordMemoryStore(db)
        logger.info("Memory store initialized with keyword mode.")

    evolution_engine = EvolutionEngine(db, llm, prompt_manager)
    automation_engine = AutomationEngine(
        db=db,
        prompt_manager=prompt_manager,
        memory_store=memory,
        evolution_engine=evolution_engine,
    )
    sm = StateMachine(
        config,
        db,
        llm,
        env_gen,
        memory,
        prompt_manager,
        automation_engine=automation_engine,
    )

    set_state_machine(sm)
    set_evolution_engine(evolution_engine)
    set_dependencies(
        db,
        sm,
        memory,
        prompt_manager=prompt_manager,
        evolution_engine=evolution_engine,
        llm_client=llm,
    )

    # streamable_http_app() is mounted below; its Starlette lifespan is not run when nested
    # under FastAPI, so we must start the session manager here or Streamable HTTP returns 500.
    async with mcp.session_manager.run():
        logger.info("State machine ready. MCP tools registered.")
        yield

    await llm.close()
    await db.close()
    logger.info("Shutdown complete.")


app = FastAPI(title="Kelsey State Machine", lifespan=lifespan)


@app.middleware("http")
async def normalize_mcp_streamable_http_path(request: Request, call_next):
    """RikkaHub / FastMCP path quirks: avoid 307 on /mcp-http and 404 on /mcp-http/."""
    if request.scope["type"] == "http":
        path = request.scope["path"]
        if path in ("/mcp-http", "/mcp-http/"):
            request.scope["path"] = "/mcp-http/mcp"
        elif path == "/mcp-http/mcp/":
            request.scope["path"] = "/mcp-http/mcp"
    return await call_next(request)


app.include_router(api_router)

app.mount("/mcp", mcp.sse_app())
# Compatibility endpoint for clients that prefer streamable HTTP transport.
app.mount("/mcp-http", mcp.streamable_http_app())

web_dir = Path(__file__).parent.parent / "web"
if web_dir.exists():
    app.mount("/static", StaticFiles(directory=str(web_dir)), name="static")


@app.get("/")
async def serve_index():
    index = Path(__file__).parent.parent / "web" / "index.html"
    if index.exists():
        return FileResponse(str(index))
    return {"message": "Kelsey State Machine is running. Web UI not found."}


@app.get("/guide")
async def serve_guide():
    page = Path(__file__).parent.parent / "web" / "guide.html"
    if page.exists():
        return FileResponse(str(page))
    return {"message": "Guide page not found."}


@app.get("/history")
async def serve_history():
    page = Path(__file__).parent.parent / "web" / "history.html"
    if page.exists():
        return FileResponse(str(page))
    return {"message": "History page not found."}


@app.get("/snapshots-history")
async def serve_snapshots_history():
    page = Path(__file__).parent.parent / "web" / "snapshots.html"
    if page.exists():
        return FileResponse(str(page))
    return {"message": "Snapshots history page not found."}


@app.get("/events-history")
async def serve_events_history():
    page = Path(__file__).parent.parent / "web" / "events.html"
    if page.exists():
        return FileResponse(str(page))
    return {"message": "Events history page not found."}


@app.get("/key-records")
async def serve_key_records():
    page = Path(__file__).parent.parent / "web" / "key-records.html"
    if page.exists():
        return FileResponse(str(page))
    return {"message": "Key records page not found."}


@app.get("/settings")
async def serve_settings():
    page = Path(__file__).parent.parent / "web" / "settings.html"
    if page.exists():
        return FileResponse(
            str(page),
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )
    return {"message": "Settings page not found."}


@app.get("/evolution")
async def serve_evolution():
    page = Path(__file__).parent.parent / "web" / "evolution.html"
    if page.exists():
        return FileResponse(str(page))
    return {"message": "Evolution page not found."}


@app.get("/vectors")
async def serve_vectors():
    page = Path(__file__).parent.parent / "web" / "vectors.html"
    if page.exists():
        return FileResponse(str(page))
    return {"message": "Vector management page not found."}


if __name__ == "__main__":
    import os

    is_dev = os.environ.get("KELSEY_DEV", "").lower() in ("1", "true", "yes")
    uvicorn.run(
        "server.main:app",
        host=config.server.host,
        port=config.server.port,
        reload=is_dev,
    )
