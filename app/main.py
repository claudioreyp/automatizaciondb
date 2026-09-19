from contextlib import asynccontextmanager
import asyncio

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select

from .api import api, legacy
from .auth import AuthContext, decode_access_token, ensure_branch_scope, require_active_scope, resolve_membership
from .config import get_settings
from .database import Base, SessionLocal, engine
from .errors import CodedHTTPException
from .models import Branch
from .realtime import hub


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings = get_settings()
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    if settings.auto_create_schema and settings.is_development:
        Base.metadata.create_all(bind=engine)
    yield


settings = get_settings()
app = FastAPI(
    title=settings.app_name,
    version="1.0.0",
    description=(
        "API multiempresa para operaciones POS gastronómicas de Escalar AI. "
        "Incluye aislamiento por negocio y sucursal, idempotencia y compatibilidad legacy controlada."
    ),
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "Idempotency-Key",
        "X-Business-Id",
        "X-Branch-Id",
        "X-Dev-Auth",
        "X-Dev-Role",
        "X-Dev-User",
        "X-Dev-Email",
        "X-Integration-Token",
        "X-CSRF-Token",
    ],
)
app.include_router(api)
app.include_router(legacy)


@app.middleware("http")
async def private_password_reset_responses(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/v1/admin/businesses/") and "/password-reset" in request.url.path:
        response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(CodedHTTPException)
async def coded_http_exception(_, exc: CodedHTTPException):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail, "code": exc.code},
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def redact_validation_secrets(_, exc: RequestValidationError):
    errors = []
    for error in exc.errors():
        safe_error = dict(error)
        location = [str(part).lower() for part in safe_error.get("loc", [])]
        if any(any(secret in part for secret in ("password", "pin", "token", "pairing_code")) for part in location):
            safe_error["input"] = "[REDACTED]"
        errors.append(safe_error)
    return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})


def websocket_user(websocket: WebSocket, branch: Branch) -> AuthContext:
    token = websocket.query_params.get("access_token")
    dev_auth = websocket.query_params.get("dev_auth")
    settings = get_settings()
    with SessionLocal() as db:
        require_active_scope(db, branch.business_id, branch.id)
        if settings.is_development and settings.dev_auth_token and dev_auth == settings.dev_auth_token:
            role = websocket.query_params.get("role", "owner")
            user = AuthContext(
                websocket.query_params.get("user_id", "dev-user"),
                role,
                branch.business_id,
                branch.id,
                "dev@impulsa.local",
            )
        elif token:
            claims = decode_access_token(token)
            from .auth_sessions import validate_provider_session
            validate_provider_session(db, token, claims, settings)
            subject = claims.get("sub")
            if not subject:
                raise ValueError("Token has no subject")
            user = resolve_membership(db, subject, branch.business_id, branch.id)
        else:
            from .device_auth import session_user
            user = session_user(db, websocket)
    ensure_branch_scope(user, branch.business_id, branch.id)
    return user


@app.websocket("/api/v1/ws/branches/{branch_id}")
async def branch_websocket(websocket: WebSocket, branch_id: int):
    with SessionLocal() as db:
        branch = db.scalar(select(Branch).where(Branch.id == branch_id, Branch.active.is_(True)))
    if not branch:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Branch not found")
        return
    try:
        await asyncio.to_thread(websocket_user, websocket, branch)
    except Exception as error:
        unavailable = isinstance(error, HTTPException) and error.status_code == 503
        await websocket.close(code=1013 if unavailable else status.WS_1008_POLICY_VIOLATION,
                              reason="Auth temporarily unavailable" if unavailable else "Unauthorized")
        return

    await hub.connect(branch_id, websocket)
    websocket.state.authorize = lambda: websocket_user(websocket, branch)
    try:
        await websocket.send_json({"event": "connected", "payload": {"branch_id": branch_id}})
        while True:
            try:
                await asyncio.wait_for(websocket.receive_text(), timeout=10)
            except asyncio.TimeoutError:
                pass
            try:
                await asyncio.to_thread(websocket_user, websocket, branch)
            except Exception as error:
                unavailable = isinstance(error, HTTPException) and error.status_code == 503
                await websocket.close(code=1013 if unavailable else status.WS_1008_POLICY_VIOLATION,
                                      reason="Auth temporarily unavailable" if unavailable else "Session expired")
                break
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await hub.disconnect(branch_id, websocket)


@app.get("/", include_in_schema=False)
def root():
    return {
        "service": settings.app_name,
        "version": app.version,
        "health": "/api/v1/health",
        "docs": "/docs",
    }
