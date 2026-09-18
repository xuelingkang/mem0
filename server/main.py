import asyncio
import csv
import io
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import telemetry
from auth import ADMIN_API_KEY, AUTH_DISABLED, JWT_SECRET, require_admin, verify_auth
from db import SessionLocal
from dotenv import load_dotenv
from dream_scheduler import DreamScheduler, SqlRunObserver, configure_scheduler
from errors import (
    UpstreamError,
    install_request_id_logging,
    new_request_id,
    request_id_var,
    upstream_error,
    upstream_error_handler,
)
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, Response
from models import RequestLog, User
from pydantic import BaseModel, Field
from rate_limit import limiter
from routers import api_keys as api_keys_router
from routers import auth as auth_router
from routers import dream as dream_router
from routers import entities as entities_router
from routers import graph as graph_router
from routers import requests as requests_router
from schemas import MessageResponse
from server_state import (
    get_current_config,
    get_memory_instance,
    initialize_state,
    set_session_factory,
    update_config,
)
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from sqlalchemy import func, select

from mem0.exceptions import ValidationError as Mem0ValidationError

load_dotenv()

install_request_id_logging()
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - [%(request_id)s] %(message)s")

MIN_KEY_LENGTH = 16
SENSITIVE_CONFIG_KEYS = {
    "admin_api_key",
    "api_key",
    "authorization",
    "jwt_secret",
    "password",
    "password_hash",
    "secret",
    "token",
}
SKIPPED_REQUEST_LOG_PATHS = {"/api/health", "/docs", "/redoc", "/openapi.json"}
SKIPPED_REQUEST_LOG_PREFIXES = ("/requests",)

BUNDLED_LLM_PROVIDERS = ("openai", "anthropic", "gemini")
BUNDLED_EMBEDDER_PROVIDERS = ("openai", "gemini")


def _warn_if_unconfigured() -> None:
    """Pre-auth deployments upgrading into this build will 401 everywhere until
    an admin key or admin user exists. Surface the fix before the support tickets."""
    try:
        with SessionLocal() as session:
            if session.scalar(select(func.count(User.id))) > 0:
                return
    except Exception:
        return

    logging.warning(
        "\n%s\n"
        "  Auth is enabled by default and this server has no admin configured.\n"
        "  Protected endpoints will return 401 until you either:\n"
        "    1. Set ADMIN_API_KEY=<long-random-value>  (fastest, no client changes)\n"
        "    2. Register an admin at http://<host>:3000/setup\n"
        "    3. Set AUTH_DISABLED=true                 (local development only)\n"
        "  Docs: https://docs.mem0.ai/open-source/features/rest-api#authentication\n"
        "%s",
        "=" * 72,
        "=" * 72,
    )


if not AUTH_DISABLED and not JWT_SECRET:
    raise RuntimeError(
        "JWT_SECRET is required. Set it in .env (generate with `openssl rand -base64 48`) "
        "or set AUTH_DISABLED=true for local development only."
    )

if AUTH_DISABLED:
    logging.warning("AUTH_DISABLED is enabled. Protected endpoints are open for local development only.")
elif ADMIN_API_KEY and len(ADMIN_API_KEY) < MIN_KEY_LENGTH:
    logging.warning(
        "ADMIN_API_KEY is shorter than %d characters - consider using a longer key for production.",
        MIN_KEY_LENGTH,
    )
elif not ADMIN_API_KEY:
    _warn_if_unconfigured()

telemetry.log_status()

POSTGRES_HOST = os.environ.get("POSTGRES_HOST", "postgres")
POSTGRES_PORT = os.environ.get("POSTGRES_PORT", "5432")
POSTGRES_DB = os.environ.get("POSTGRES_DB", "postgres")
POSTGRES_USER = os.environ.get("POSTGRES_USER", "postgres")
POSTGRES_PASSWORD = os.environ.get("POSTGRES_PASSWORD", "postgres")
POSTGRES_COLLECTION_NAME = os.environ.get("POSTGRES_COLLECTION_NAME", "memories")

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL")
HISTORY_DB_PATH = os.environ.get("HISTORY_DB_PATH", "/app/history/history.db")
DEFAULT_LLM_MODEL = os.environ.get("MEM0_DEFAULT_LLM_MODEL", "gpt-5-mini")
DEFAULT_EMBEDDER_MODEL = os.environ.get("MEM0_DEFAULT_EMBEDDER_MODEL", "text-embedding-3-small")

