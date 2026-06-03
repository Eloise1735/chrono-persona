"""Kelsey State Machine FastAPI entry point."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from logging import StreamHandler
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from starlette.requests import Request

from server.config import load_config
from server.database import Database
from server.llm_client import LLMClient, EnvironmentLLMClient, SnapshotLLMClient
from server.environment import TemplateEnvironmentGenerator
from server.memory_store import KeywordMemoryStore
from server.vector_memory_store import VectorMemoryStore
from server.prompts import PromptManager, DEFAULT_SETTINGS
from server.state_machine import StateMachine
from server.evolution import EvolutionEngine
from server.automation_engine import AutomationEngine
from server.npc_engine import NPCEngine
from server.plan_engine import PlanEngine
from server.mcp_tools import mcp, set_state_machine, set_evolution_engine, set_plan_engine
from server.ob_client import OBClient
from server.ob_decay import OBDecayEngine, ob_decay_settings_from_config
from server.ob_embedding import OBEmbeddingStore
from server.api_routes import router as api_router, set_dependencies
from server.security import get_admin_token, request_has_admin_token

# Keep logs in UTC+8 to match application-facing time displays.
_CST = timezone(timedelta(hours=8))


class _ShanghaiLogFormatter(logging.Formatter):
    """Format log timestamps in UTC+8."""

    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, tz=timezone.utc).astimezone(_CST)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.isoformat(sep=" ", timespec="seconds")


_shanghai_handler = StreamHandler()
_shanghai_handler.setFormatter(
    _ShanghaiLogFormatter(
        fmt="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
)
logging.basicConfig(level=logging.INFO, handlers=[_shanghai_handler])
logger = logging.getLogger(__name__)

config = load_config()


def _scheduler_tick_log_summary(result: dict) -> str:
    """Build a one-line scheduler summary for INFO logs."""
    status = str(result.get("status") or "")
    reason = str(result.get("reason") or "")
    parts = [f"status={status}"]
    if reason:
        parts.append(f"reason={reason}")
    if "lag_hours" in result:
        parts.append(f"lag_h={result.get('lag_hours')}")
    if "min_time_unit_hours" in result:
        parts.append(f"min_unit_h={result.get('min_time_unit_hours')}")
    if result.get("latest_snapshot_cst"):
        parts.append(f"latest_cst={result.get('latest_snapshot_cst')}")
    if result.get("now_cst"):
        parts.append(f"now_cst={result.get('now_cst')}")
    if result.get("resume_at_cst"):
        parts.append(f"resume_at={result.get('resume_at_cst')}")
    if result.get("last_auto_snapshot_cst"):
        parts.append(f"last_auto_cst={result.get('last_auto_snapshot_cst')}")
    if "raw_lag_hours" in result:
        parts.append(f"raw_lag_h={result.get('raw_lag_hours')}")
    if "interval_sec" in result:
        parts.append(f"next_sleep_s={result.get('interval_sec')}")
    n_gen = len(result.get("generated_snapshots") or [])
    if n_gen or status == "advanced":
        parts.append(f"generated={n_gen}")
    return " ".join(parts)



async def _snapshot_scheduler_loop(state_machine: StateMachine):
    first = True
    while True:
        interval_sec = 60
        try:
            interval_sec = await state_machine.get_snapshot_scheduler_interval_seconds()
            if first:
                logger.info(
                    "Snapshot scheduler loop running (interval_sec=%s). Each tick logs one INFO line.",
                    interval_sec,
                )
                first = False
            result = await state_machine.run_snapshot_scheduler_tick()
            status = str(result.get("status") or "")
            if status == "advanced":
                logger.info(
                    "Snapshot scheduler tick: %s | %s",
                    _scheduler_tick_log_summary(result),
                    json.dumps(result, ensure_ascii=False),
                )
            else:
                logger.info("Snapshot scheduler tick: %s", _scheduler_tick_log_summary(result))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Snapshot scheduler loop failed.")
        await asyncio.sleep(max(5, int(interval_sec)))


async def _life_scheduler_loop(
    state_machine: StateMachine,
    plan_engine: PlanEngine,
):
    first = True
    while True:
        interval_sec = 60
        try:
            interval_sec = await state_machine.get_snapshot_scheduler_interval_seconds()
            if first:
                logger.info(
                    "Life scheduler loop running (interval_sec=%s).",
                    interval_sec,
                )
                first = False
            if await plan_engine.is_enabled():
                await plan_engine.ensure_today_plan()
                current_item = await plan_engine.get_current_item()
                if current_item is not None:
                    await plan_engine.execute_item(current_item)
            result = await state_machine.run_snapshot_scheduler_tick()
            if await plan_engine.is_enabled():
                await plan_engine.maybe_replan_on_drift()
            status = str(result.get("status") or "")
            if status == "advanced":
                logger.info(
                    "Life scheduler tick: %s | %s",
                    _scheduler_tick_log_summary(result),
                    json.dumps(result, ensure_ascii=False),
                )
            else:
                logger.info("Life scheduler tick: %s", _scheduler_tick_log_summary(result))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Life scheduler loop failed.")
        await asyncio.sleep(max(5, int(interval_sec)))


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = Database(config.database.path)
    await db.initialize()
    logger.info("Database initialized at %s", config.database.path)

    llm = LLMClient(config.llm, db=db)
    env_llm = EnvironmentLLMClient(config.llm, db=db)
    snapshot_llm = SnapshotLLMClient(config.llm, db=db)
    prompt_manager = PromptManager(db)
    await db.initialize_default_settings(DEFAULT_SETTINGS)
    env_gen = TemplateEnvironmentGenerator(prompt_manager=prompt_manager, llm=env_llm)
    if config.memory_store.type == "vector":
        memory = VectorMemoryStore(db)
        logger.info("Memory store initialized with vector mode.")
    else:
        memory = KeywordMemoryStore(db)
        logger.info("Memory store initialized with keyword mode.")
    ob_embedding_store = OBEmbeddingStore(db, config.ob.buckets_dir) if config.ob.enabled else None
    ob_client = (
        OBClient(config.ob.buckets_dir, embedding_store=ob_embedding_store)
        if config.ob.enabled
        else None
    )
    ob_decay_engine = (
        OBDecayEngine(ob_client, ob_decay_settings_from_config(config))
        if ob_client is not None
        else None
    )
    if ob_client is not None:
        logger.info("OB bucket store initialized at %s.", config.ob.buckets_dir)

    evolution_engine = EvolutionEngine(db, llm, prompt_manager, snapshot_llm=snapshot_llm)
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
        snapshot_llm=snapshot_llm,
        automation_engine=automation_engine,
    )
    npc_engine = NPCEngine(db, llm, prompt_manager)
    plan_engine = PlanEngine(db, llm, prompt_manager, sm, npc_engine)
    sm.set_plan_engine(plan_engine)
    sm.set_ob_client(ob_client)

    set_state_machine(sm)
    set_evolution_engine(evolution_engine)
    set_plan_engine(plan_engine)
    from server.mcp_tools import set_ob_client
    set_ob_client(ob_client)
    from server.mcp_tools import set_ob_decay_engine
    set_ob_decay_engine(ob_decay_engine)
    set_dependencies(
        db,
        sm,
        memory,
        prompt_manager=prompt_manager,
        evolution_engine=evolution_engine,
        llm_client=llm,
        env_llm_client=env_llm,
        snapshot_llm_client=snapshot_llm,
        plan_engine=plan_engine,
        npc_engine=npc_engine,
        ob_client=ob_client,
        ob_embedding_store=ob_embedding_store,
        ob_decay_engine=ob_decay_engine,
    )

    # streamable_http_app() is mounted below; its Starlette lifespan is not run when nested
    # under FastAPI, so we must start the session manager here or Streamable HTTP returns 500.
    async with mcp.session_manager.run():
        scheduler_task = asyncio.create_task(_life_scheduler_loop(sm, plan_engine))
        if ob_decay_engine is not None:
            await ob_decay_engine.start()
        logger.info("State machine ready. MCP tools registered.")
        try:
            yield
        finally:
            if ob_decay_engine is not None:
                await ob_decay_engine.stop()
            scheduler_task.cancel()
            try:
                await scheduler_task
            except asyncio.CancelledError:
                logger.info("Snapshot scheduler stopped.")

    await llm.close()
    await db.close()
    logger.info("Shutdown complete.")


app = FastAPI(title="Kelsey State Machine", lifespan=lifespan)


@app.middleware("http")
async def require_admin_token_for_private_surfaces(request: Request, call_next):
    """Optional shared-secret guard for API and MCP surfaces.

    Set KELSEY_ADMIN_TOKEN on the server and send it as X-Kelsey-Admin-Token
    or Authorization: Bearer <token>. If unset, local/dev behavior stays unchanged.
    """
    token = get_admin_token()
    path = request.url.path
    if token and path.startswith(("/api", "/mcp", "/mcp-http")):
        if request.method.upper() == "OPTIONS":
            return await call_next(request)
        if not request_has_admin_token(request, token):
            return JSONResponse(
                {"detail": "Admin token required"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
    return await call_next(request)


@app.middleware("http")
async def log_unhandled_request_exceptions(request: Request, call_next):
    """Log unhandled request exceptions with a full traceback."""
    try:
        return await call_next(request)
    except Exception:
        logger.exception(
            "Unhandled exception on %s %s",
            request.method,
            request.url.path,
        )
        raise


@app.middleware("http")
async def normalize_mcp_streamable_http_path(request: Request, call_next):
    """RikkaHub / FastMCP path quirks: avoid redirects and strict Accept failures."""
    if request.scope["type"] == "http":
        path = request.scope["path"]
        if path in ("/mcp-http", "/mcp-http/"):
            request.scope["path"] = "/mcp-http/mcp"
        elif path == "/mcp-http/mcp/":
            request.scope["path"] = "/mcp-http/mcp"
        if request.scope["path"] == "/mcp-http/mcp" and request.method.upper() == "POST":
            headers = list(request.scope.get("headers") or [])
            accept = ""
            accept_index = None
            for index, (name, value) in enumerate(headers):
                if name.lower() == b"accept":
                    accept = value.decode("latin-1")
                    accept_index = index
                    break
            accepts_json = "application/json" in accept
            accepts_sse = "text/event-stream" in accept
            if not (accepts_json and accepts_sse):
                normalized = b"application/json, text/event-stream"
                if accept_index is None:
                    headers.append((b"accept", normalized))
                else:
                    headers[accept_index] = (b"accept", normalized)
                request.scope["headers"] = headers
    return await call_next(request)


app.include_router(api_router)

app.mount("/mcp", mcp.sse_app())
# Compatibility endpoint for clients that prefer streamable HTTP transport.
app.mount("/mcp-http", mcp.streamable_http_app())

web_dir = Path(__file__).parent.parent / "web"
if web_dir.exists():
    app.mount("/static", StaticFiles(directory=str(web_dir)), name="static")


@app.get("/favicon.ico")
async def serve_favicon_ico():
    """Return a lightweight favicon response if a browser asks for one."""
    path = web_dir / "favicon.jpg"
    if not path.exists():
        raise HTTPException(status_code=404)
    return FileResponse(
        str(path),
        media_type="image/jpeg",
        headers={"Cache-Control": "no-cache, must-revalidate"},
    )


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


@app.get("/world-books")
async def serve_world_books():
    page = Path(__file__).parent.parent / "web" / "world-books.html"
    if page.exists():
        return FileResponse(str(page))
    return {"message": "World books page not found."}


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


@app.get("/ob")
async def serve_ob():
    page = Path(__file__).parent.parent / "web" / "ob-dashboard.html"
    if page.exists():
        return FileResponse(
            str(page),
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )
    return {"message": "OB dashboard page not found."}


@app.get("/ob/dashboard")
async def serve_ob_dashboard():
    page = Path(__file__).parent.parent / "web" / "ob-dashboard.html"
    if page.exists():
        return FileResponse(
            str(page),
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )
    return {"message": "OB dashboard page not found."}


@app.get("/environment-manage")
async def serve_environment_manage():
    page = Path(__file__).parent.parent / "web" / "environment-manage.html"
    if page.exists():
        return FileResponse(str(page))
    return {"message": "Environment management page not found."}


@app.get("/schedule")
async def serve_schedule():
    page = Path(__file__).parent.parent / "web" / "schedule.html"
    if page.exists():
        return FileResponse(
            str(page),
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )
    return {"message": "Schedule page not found."}


@app.get("/npcs")
async def serve_npcs():
    page = Path(__file__).parent.parent / "web" / "npcs.html"
    if page.exists():
        return FileResponse(str(page))
    return {"message": "NPC page not found."}


if __name__ == "__main__":
    import os

    is_dev = os.environ.get("KELSEY_DEV", "").lower() in ("1", "true", "yes")
    uvicorn.run(
        "server.main:app",
        host=config.server.host,
        port=config.server.port,
        reload=is_dev,
    )
