from __future__ import annotations

import asyncio
import json
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from jobs import (
    ActiveJobError,
    CapacityError,
    ClaimRetryError,
    CreationRateLimitError,
    JobManager,
)
from providers import (
    GEMINI_HEADER,
    TAVILY_HEADER,
    InvalidProviderCredentials,
    ProviderCredentials,
)

# Load backend configuration before constructing provider clients or the job manager.
load_dotenv()

MAX_REQUEST_BYTES = 256 * 1024
EXTENSION_ORIGIN = re.compile(r"^chrome-extension://[a-p]{32}$")
PUBLIC_DIR = Path(__file__).resolve().parent / "public"


def _env_int(name: str, default: int, *aliases: str) -> int:
    value = next(
        (value for key in (name, *aliases) if (value := os.getenv(key)) is not None),
        None,
    )
    try:
        parsed = int(value) if value is not None else default
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer.") from exc
    if parsed < 1:
        raise RuntimeError(f"{name} must be positive.")
    return parsed


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    try:
        parsed = float(value) if value is not None else default
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number.") from exc
    if parsed <= 0:
        raise RuntimeError(f"{name} must be positive.")
    return parsed


class RequestSizeLimitMiddleware:
    def __init__(self, app, max_bytes: int):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or scope.get("method") not in {"POST", "PUT", "PATCH"}
            or not scope.get("path", "").startswith("/v1/")
        ):
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        try:
            content_length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            content_length = 0
        if content_length > self.max_bytes:
            await self._reject(scope, receive, send)
            return

        messages = []
        received = 0
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    await self._reject(scope, receive, send)
                    return
                if not message.get("more_body", False):
                    break
            elif message["type"] == "http.disconnect":
                break

        index = 0

        async def replay_receive():
            nonlocal index
            if index < len(messages):
                message = messages[index]
                index += 1
                return message
            return await receive()

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _reject(scope, receive, send):
        response = JSONResponse(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            content={"detail": "Request body is too large."},
        )
        await response(scope, receive, send)


class CheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    type: Literal["page", "url", "text"]
    url: str | None = Field(default=None, max_length=2048)
    title: str | None = Field(default=None, max_length=500)
    text: str | None = Field(default=None, max_length=200_000)

    @model_validator(mode="after")
    def validate_variant(self) -> "CheckRequest":
        if self.type == "url":
            if not self.url:
                raise ValueError("A URL check requires url.")
            if self.title is not None or self.text is not None:
                raise ValueError("A URL check accepts only url.")
        elif self.type == "text":
            if not self.text:
                raise ValueError("A text check requires text.")
            if self.url is not None or self.title is not None:
                raise ValueError("A text check accepts only text.")
        elif not self.text:
            raise ValueError("A page check requires text.")
        return self

    def job_payload(self) -> dict:
        if self.type == "url":
            return {"url": self.url}
        metadata = {"source": self.type}
        if self.type == "page":
            if self.url is not None:
                metadata["url"] = self.url
            if self.title is not None:
                metadata["title"] = self.title
        return {"text": self.text, "metadata": metadata}


def _manager_from_env() -> JobManager:
    return JobManager(
        max_workers=_env_int(
            "JOB_MAX_WORKERS", 1, "FACT_CHECKER_MAX_WORKERS", "FACT_CHECK_MAX_WORKERS"
        ),
        capacity=_env_int(
            "JOB_CAPACITY",
            8,
            "JOB_QUEUE_CAPACITY",
            "FACT_CHECKER_JOB_CAPACITY",
            "FACT_CHECK_JOB_CAPACITY",
        ),
        rate_limit=_env_int(
            "JOB_CREATION_RATE_LIMIT",
            5,
            "JOB_RATE_LIMIT_PER_MINUTE",
            "FACT_CHECKER_CREATION_RATE_LIMIT",
            "FACT_CHECK_CREATION_RATE_LIMIT",
        ),
        ttl_seconds=_env_int(
            "JOB_TTL_SECONDS",
            3600,
            "FACT_CHECKER_JOB_TTL_SECONDS",
            "FACT_CHECK_JOB_TTL_SECONDS",
        ),
        history_limit=_env_int(
            "JOB_EVENT_HISTORY",
            256,
            "JOB_EVENT_HISTORY_LIMIT",
            "FACT_CHECKER_EVENT_HISTORY",
            "FACT_CHECK_EVENT_HISTORY",
        ),
        gemini_concurrency=_env_int("GEMINI_CONCURRENCY", 3),
        tavily_concurrency=_env_int("TAVILY_CONCURRENCY", 4),
    )