# LLM 与 embedder 可分别对接不同 provider（各自独立 key/base_url）。
# 未单独设置时回落到 OPENAI_*，保持旧行为。
LLM_API_KEY = os.environ.get("LLM_API_KEY", OPENAI_API_KEY)
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", OPENAI_BASE_URL)
EMBEDDER_API_KEY = os.environ.get("EMBEDDER_API_KEY", OPENAI_API_KEY)
EMBEDDER_BASE_URL = os.environ.get("EMBEDDER_BASE_URL", OPENAI_BASE_URL)

# Single source of truth for vector dimensionality. The embedder's output size and the
# Qdrant collection's vector size must agree, so both read this one value: setting only
# one of the two env vars can no longer silently desync them.
EMBEDDING_DIMS = int(
    os.environ.get("EMBEDDER_EMBEDDING_DIMS")
    or os.environ.get("QDRANT_EMBEDDING_DIMS")
    or "2048"
)


def _env_flag(name: str, default: bool) -> bool:
    """Read a boolean env override; leave the SDK default in place when unset."""
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def _env_number(name: str, default, cast):
    """Read a numeric env override, falling back to the SDK default on bad input."""
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return cast(str(raw).strip())
    except (TypeError, ValueError):
        logging.warning("Ignoring invalid %s=%r; using %s", name, raw, default)
        return default


# Retrieval decay (memory decay): the SDK owns the semantics of these knobs
# (`mem0.configs.base.DecayConfig`), this block only lets a deployment override the
# defaults through the environment without editing code. `enabled` stays off unless
# asked for, which keeps the retrieval behaviour byte-identical to pre-decay releases.
DECAY_CONFIG = {
    "enabled": _env_flag("MEM0_DECAY_ENABLED", False),
    "halflife_days": _env_number("MEM0_DECAY_HALFLIFE_DAYS", 12.0, float),
    "strength_step_days": _env_number("MEM0_DECAY_STRENGTH_STEP_DAYS", 3.0, float),
    "access_cap": _env_number("MEM0_DECAY_ACCESS_CAP", 20, int),
    "floor": _env_number("MEM0_DECAY_FLOOR", 0.90, float),
    "cooldown_seconds": _env_number("MEM0_DECAY_COOLDOWN_SECONDS", 300.0, float),
}

# Dream (background memory synthesis): the integration subsystem keeps its own knobs and
# stays off unless asked for. It is the one feature here that *writes* new records on its
# own schedule, so it must never switch itself on during an upgrade; `enabled=false` keeps
# both the periodic thread and the two trigger endpoints inert.
DREAM_CONFIG = {
    "enabled": _env_flag("DREAM_ENABLED", False),
    "interval_seconds": _env_number("DREAM_INTERVAL_SECONDS", 86400.0, float),
    "initial_delay_seconds": _env_number("DREAM_INITIAL_DELAY_SECONDS", 1800.0, float),
    "run_timeout_seconds": _env_number("DREAM_RUN_TIMEOUT_SECONDS", 1800.0, float),
    "per_cluster_timeout_seconds": _env_number("DREAM_PER_CLUSTER_TIMEOUT_SECONDS", 60.0, float),
    "report_dir": os.environ.get("DREAM_REPORT_DIR", "/app/history/dream-reports"),
    "lock_path": os.environ.get("DREAM_LOCK_PATH", "/app/history/dream.lock"),
}

# Graph memory (Graphiti side-car graph): the SDK owns the semantics of these knobs
# (`mem0.configs.base.GraphConfig`), this block only lets a deployment override the
# defaults through the environment. `enabled` stays off unless asked for, so retrieval
# and writes stay byte-identical to pre-graph releases.
GRAPH_CONFIG = {
    "enabled": _env_flag("MEM0_GRAPH_ENABLED", False),
    "endpoint": os.environ.get("MEM0_GRAPH_ENDPOINT", "http://graph-bridge:8000"),
    "weight": _env_number("MEM0_GRAPH_WEIGHT", 0.5, float),
    "max_facts": _env_number("MEM0_GRAPH_MAX_FACTS", 10, int),
    "timeout_seconds": _env_number("MEM0_GRAPH_TIMEOUT_SECONDS", 1.0, float),
    "include_invalidated": _env_flag("MEM0_GRAPH_INCLUDE_INVALIDATED", False),
    "queue_size": _env_number("MEM0_GRAPH_QUEUE_SIZE", 1000, int),
    "max_retries": _env_number("MEM0_GRAPH_MAX_RETRIES", 3, int),
    "retry_backoff_seconds": _env_number("MEM0_GRAPH_RETRY_BACKOFF_SECONDS", 5.0, float),
    "circuit_breaker_failures": _env_number("MEM0_GRAPH_CIRCUIT_BREAKER_FAILURES", 5, int),
    "circuit_cooldown_seconds": _env_number("MEM0_GRAPH_CIRCUIT_COOLDOWN_SECONDS", 60.0, float),
    "request_timeout_seconds": _env_number("MEM0_GRAPH_REQUEST_TIMEOUT_SECONDS", 120.0, float),
}

