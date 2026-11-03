"""FastAPI Dynamic CRUD Engine, Chaos Middleware, HMAC-JWT Mock Auth, Codegen & Live Inspector API."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import math
import random
import time
from collections import deque
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse

from . import __version__, seeder
from .store import DataStore, validate_collection_name, validate_json_values

STATIC_DIR = Path(__file__).resolve().parent / "static"
JWT_SECRET = b"quickmock-local-dev-hmac-secret-key"

CHAOS_PRESETS: dict[str, dict[str, Any]] = {
    "clean": {"delay_ms": 0, "jitter_ms": 0, "error_rate": 0.0, "error_status": 500},
    "slow_3g": {"delay_ms": 850, "jitter_ms": 250, "error_rate": 0.0, "error_status": 500},
    "flaky": {"delay_ms": 120, "jitter_ms": 60, "error_rate": 25.0, "error_status": 503},
    "rate_limit": {"delay_ms": 20, "jitter_ms": 0, "error_rate": 100.0, "error_status": 429},
    "outage": {"delay_ms": 40, "jitter_ms": 0, "error_rate": 100.0, "error_status": 503},
}


@dataclass
class ChaosConfig:
    delay_ms: int = 0
    jitter_ms: int = 0
    error_rate: float = 0.0  # 0.0 to 100.0 (percentage)
    error_status: int = 500
    require_auth: bool = False
    read_only: bool = False


@dataclass
class RequestLogEntry:
    id: int
    timestamp: str
    method: str
    path: str
    query: str
    status: int
    duration_ms: float
    chaos_tag: str | None = None


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * ((4 - len(s) % 4) % 4)
    return base64.urlsafe_b64decode(s + pad)


def _make_mock_jwt(payload: dict[str, Any]) -> str:
    header_b64 = _b64url_encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body_b64 = _b64url_encode(json.dumps(payload).encode())
    signing_input = f"{header_b64}.{body_b64}".encode("ascii")
    sig = _b64url_encode(hmac.new(JWT_SECRET, signing_input, hashlib.sha256).digest())
    return f"{header_b64}.{body_b64}.{sig}"


def _verify_mock_jwt(token: str) -> dict[str, Any] | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
        expected_sig = _b64url_encode(hmac.new(JWT_SECRET, signing_input, hashlib.sha256).digest())
        if not hmac.compare_digest(parts[2], expected_sig):
            return None
        payload = json.loads(_b64url_decode(parts[1]).decode("utf-8"))
        if not isinstance(payload, dict):
            return None
        if "exp" in payload and int(payload["exp"]) < int(time.time()):
            return None
        return payload
    except Exception:
        return None


def _strict_int(val: Any, name: str) -> int:
    if isinstance(val, bool) or val is None:
        raise ValueError(f"'{name}' must be an integer, got {type(val).__name__}")
    result = int(val)
    if isinstance(val, float) and val != result:
        raise ValueError(f"'{name}' must be an integer")
    return result


def _strict_float(val: Any, name: str) -> float:
    if isinstance(val, bool) or val is None:
        raise ValueError(f"'{name}' must be a number, got {type(val).__name__}")
    f = float(val)
    if not math.isfinite(f):
        raise ValueError(f"'{name}' must be a finite number")
    return f


def _strict_bool(val: Any, name: str) -> bool:
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        low = val.strip().lower()
        if low in ("true", "1", "yes"):
            return True
        if low in ("false", "0", "no"):
            return False
    raise ValueError(f"'{name}' must be a boolean")


async def _read_json_dict(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception as ex:
        raise HTTPException(status_code=400, detail=f"Invalid JSON payload: {ex}")
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Request body must be a JSON object")
    try:
        validate_json_values(body)
    except ValueError as ex:
        raise HTTPException(status_code=400, detail=str(ex))
    return body


def _extract_query_params(request: Request) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for k in set(request.query_params.keys()):
        vals = request.query_params.getlist(k)
        if k in ("_expand", "expand", "_embed", "embed"):
            params[k] = ",".join(vals)
        elif len(vals) > 1:
            params[k] = vals
        elif vals:
            params[k] = vals[0]
    return params


def create_app(
    store: DataStore | None = None,
    chaos: ChaosConfig | None = None,
) -> FastAPI:
    if store is None:
        store = DataStore(seeder.generate_preset("ecommerce", count=12, lang="tr"))
    if chaos is None:
        chaos = ChaosConfig(read_only=store.read_only)
    store.read_only = chaos.read_only

    app = FastAPI(
        title="QuickMock API Server",
        version=__version__,
        docs_url=None,
        openapi_url=None,
        redoc_url=None,
    )

    app.state.store = store
    app.state.chaos = chaos
    app.state.logs: deque[RequestLogEntry] = deque(maxlen=150)
    app.state.req_counter = 0
    app.state.chaos_errors_triggered = 0

    @app.exception_handler(OSError)
    async def storage_error(request: Request, exc: OSError):
        return JSONResponse(status_code=507, content={"detail": "Storage operation failed; changes were not saved"})

    @app.middleware("http")
    async def chaos_and_inspector_middleware(request: Request, call_next):
        start_t = time.perf_counter()
        path = request.url.path
        is_api = path.startswith("/api/") and request.method != "OPTIONS"
        is_auth_login = path == "/api/auth/login"
        chaos_cfg: ChaosConfig = app.state.chaos
        chaos_tags: list[str] = []

        # The mock API is intentionally cross-origin; the Studio control plane isn't.
        if path.startswith("/_admin/"):
            origin = request.headers.get("origin")
            expected_origin = f"{request.url.scheme}://{request.url.netloc}"
            if ((origin is not None and origin != expected_origin)
                    or request.headers.get("sec-fetch-site") == "cross-site"):
                return JSONResponse(status_code=403, content={"detail": "Studio admin endpoints require the same origin"})

        if is_api:
            # 1. Artificial Latency + Jitter Injection
            total_delay_ms = chaos_cfg.delay_ms
            if chaos_cfg.jitter_ms > 0:
                total_delay_ms += random.randint(0, chaos_cfg.jitter_ms)
            if total_delay_ms > 0:
                chaos_tags.append(f"delay:{total_delay_ms}ms")
                await asyncio.sleep(total_delay_ms / 1000.0)

            # 2. Chaos Failure Injection
            if chaos_cfg.error_rate > 0 and random.uniform(0, 100) < chaos_cfg.error_rate:
                app.state.chaos_errors_triggered += 1
                chaos_tags.append(f"fault:{chaos_cfg.error_status}")
                dur = round((time.perf_counter() - start_t) * 1000, 1)
                _record_log(app, request, chaos_cfg.error_status, dur, ",".join(chaos_tags))
                return JSONResponse(
                    status_code=chaos_cfg.error_status,
                    content={
                        "error": "Chaos Simulation Fault",
                        "message": f"Simulated HTTP {chaos_cfg.error_status} injected by QuickMock Chaos Engine ({chaos_cfg.error_rate}% rate).",
                        "chaos": True,
                    },
                    headers={"X-QuickMock-Chaos": ",".join(chaos_tags)},
                )

            # 3. Bearer Token Auth Guard
            if chaos_cfg.require_auth and not is_auth_login:
                auth_header = request.headers.get("Authorization", "")
                token_str = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""
                if not token_str or _verify_mock_jwt(token_str) is None:
                    chaos_tags.append("auth:rejected")
                    dur = round((time.perf_counter() - start_t) * 1000, 1)
                    _record_log(app, request, 401, dur, ",".join(chaos_tags))
                    return JSONResponse(
                        status_code=401,
                        content={
                            "error": "Unauthorized",
                            "message": "Missing or invalid Bearer JWT token. Obtain one via POST /api/auth/login or disable Auth Guard.",
                        },
                    )

            # 4. Read-Only Guard
            if chaos_cfg.read_only and request.method in ("POST", "PUT", "PATCH", "DELETE") and not is_auth_login:
                chaos_tags.append("readonly:blocked")
                dur = round((time.perf_counter() - start_t) * 1000, 1)
                _record_log(app, request, 403, dur, ",".join(chaos_tags))
                return JSONResponse(
                    status_code=403,
                    content={
                        "error": "Forbidden",
                        "message": "Server is currently in Read-Only mode.",
                    },
                )

        response: Response = await call_next(request)

        if is_api:
            dur = round((time.perf_counter() - start_t) * 1000, 1)
            tag_str = ",".join(chaos_tags) if chaos_tags else None
            if tag_str:
                response.headers["X-QuickMock-Chaos"] = tag_str
            _record_log(app, request, response.status_code, dur, tag_str)

        return response

    # Register CORSMiddleware AFTER @app.middleware("http") so it wraps the outermost layer
    # and attaches Access-Control-Allow-Origin headers even to early 401/403/503 responses.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Total-Count", "X-QuickMock-Chaos"],
    )

    def _record_log(app_inst: FastAPI, req: Request, status: int, duration_ms: float, chaos_tag: str | None) -> None:
        app_inst.state.req_counter += 1
        entry = RequestLogEntry(
            id=app_inst.state.req_counter,
            timestamp=datetime.now(timezone.utc).strftime("%H:%M:%S.%f")[:-3],
            method=req.method,
            path=req.url.path,
            query=req.url.query,
            status=status,
            duration_ms=duration_ms,
            chaos_tag=chaos_tag,
        )
        app_inst.state.logs.appendleft(entry)

    # --------------------------------------------------------- Dashboard UI & Demo App
    @app.get("/swagger", include_in_schema=False)
    async def swagger():
        return get_swagger_ui_html(openapi_url="/_admin/openapi.json", title="QuickMock API")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    async def dashboard():
        html_path = STATIC_DIR / "index.html"
        if html_path.is_file():
            return html_path.read_text(encoding="utf-8")
        return "<h1>QuickMock Server Running</h1>"

    @app.get("/demo", response_class=HTMLResponse, include_in_schema=False)
    async def demo_storefront():
        demo_path = STATIC_DIR / "demo.html"
        if demo_path.is_file():
            return demo_path.read_text(encoding="utf-8")
        return "<h1>QuickMock Demo App Not Found</h1>"

    # --------------------------------------------------------- Admin Control API
    @app.get("/_admin/state", include_in_schema=False)
    async def admin_state():
        s: DataStore = app.state.store
        c: ChaosConfig = app.state.chaos
        logs = list(app.state.logs)
        return {
            "version": __version__,
            "file_path": str(s.file_path) if s.file_path else "in-memory",
            "chaos": asdict(c),
            "collections": s.collections_summary(),
            "stats": {
                "total_requests": app.state.req_counter,
                "chaos_errors": app.state.chaos_errors_triggered,
                "recent_logs": [asdict(l) for l in logs[:50]],
            },
        }

    @app.patch("/_admin/config", include_in_schema=False)
    async def update_chaos_config(request: Request):
        payload = await _read_json_dict(request)
        c: ChaosConfig = replace(app.state.chaos)
        s: DataStore = app.state.store
        try:
            if "delay_ms" in payload:
                d = _strict_int(payload["delay_ms"], "delay_ms")
                if d < 0:
                    raise ValueError("delay_ms must be >= 0")
                c.delay_ms = min(10000, d)
            if "jitter_ms" in payload:
                j = _strict_int(payload["jitter_ms"], "jitter_ms")
                if j < 0:
                    raise ValueError("jitter_ms must be >= 0")
                c.jitter_ms = min(5000, j)
            if "error_rate" in payload:
                er = _strict_float(payload["error_rate"], "error_rate")
                if not (0.0 <= er <= 100.0):
                    raise ValueError("error_rate must be between 0 and 100")
                c.error_rate = er
            if "error_status" in payload:
                es = _strict_int(payload["error_status"], "error_status")
                if not (400 <= es <= 599):
                    raise ValueError("error_status must be an HTTP error code between 400 and 599")
                c.error_status = es
            if "require_auth" in payload:
                c.require_auth = _strict_bool(payload["require_auth"], "require_auth")
            if "read_only" in payload:
                c.read_only = _strict_bool(payload["read_only"], "read_only")
        except (ValueError, TypeError, OverflowError) as ex:
            raise HTTPException(status_code=400, detail=f"Invalid chaos configuration: {ex}")
        app.state.chaos = c
        s.read_only = c.read_only
        return asdict(c)

    @app.post("/_admin/chaos-preset", include_in_schema=False)
    async def apply_chaos_preset(request: Request):
        payload = await _read_json_dict(request)
        preset_name = payload.get("preset", "clean")
        if not isinstance(preset_name, str) or preset_name not in CHAOS_PRESETS:
            raise HTTPException(status_code=400, detail=f"Unknown chaos preset: '{preset_name}'")
        c: ChaosConfig = app.state.chaos
        s: DataStore = app.state.store
        vals = CHAOS_PRESETS[preset_name]
        c.delay_ms = vals["delay_ms"]
        c.jitter_ms = vals["jitter_ms"]
        c.error_rate = vals["error_rate"]
        c.error_status = vals["error_status"]
        if preset_name == "clean":
            c.require_auth = False
            c.read_only = False
            s.read_only = False
        return asdict(c)

    @app.post("/_admin/reset", include_in_schema=False)
    async def reset_store():
        s: DataStore = app.state.store
        counts = s.reset()
        return {"status": "reset", "collections": counts}

    @app.post("/_admin/seed", include_in_schema=False)
    async def seed_store(request: Request):
        payload = await _read_json_dict(request)
        s: DataStore = app.state.store
        preset = str(payload.get("preset", "ecommerce"))
        lang = str(payload.get("lang", "tr"))
        mode = str(payload.get("mode", "replace"))
        schema_str = str(payload.get("schema", "")).strip()

        try:
            if mode not in ("merge", "replace"):
                raise ValueError("mode must be 'merge' or 'replace'")
            if lang not in ("tr", "en"):
                raise ValueError("lang must be 'tr' or 'en'")
            raw_count = payload.get("count", 12)
            count = _strict_int(raw_count, "count")
            if not (1 <= count <= 200):
                raise ValueError("count must be between 1 and 200")
            if schema_str:
                new_data = seeder.generate_from_schema(schema_str, count=count, lang=lang)
            else:
                new_data = seeder.generate_preset(preset, count=count, lang=lang, seed=random.randint(1, 99999))

            if mode == "merge":
                counts = s.merge_collections(new_data)
            else:
                counts = s.replace_all(new_data, update_snapshot=True)
        except (ValueError, TypeError, OverflowError) as ex:
            raise HTTPException(status_code=400, detail=str(ex))
        return {"status": "seeded", "collections": counts}

    @app.delete("/_admin/collections/{collection}", include_in_schema=False)
    async def remove_collection(collection: str):
        s: DataStore = app.state.store
        if not s.delete_collection(collection):
            raise HTTPException(status_code=404, detail=f"Collection '{collection}' not found")
        return {"deleted": True, "collection": collection}

    @app.get("/_admin/codegen/{collection}", include_in_schema=False)
    async def codegen_collection(collection: str, request: Request):
        s: DataStore = app.state.store
        if not s.has_collection(collection):
            raise HTTPException(status_code=404, detail=f"Collection '{collection}' not found")
        base_url = f"{request.url.scheme}://{request.url.netloc}"
        return s.generate_typescript(collection, base_url=base_url)

    @app.delete("/_admin/logs", include_in_schema=False)
    async def clear_logs():
        app.state.logs.clear()
        app.state.req_counter = 0
        app.state.chaos_errors_triggered = 0
        return {"status": "cleared"}

    @app.get("/openapi.json", include_in_schema=False)
    @app.get("/_admin/openapi.json", include_in_schema=False)
    async def export_openapi(request: Request):
        s: DataStore = app.state.store
        base_url = f"{request.url.scheme}://{request.url.netloc}"
        return s.generate_openapi(base_url=base_url)

    # --------------------------------------------------------- Mock Auth Endpoints
    @app.post("/api/auth/login", tags=["auth"])
    async def mock_login(request: Request):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                body = {}
        except Exception:
            body = {}
        identifier = str(body.get("email") or body.get("username") or "dev@quickmock.local")
        role = str(body.get("role") or "admin")
        now = int(time.time())
        token_payload = {
            "sub": str(body.get("id", "1")),
            "email": identifier,
            "role": role,
            "iat": now,
            "exp": now + 86400,
        }
        token = _make_mock_jwt(token_payload)
        return {
            "access_token": token,
            "token_type": "Bearer",
            "expires_in": 86400,
            "user": {"id": token_payload["sub"], "email": identifier, "role": role},
        }

    @app.get("/api/auth/me", tags=["auth"])
    async def mock_me(request: Request):
        auth = request.headers.get("Authorization", "")
        token_str = auth[7:].strip() if auth.startswith("Bearer ") else ""
        claims = _verify_mock_jwt(token_str) if token_str else None
        if not claims:
            raise HTTPException(status_code=401, detail="Invalid or missing Bearer JWT token")
        return {
            "id": claims.get("sub", "1"),
            "email": claims.get("email", "dev@quickmock.local"),
            "role": claims.get("role", "admin"),
            "authenticated": True,
            "claims": claims,
        }

    # --------------------------------------------------------- Dynamic CRUD Endpoints
    @app.get("/api/{collection}/_stats")
    async def get_collection_stats(collection: str):
        s: DataStore = app.state.store
        if not s.has_collection(collection):
            raise HTTPException(status_code=404, detail=f"Collection '{collection}' not found")
        try:
            return s.collection_stats(collection)
        except ValueError as ex:
            raise HTTPException(status_code=400, detail=str(ex))

    @app.post("/api/{collection}/_bulk", status_code=201)
    async def bulk_create_collection_items(collection: str, request: Request):
        s: DataStore = app.state.store
        try:
            validate_collection_name(collection)
            payload = await request.json()
        except ValueError as ex:
            raise HTTPException(status_code=400, detail=str(ex))
        except Exception as ex:
            raise HTTPException(status_code=400, detail=f"Invalid JSON array payload: {ex}")
        try:
            created = s.create_bulk(collection, payload)
        except ValueError as ex:
            code = 409 if "Duplicate id" in str(ex) else 400
            raise HTTPException(status_code=code, detail=str(ex))
        return {"inserted": len(created), "items": created}

    @app.get("/api/{collection}")
    async def list_collection(collection: str, request: Request, response: Response):
        s: DataStore = app.state.store
        if not s.has_collection(collection):
            raise HTTPException(status_code=404, detail=f"Collection '{collection}' not found")
        params = _extract_query_params(request)
        try:
            rows, total = s.query(collection, params)
        except ValueError as ex:
            raise HTTPException(status_code=400, detail=str(ex))
        response.headers["X-Total-Count"] = str(total)
        return rows

    @app.get("/api/{collection}/{item_id}")
    async def get_single_item(collection: str, item_id: str, request: Request):
        s: DataStore = app.state.store
        if not s.has_collection(collection):
            raise HTTPException(status_code=404, detail=f"Collection '{collection}' not found")
        q = _extract_query_params(request)
        expand = q.get("_expand") or q.get("expand") or ""
        embed = q.get("_embed") or q.get("embed") or ""
        item = s.get_by_id(collection, item_id, expand_raw=expand, embed_raw=embed)
        if item is None:
            raise HTTPException(status_code=404, detail=f"Item '{item_id}' not found in '{collection}'")
        return item

    @app.post("/api/{collection}", status_code=201)
    async def create_collection_item(collection: str, request: Request):
        s: DataStore = app.state.store
        try:
            validate_collection_name(collection)
        except ValueError as ex:
            raise HTTPException(status_code=400, detail=str(ex))
        payload = await _read_json_dict(request)
        try:
            created = s.create(collection, payload)
        except ValueError as ex:
            code = 409 if "Duplicate id" in str(ex) else 400
            raise HTTPException(status_code=code, detail=str(ex))
        return created

    @app.put("/api/{collection}/{item_id}")
    async def replace_collection_item(collection: str, item_id: str, request: Request):
        s: DataStore = app.state.store
        if not s.has_collection(collection):
            raise HTTPException(status_code=404, detail=f"Collection '{collection}' not found")
        payload = await _read_json_dict(request)
        try:
            replaced = s.replace(collection, item_id, payload)
        except ValueError as ex:
            raise HTTPException(status_code=400, detail=str(ex))
        if replaced is None:
            raise HTTPException(status_code=404, detail=f"Item '{item_id}' not found in '{collection}'")
        return replaced

    @app.patch("/api/{collection}/{item_id}")
    async def update_collection_item(collection: str, item_id: str, request: Request):
        s: DataStore = app.state.store
        if not s.has_collection(collection):
            raise HTTPException(status_code=404, detail=f"Collection '{collection}' not found")
        payload = await _read_json_dict(request)
        try:
            updated = s.update(collection, item_id, payload)
        except ValueError as ex:
            raise HTTPException(status_code=400, detail=str(ex))
        if updated is None:
            raise HTTPException(status_code=404, detail=f"Item '{item_id}' not found in '{collection}'")
        return updated

    @app.delete("/api/{collection}/{item_id}")
    async def delete_collection_item(collection: str, item_id: str):
        s: DataStore = app.state.store
        if not s.has_collection(collection):
            raise HTTPException(status_code=404, detail=f"Collection '{collection}' not found")
        ok = s.delete(collection, item_id)
        if not ok:
            raise HTTPException(status_code=404, detail=f"Item '{item_id}' not found in '{collection}'")
        return {"deleted": True, "id": item_id}

    return app