def create_app(manager_factory=_manager_from_env) -> FastAPI:
    environment = os.getenv(
        "APP_ENV",
        os.getenv("FACT_CHECKER_ENV", os.getenv("ENVIRONMENT", "development")),
    ).lower()
    production = environment in {"production", "prod"}
    origins = [
        origin.strip()
        for origin in os.getenv(
            "CORS_ORIGINS", os.getenv("FACT_CHECKER_CORS_ORIGINS", "")
        ).split(",")
        if origin.strip()
    ]
    if production and (
        not origins or any(EXTENSION_ORIGIN.fullmatch(origin) is None for origin in origins)
    ):
        raise RuntimeError(
            "Production CORS_ORIGINS must contain exact chrome-extension:// origins."
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.job_manager = manager_factory()
        try:
            yield
        finally:
            await asyncio.to_thread(app.state.job_manager.shutdown)

    application = FastAPI(
        title="Fact Checker API",
        version="1",
        lifespan=lifespan,
        docs_url=None if production else "/docs",
        redoc_url=None if production else "/redoc",
        openapi_url=None if production else "/openapi.json",
    )
    application.add_middleware(
        RequestSizeLimitMiddleware,
        max_bytes=MAX_REQUEST_BYTES,
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=origins if production else [],
        allow_origin_regex=None if production else r"^chrome-extension://[a-p]{32}$",
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=[
            "Accept",
            "Authorization",
            "Content-Type",
            "Last-Event-ID",
            "X-Client-Id",
            GEMINI_HEADER,
            TAVILY_HEADER,
        ],
        allow_private_network=True,
    )

    @application.middleware("http")
    async def security_headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=()"
        )
        if request.url.path.startswith("/v1/"):
            response.headers["Cache-Control"] = "no-store"
        if production:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        return response

    @application.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError):
        details = [
            {
                "loc": list(error.get("loc", ())),
                "msg": "Invalid request.",
                "type": error.get("type", "value_error"),
            }
            for error in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": details})

    def bearer_token(request: Request) -> str:
        supplied = request.headers.get("authorization", "")
        scheme, separator, token = supplied.partition(" ")
        if not separator or scheme.lower() != "bearer" or not token:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized.",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return token

    def manager(request: Request) -> JobManager:
        return request.app.state.job_manager

    def client_identity(request: Request) -> tuple[str, tuple[str, str]]:
        address = request.client.host if request.client else "unknown"
        install_id = request.headers.get("x-client-id", "")
        if install_id and (
            len(install_id) > 128
            or not all(character.isalnum() or character in "-_" for character in install_id)
        ):
            raise HTTPException(status_code=400, detail="Invalid client identifier.")
        client = f"{address}:{install_id or 'anonymous'}"
        return client, (f"ip:{address}", f"client:{client}")

    def provider_credentials(request: Request) -> ProviderCredentials:
        try:
            return ProviderCredentials.create(
                request.headers.get(GEMINI_HEADER),
                request.headers.get(TAVILY_HEADER),
            )
        except InvalidProviderCredentials as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None

    def authorize_job(
        job_id: str,
        request: Request,
        jobs: JobManager = Depends(manager),
    ) -> None:
        if not jobs.authorize(job_id, bearer_token(request)):
            raise HTTPException(status_code=404, detail="Check not found.")

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    def public_page(name: str) -> FileResponse:
        return FileResponse(
            PUBLIC_DIR / name,
            media_type="text/html",
            headers={
                "Content-Security-Policy": (
                    "default-src 'none'; style-src 'unsafe-inline'; "
                    "img-src 'self'; base-uri 'none'; form-action 'none'; "
                    "frame-ancestors 'none'"
                ),
                "Cache-Control": "public, max-age=3600",
            },
        )

    @application.get("/privacy", include_in_schema=False)
    async def privacy():
        return public_page("privacy.html")

    @application.get("/support", include_in_schema=False)
    async def support():
        return public_page("support.html")

    @application.post(
        "/v1/checks",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def create_check(
        body: CheckRequest,
        request: Request,
        jobs: JobManager = Depends(manager),
    ):
        requester, rate_keys = client_identity(request)
        try:
            return jobs.submit(
                body.type,
                body.job_payload(),
                requester,
                provider_credentials(request),
                rate_keys=rate_keys,
            )
        except ActiveJobError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except CapacityError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None
        except CreationRateLimitError as exc:
            raise HTTPException(
                status_code=429, detail=str(exc), headers={"Retry-After": "60"}
            ) from None

    @application.get(
        "/v1/checks/{job_id}", dependencies=[Depends(authorize_job)]
    )
    async def get_check(
        job_id: str, jobs: JobManager = Depends(manager)
    ):
        snapshot = jobs.get(job_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="Check not found.")
        return snapshot

    @application.delete(
        "/v1/checks/{job_id}",
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(authorize_job)],
    )
    async def cancel_check(
        job_id: str, jobs: JobManager = Depends(manager)
    ):
        snapshot = jobs.cancel(job_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="Check not found.")
        return snapshot

    @application.post(
        "/v1/checks/{job_id}/claims/{claim_index}/retry",
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(authorize_job)],
    )
    async def retry_claim(
        job_id: str,
        claim_index: int,
        request: Request,
        jobs: JobManager = Depends(manager),
    ):
        requester, rate_keys = client_identity(request)
        try:
            snapshot = jobs.retry_claim(
                job_id,
                claim_index,
                requester,
                provider_credentials(request),
                rate_keys=rate_keys,
            )
        except (ActiveJobError, ClaimRetryError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except CapacityError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None
        except CreationRateLimitError as exc:
            raise HTTPException(
                status_code=429, detail=str(exc), headers={"Retry-After": "60"}
            ) from None
        if snapshot is None:
            raise HTTPException(status_code=404, detail="Check not found.")
        return snapshot

    @application.get(
        "/v1/checks/{job_id}/events", dependencies=[Depends(authorize_job)]
    )
    async def stream_check(
        job_id: str,
        jobs: JobManager = Depends(manager),
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ):
        try:
            start_sequence = int(last_event_id) if last_event_id is not None else 0
            if start_sequence < 0:
                raise ValueError
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Last-Event-ID.") from None
        if jobs.get(job_id) is None:
            raise HTTPException(status_code=404, detail="Check not found.")
        heartbeat = _env_float("SSE_HEARTBEAT_SECONDS", 15.0)

        async def generate():
            sequence = start_sequence
            while True:
                update = await asyncio.to_thread(
                    jobs.wait_for_events, job_id, sequence, heartbeat
                )
                if update is None:
                    return
                events, terminal = update
                for event in events:
                    sequence = max(sequence, event["sequence"])
                    data = json.dumps(event["data"], separators=(",", ":"))
                    yield (
                        f"id: {event['sequence']}\n"
                        f"event: {event['event']}\n"
                        f"data: {data}\n\n"
                    )
                if terminal:
                    return
                if not events:
                    yield ": heartbeat\n\n"

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    return application


app = create_app()


if __name__ == "__main__":
    uvicorn.run("api:app", host="127.0.0.1", port=8000, reload=False)