DEFAULT_CONFIG = {
    "version": "v1.1",
    "vector_store": {
        "provider": "qdrant",
        "config": {
            "host": os.environ.get("QDRANT_HOST", "qdrant"),
            "port": int(os.environ.get("QDRANT_PORT", "6333")),
            "collection_name": os.environ.get("QDRANT_COLLECTION_NAME", "memories"),
            "embedding_model_dims": EMBEDDING_DIMS,
        },
    },
    "llm": {
        "provider": "openai",
        "config": {"api_key": LLM_API_KEY, "openai_base_url": LLM_BASE_URL, "temperature": 0.2, "model": DEFAULT_LLM_MODEL},
    },
    "embedder": {"provider": "openai", "config": {"api_key": EMBEDDER_API_KEY, "openai_base_url": EMBEDDER_BASE_URL, "model": DEFAULT_EMBEDDER_MODEL, "embedding_dims": EMBEDDING_DIMS}},
    "history_db_path": HISTORY_DB_PATH,
    "decay": DECAY_CONFIG,
    "dream": DREAM_CONFIG,
    "graph": GRAPH_CONFIG,
}


set_session_factory(SessionLocal)
initialize_state(DEFAULT_CONFIG)


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    """挂载 Dream 的进程内调度线程（设计 §5.6）。

    调度器只在 `dream.enabled=true` 时真正启动周期线程；关闭态下 `start()` 立即返回，
    `POST /dream/*` 由端点翻译成 409。关停信号置位后线程在单簇边界处退出。
    """
    scheduler = DreamScheduler(
        get_config=get_current_config,
        get_memory=get_memory_instance,
        observer=SqlRunObserver(),
    )
    configure_scheduler(scheduler)
    scheduler.start()
    try:
        yield
    finally:
        scheduler.stop()
        configure_scheduler(None)


app = FastAPI(
    title="Mem0 REST APIs",
    description=(
        "A REST API for managing and searching memories for your AI Agents and Apps.\n\n"
        "## Authentication\n"
        "Supports Bearer JWT tokens, per-user API keys via `X-API-Key` header, "
        "or the legacy `ADMIN_API_KEY` environment variable. Set `AUTH_DISABLED=true` for local development only."
    ),
    version="1.0.0",
    redirect_slashes=False,
    lifespan=_lifespan,
)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_exception_handler(UpstreamError, upstream_error_handler)
DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "http://localhost:3000")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[DASHBOARD_URL],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router.router)
app.include_router(api_keys_router.router)
app.include_router(entities_router.router)
app.include_router(requests_router.router)
app.include_router(dream_router.router)
app.include_router(graph_router.router)


class Message(BaseModel):
    role: str = Field(..., description="Role of the message (user or assistant).")
    content: str = Field(..., description="Message content.")


class MemoryCreate(BaseModel):
    messages: List[Message] = Field(..., description="List of messages to store.")
    user_id: Optional[str] = None
    agent_id: Optional[str] = None
    run_id: Optional[str] = None
    metadata: Optional[Dict[str, Any]] = None
    expiration_date: Optional[str] = Field(None, description="Expiration date in YYYY-MM-DD format.")
    infer: Optional[bool] = Field(None, description="Whether to extract facts from messages. Defaults to True.")
    memory_type: Optional[str] = Field(None, description="Type of memory to store (e.g. 'core').")
    prompt: Optional[str] = Field(None, description="Custom prompt to use for fact extraction.")


class MemoryUpdate(BaseModel):
    text: Optional[str] = Field(None, description="New content to update the memory with.")
    metadata: Optional[Dict[str, Any]] = Field(None, description="Metadata to update.")
    expiration_date: Optional[str] = Field(None, description="Expiration date in YYYY-MM-DD format, or null to clear.")


class SearchRequest(BaseModel):
    query: str = Field(..., description="Search query.")
    user_id: Optional[str] = Field(None, description="Deprecated: pass inside `filters` instead.", deprecated=True)
    run_id: Optional[str] = Field(None, description="Deprecated: pass inside `filters` instead.", deprecated=True)
    agent_id: Optional[str] = Field(None, description="Deprecated: pass inside `filters` instead.", deprecated=True)
    filters: Optional[Dict[str, Any]] = None
    top_k: Optional[int] = Field(None, description="Maximum number of results to return.")
    threshold: Optional[float] = Field(None, description="Minimum similarity score for results.")
    explain: Optional[bool] = Field(None, description="Include score details for each search result.")
    show_expired: Optional[bool] = Field(None, description="Include expired memories.")
    as_of: Optional[str] = Field(
        None,
        description="Point-in-time instant (YYYY-MM-DD or ISO8601). Returns the facts valid at that moment.",
    )
    include_invalidated: Optional[bool] = Field(
        None, description="Return invalidated facts too, ignoring the bi-temporal validity filter."
    )
    include_observations: Optional[bool] = Field(
        None,
        description=(
            "Include Dream observations in the candidate set. Defaults to false: observations "
            "are synthesized beliefs, not user memories, so they stay out of the memory-injection "
            "path unless asked for. The flag only widens the candidate set -- scoring is untouched."
        ),
    )


class GenerateInstructionsRequest(BaseModel):
    use_case: str = Field(..., description="Description of what the user will use Mem0 for.")


def _client_error(exc: Exception) -> HTTPException:
    """Map core validation / not-found errors to 4xx so clients can tell a bad
    request from an upstream outage. 'not found' is a 404, everything else a 400."""
    detail = str(exc)
    status_code = 404 if isinstance(exc, ValueError) and "not found" in detail.lower() else 400
    return HTTPException(status_code=status_code, detail=detail)


def _redact_config(value: Any, key: str | None = None) -> Any:
    if isinstance(value, dict):
        return {item_key: _redact_config(item_value, item_key) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [_redact_config(item_value, key) for item_value in value]
    if key is not None and key.lower() in SENSITIVE_CONFIG_KEYS:
        return "[redacted]" if value else value
    return value


def _validate_bundled_providers(config: Dict[str, Any]) -> None:
    llm = config.get("llm")
    if isinstance(llm, dict) and (provider := llm.get("provider")) and provider not in BUNDLED_LLM_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"LLM provider '{provider}' is not bundled in this image. "
                f"Bundled providers: {', '.join(BUNDLED_LLM_PROVIDERS)}. "
                "To use another provider, install its Python package, rebuild the container, "
                "and extend BUNDLED_LLM_PROVIDERS in server/main.py."
            ),
        )

    embedder = config.get("embedder")
    if (
        isinstance(embedder, dict)
        and (provider := embedder.get("provider"))
        and provider not in BUNDLED_EMBEDDER_PROVIDERS
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Embedder provider '{provider}' is not bundled in this image. "
                f"Bundled providers: {', '.join(BUNDLED_EMBEDDER_PROVIDERS)}. "
                "To use another provider, install its Python package, rebuild the container, "
                "and extend BUNDLED_EMBEDDER_PROVIDERS in server/main.py."
            ),
        )


def _should_log_request(request: Request) -> bool:
    if request.method == "OPTIONS":
        return False
    path = request.url.path
    if path in SKIPPED_REQUEST_LOG_PATHS:
        return False
    return not path.startswith(SKIPPED_REQUEST_LOG_PREFIXES)


def _persist_request_log(method: str, path: str, status_code: int, latency_ms: float, auth_type: str) -> None:
    session = SessionLocal()

    try:
        session.add(
            RequestLog(
                method=method,
                path=path,
                status_code=status_code,
                latency_ms=latency_ms,
                auth_type=auth_type,
            )
        )
        session.commit()
    except Exception:
        session.rollback()
        logging.exception("Failed to persist request log")
    finally:
        session.close()


@app.middleware("http")
async def log_requests(request: Request, call_next):
    request.state.auth_type = getattr(request.state, "auth_type", "none")
    rid = new_request_id()
    token = request_id_var.set(rid)
    start = time.perf_counter()
    status_code = 500

    try:
        response = await call_next(request)
        status_code = response.status_code
        response.headers["X-Request-ID"] = rid
        return response
    except Exception:
        status_code = 500
        raise
    finally:
        request_id_var.reset(token)
        if _should_log_request(request):
            asyncio.get_running_loop().run_in_executor(
                None,
                _persist_request_log,
                request.method,
                request.url.path,
                status_code,
                round((time.perf_counter() - start) * 1000, 2),
                getattr(request.state, "auth_type", "none"),
            )


@app.get("/configure", summary="Get current Mem0 configuration")
def get_config(_auth=Depends(verify_auth)):
    return _redact_config(get_current_config())


@app.get("/configure/providers", summary="List bundled LLM and embedder providers")
def list_bundled_providers(_auth=Depends(verify_auth)):
    return {"llm": list(BUNDLED_LLM_PROVIDERS), "embedder": list(BUNDLED_EMBEDDER_PROVIDERS)}


@app.post("/configure", summary="Configure Mem0")
def set_config(config: Dict[str, Any], _auth=Depends(require_admin)):
    """Set memory configuration. Requires admin role."""
    _validate_bundled_providers(config)
    update_config(config)
    return {"message": "Configuration set successfully"}


@app.post("/generate-instructions", summary="Generate custom instructions from a use case")
def generate_instructions(req: GenerateInstructionsRequest, _auth=Depends(verify_auth)):
    """Generate custom instructions and a contextual test message tailored to a use case."""
    try:
        llm = get_memory_instance().llm
        prompt = (
            "You are configuring a memory system. Given the use case below, produce two things:\n"
            "1. INSTRUCTIONS: A short paragraph of custom instructions telling the memory extraction system "
            "what kinds of facts, preferences, and context to prioritize. Be specific to the use case.\n"
            "2. TEST_MESSAGE: A single realistic sentence a user in this use case would say, suitable for "
            "testing that the memory system works.\n\n"
            "Respond in exactly this format (no markdown, no extra text):\n"
            "INSTRUCTIONS: <your instructions>\n"
            f"TEST_MESSAGE: <your test message>\n\nUse case: {req.use_case}"
        )
        response = llm.generate_response([{"role": "user", "content": prompt}])
        instructions = response
        test_message = "I like to hike on weekends."
        if "INSTRUCTIONS:" in response and "TEST_MESSAGE:" in response:
            parts = response.split("TEST_MESSAGE:")
            instructions = parts[0].replace("INSTRUCTIONS:", "").strip()
            test_message = parts[1].strip()
        return {"custom_instructions": instructions, "test_message": test_message}
    except Exception:
        raise upstream_error()


@app.post("/memories", summary="Create memories")
def add_memory(memory_create: MemoryCreate, _auth=Depends(verify_auth)):
    """Store new memories."""
    if not any([memory_create.user_id, memory_create.agent_id, memory_create.run_id]):
        raise HTTPException(status_code=400, detail="At least one identifier (user_id, agent_id, run_id) is required.")

    params = {k: v for k, v in memory_create.model_dump().items() if v is not None and k != "messages"}
    try:
        response = get_memory_instance().add(messages=[m.model_dump() for m in memory_create.messages], **params)
        if response.get("results"):
            telemetry.log_dashboard_nudge_once(DASHBOARD_URL)
        return JSONResponse(content=response)
    except (ValueError, Mem0ValidationError) as e:
        raise _client_error(e)
    except Exception:
        raise upstream_error()


ALL_MEMORIES_LIMIT = 1000
# Payload keys surfaced as first-class fields: they are pulled out of `metadata` so a
# client never sees a bi-temporal field nested under it (the four fields sit one level
# up, next to `hash` and `created_at`). The two access-footprint fields (memory decay)
# are the same kind of record attribute and are reserved here as well.
_RESERVED_PAYLOAD_KEYS = {
    "data",
    "user_id",
    "agent_id",
    "run_id",
    "hash",
    "created_at",
    "updated_at",
    "expiration_date",
    "valid_at",
    "invalid_at",
    "superseded_by",
    "invalid_reason",
    "last_accessed",
    "access_count",
    # Dream observation payload: an observation's evidence chain is a first-class
    # attribute of the record (design §5.5 / §6.4), so it is never nested in `metadata`.
    "memory_kind",
    "observation_key",
    "source_memory_ids",
    "evidence_count",
    "dream_run_id",
}


def _serialize_memory(row: Any) -> Dict[str, Any]:
    payload = getattr(row, "payload", None) or {}
    return {
        "id": getattr(row, "id", None),
        "memory": payload.get("data"),
        "user_id": payload.get("user_id"),
        "agent_id": payload.get("agent_id"),
        "run_id": payload.get("run_id"),
        "hash": payload.get("hash"),
        "expiration_date": payload.get("expiration_date"),
        # Always present (null when the fact has never been invalidated) so clients can
        # rely on a stable response shape.
        "valid_at": payload.get("valid_at"),
        "invalid_at": payload.get("invalid_at"),
        "superseded_by": payload.get("superseded_by"),
        "invalid_reason": payload.get("invalid_reason"),
        # Access footprint (memory decay): null until the record is first returned by a
        # search, so the response shape stays stable for clients.
        "last_accessed": payload.get("last_accessed"),
        "access_count": payload.get("access_count"),
        # Dream observation attributes: null on a plain fact, which is the "no observation
        # fields at all" case made explicit rather than absent (design §4.1).
        "memory_kind": payload.get("memory_kind"),
        "observation_key": payload.get("observation_key"),
        "source_memory_ids": payload.get("source_memory_ids"),
        "evidence_count": payload.get("evidence_count"),
        "dream_run_id": payload.get("dream_run_id"),
        "metadata": {k: v for k, v in payload.items() if k not in _RESERVED_PAYLOAD_KEYS},
        "created_at": payload.get("created_at"),
        "updated_at": payload.get("updated_at"),
    }


def _list_all_memories(limit: int = ALL_MEMORIES_LIMIT, cursor: Optional[str] = None) -> Dict[str, Any]:
    results = get_memory_instance().vector_store.list(top_k=limit, cursor=cursor)
    rows = results[0] if results and isinstance(results, (list, tuple)) and isinstance(results[0], list) else results or []
    rows = list(rows)
    # Admin all-memory listing (dashboard browsing) is presentation-oriented: sort newest-first.
    # The vector-search path (/search) is untouched, so retrieval semantics stay similarity-based.
    # Qdrant now returns them newest-first via order_by; sort defensively (stable, cheap).
    rows.sort(
        key=lambda r: (getattr(r, "payload", None) or {}).get("created_at") or "",
        reverse=True,
    )
    next_cursor = None
    if rows:
        last = (getattr(rows[-1], "payload", None) or {}).get("created_at")
        if last:
            next_cursor = str(last)
    # Exact total across the collection (admin all-memory listing is unfiltered).
    total = None
    try:
        vs = get_memory_instance().vector_store
        total = vs.client.count(collection_name=vs.collection_name, exact=True).count
    except Exception:
        pass
    return {
        "results": [_serialize_memory(row) for row in rows],
        "next_cursor": next_cursor,
        "total": total,
    }


@app.get("/memories", summary="Get memories")
def get_all_memories(
    request: Request,
    user_id: Optional[str] = None,
    run_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    top_k: Optional[int] = Query(None, ge=0, le=ALL_MEMORIES_LIMIT),
    cursor: Optional[str] = Query(None),
    show_expired: bool = Query(False),
    _auth=Depends(verify_auth),
):
    """Retrieve stored memories. Lists all memories when no identifier is provided (admin only).

    Pagination (admin listing): pass `top_k` (default 1000) plus `cursor` (opaque value
    from the previous response's `next_cursor`) to walk the full history page by page.
    """
    try:
        if not any([user_id, run_id, agent_id]):
            auth_type = getattr(request.state, "auth_type", "none")
            if _auth is not None and _auth.role != "admin" and auth_type not in {"admin_api_key", "disabled"}:
                raise HTTPException(status_code=403, detail="Admin role required to list all memories.")
            # Admin all-memory listing is intentionally raw; scoped get_all below applies expiry visibility.
            return _list_all_memories(limit=top_k if top_k is not None else ALL_MEMORIES_LIMIT, cursor=cursor)
        filters = {
            k: v for k, v in {"user_id": user_id, "run_id": run_id, "agent_id": agent_id}.items() if v
        }
        params = {"filters": filters}
        if top_k is not None:
            params["top_k"] = top_k
        params["show_expired"] = show_expired
        # Management listing stays full: the SDK's default read excludes Dream observations
        # (they are synthesized beliefs, not injected memories), but the dashboard and the
        # export must not show a dataset smaller than the inventory (design §5.5).
        params["include_observations"] = True
        return get_memory_instance().get_all(**params)
    except HTTPException:
        raise
    except Exception:
        raise upstream_error()


# Dream 观察条目标记。与 `mem0.configs.prompts.MEMORY_KIND_OBSERVATION` / `routers/dream.py`
# 同值；此处不 import SDK 常量，避免为取一个字符串而依赖整合内核。
OBSERVATION_KIND = "observation"
_OBSERVATION_FILTER = {"memory_kind": {"eq": OBSERVATION_KIND}}


def _count_with_filter(filters: Dict[str, Any]) -> Optional[int]:
    """按与 `vector_store.list()` 相同的过滤口径取集合内精确条数。

    取不到时返回 None（前端按既有约定回落为「已加载 N+」），绝不因计数失败而让清单
    接口 5xx。仅 Qdrant 实现有 `_create_filter`；其它实现直接跳过精确总数。
    """
    try:
        vs = get_memory_instance().vector_store
        build_filter = getattr(vs, "_create_filter", None)
        if build_filter is None:
            return None
        count_filter = build_filter(filters)
        if count_filter is None:
            return None
        counted = vs.client.count(collection_name=vs.collection_name, count_filter=count_filter, exact=True)
        return int(counted.count)
    except Exception:
        return None


def _list_observations(limit: int = ALL_MEMORIES_LIMIT, cursor: Optional[str] = None) -> Dict[str, Any]:
    """列全部 Dream 观察（最新优先 + keyset 游标）。

    排序与游标口径取自管理面列表：`created_at` 最新优先，`cursor` 取上一页最后一行的
    `created_at`，服务端 `created_at < cursor` 严格递减续读，因此页间永不重叠。

    行为上与 `GET /memories` 的差别只有**末页判定**：本页不足 `limit` 行时显式返回
    `next_cursor = null`（`GET /memories` 在该情形仍回一个游标，由客户端再请求一次
    空页才收敛）。清单是会反复打开的页面，直接给出「已到末页」省掉那次空往返；
    分页参数名的差别见 `get_observations` 的说明。
    """
    filters = _OBSERVATION_FILTER
    results = get_memory_instance().vector_store.list(filters=filters, top_k=limit, cursor=cursor)
    rows = results[0] if results and isinstance(results, (list, tuple)) and isinstance(results[0], list) else results or []
    rows = list(rows)
    rows.sort(
        key=lambda r: (getattr(r, "payload", None) or {}).get("created_at") or "",
        reverse=True,
    )
    next_cursor = None
    if rows and len(rows) == limit:
        last = (getattr(rows[-1], "payload", None) or {}).get("created_at")
        if last:
            next_cursor = str(last)
    return {
        "results": [_serialize_memory(row) for row in rows],
        "next_cursor": next_cursor,
        "total": _count_with_filter(filters),
    }


@app.get("/observations", summary="List Dream observations")
def get_observations(
    page_size: Optional[int] = Query(None, ge=0, le=ALL_MEMORIES_LIMIT),
    cursor: Optional[str] = Query(None),
    _auth=Depends(verify_auth),
):
    """列出全部 Dream 观察条目（`memory_kind == "observation"`），只读。

    行口径与 `GET /memories` 完全一致（同一序列化器），因此观察的正文、证据链与
    bi-temporal 字段一并返回；分页用 `page_size` + `cursor`（上一页响应的 `next_cursor`）。

    分页参数名与 `GET /memories` 不同是**有意**的：`GET /memories` 的 `top_k` 是上游既有
    对外契约（`upstream/main` 即如此），本批不改名；而本端点为本批新增、无既有调用方，
    故取列表接口更通用的 `page_size`，不复用 `top_k` 这个语义偏「检索条数」的拼法。
    页上限（`ALL_MEMORIES_LIMIT`）与 `cursor` 语义两端点一致。

    为什么另开端点而不复用 `GET /memories`：管理面列表是 raw 全量，观察在其中按
    `created_at` 位置漂移，客户端只能在自己已加载的页里过滤，写入量一涨就会误报空集；
    这里由服务端按 `memory_kind` 的 keyword 索引过滤（一次请求即可拿全量清单）。
    """
    try:
        return _list_observations(
            limit=page_size if page_size is not None else ALL_MEMORIES_LIMIT,
            cursor=cursor,
        )
    except HTTPException:
        raise
    except Exception:
        raise upstream_error()


MAX_EXPORT_PAGES = 200
EXPORT_CSV_COLUMNS = [
    "id",
    "memory",
    "user_id",
    "agent_id",
    "run_id",
    "hash",
    "valid_at",
    "invalid_at",
    "superseded_by",
    "invalid_reason",
    "last_accessed",
    "access_count",
    "memory_kind",
    "observation_key",
    "source_memory_ids",
    "evidence_count",
    "dream_run_id",
    "created_at",
    "updated_at",
]


@app.get("/memories/export", summary="Export all memories")
def export_memories(
    format: str = Query("json", pattern="^(json|csv)$"),
    _auth=Depends(verify_auth),
):
    """Download the whole collection as one JSON or CSV file.

    Walks the same cursor-paginated listing the dashboard browses, so the export
    covers every memory rather than only the newest page.
    """
    try:
        rows: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        for _ in range(MAX_EXPORT_PAGES):
            page = _list_all_memories(limit=ALL_MEMORIES_LIMIT, cursor=cursor)
            batch = page["results"]
            if not batch:
                break
            rows.extend(batch)
            cursor = page.get("next_cursor")
            if not cursor:
                break

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        if format == "csv":
            buffer = io.StringIO()
            writer = csv.DictWriter(
                buffer, fieldnames=EXPORT_CSV_COLUMNS, extrasaction="ignore"
            )
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
            return Response(
                content=buffer.getvalue(),
                media_type="text/csv",
                headers={
                    "Content-Disposition": f'attachment; filename="mem0-memories-{stamp}.csv"'
                },
            )

        return JSONResponse(
            content={"memories": rows, "total": len(rows), "exported_at": stamp},
            headers={
                "Content-Disposition": f'attachment; filename="mem0-memories-{stamp}.json"'
            },
        )
    except HTTPException:
        raise
    except Exception:
        raise upstream_error()


@app.get("/memories/{memory_id}", summary="Get a memory")
def get_memory(memory_id: str, _auth=Depends(verify_auth)):
    """Retrieve a specific memory by ID."""
    try:
        return get_memory_instance().get(memory_id)
    except Exception:
        raise upstream_error()


@app.post("/search", summary="Search memories")
def search_memories(search_req: SearchRequest, _auth=Depends(verify_auth)):
    """Search for memories based on a query."""
    try:
        filters = search_req.filters or {}
        deprecated_keys = []
        for entity_key in ("user_id", "agent_id", "run_id"):
            entity_val = getattr(search_req, entity_key, None)
            if entity_val:
                filters[entity_key] = entity_val
                deprecated_keys.append(entity_key)
        if deprecated_keys:
            logging.warning(
                "Top-level %s in /search is deprecated. Use filters={%s} instead.",
                ", ".join(deprecated_keys),
                ", ".join(f'"{k}": "..."' for k in deprecated_keys),
            )
        params = {}
        if search_req.top_k is not None:
            params["top_k"] = search_req.top_k
        if search_req.threshold is not None:
            params["threshold"] = search_req.threshold
        if search_req.explain is not None:
            params["explain"] = search_req.explain
        if search_req.show_expired is not None:
            params["show_expired"] = search_req.show_expired
        if search_req.as_of is not None:
            params["as_of"] = search_req.as_of
        if search_req.include_invalidated is not None:
            params["include_invalidated"] = search_req.include_invalidated
        if search_req.include_observations is not None:
            params["include_observations"] = search_req.include_observations
        return get_memory_instance().search(query=search_req.query, filters=filters, **params)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException:
        raise
    except Exception:
        raise upstream_error()


@app.put("/memories/{memory_id}", summary="Update a memory")
def update_memory(memory_id: str, updated_memory: MemoryUpdate, _auth=Depends(verify_auth)):
    """Update an existing memory."""
    try:
        fields_set = getattr(updated_memory, "model_fields_set", getattr(updated_memory, "__fields_set__", set()))
        params = {"memory_id": memory_id}
        if "text" in fields_set:
            params["data"] = updated_memory.text
        if "metadata" in fields_set:
            params["metadata"] = updated_memory.metadata
        if "expiration_date" in fields_set:
            params["expiration_date"] = updated_memory.expiration_date
        return get_memory_instance().update(**params)
    except (ValueError, Mem0ValidationError) as e:
        raise _client_error(e)
    except Exception:
        raise upstream_error()


@app.get("/memories/{memory_id}/history", summary="Get memory history")
def memory_history(memory_id: str, _auth=Depends(verify_auth)):
    """Retrieve memory history."""
    try:
        return get_memory_instance().history(memory_id=memory_id)
    except Exception:
        raise upstream_error()


@app.delete("/memories/{memory_id}", summary="Delete a memory", response_model=MessageResponse)
def delete_memory(memory_id: str, _auth=Depends(verify_auth)):
    """Delete a specific memory by ID."""
    try:
        get_memory_instance().delete(memory_id=memory_id)
        return MessageResponse(message="Memory deleted successfully")
    except (ValueError, Mem0ValidationError) as e:
        raise _client_error(e)
    except Exception:
        raise upstream_error()


@app.delete("/memories", summary="Delete all memories", response_model=MessageResponse)
def delete_all_memories(
    user_id: Optional[str] = None,
    run_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    _auth=Depends(require_admin),
):
    """Delete all memories for a given identifier. Requires admin role."""
    if not any([user_id, run_id, agent_id]):
        raise HTTPException(status_code=400, detail="At least one identifier is required.")
    try:
        params = {
            k: v for k, v in {"user_id": user_id, "run_id": run_id, "agent_id": agent_id}.items() if v
        }
        get_memory_instance().delete_all(**params)
        return MessageResponse(message="All relevant memories deleted")
    except Exception:
        raise upstream_error()


@app.post("/reset", summary="Reset all memories")
def reset_memory(_auth=Depends(require_admin)):
    """Completely reset stored memories. Requires admin role."""
    try:
        get_memory_instance().reset()
        return {"message": "All memories reset"}
    except Exception:
        raise upstream_error()


@app.get("/", summary="Redirect to the OpenAPI documentation", include_in_schema=False)
def home():
    """Redirect to the OpenAPI documentation."""
    return RedirectResponse(url="/docs")
