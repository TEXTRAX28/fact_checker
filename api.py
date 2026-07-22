from __future__ import annotations

import asyncio
import json
import os
import secrets
from contextlib import asynccontextmanager
from typing import Literal

import uvicorn
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from jobs import (
    ActiveJobError,
    CapacityError,
    ClaimRetryError,
    CreationRateLimitError,
    JobManager,
)

# main.py (the CLI) loads .env too; this call is needed here because api.py no
# longer imports main.py (service.py is the shared boundary instead), so that
# side effect doesn't happen for free when running the API standalone.
load_dotenv()


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
    )


def create_app(manager_factory=_manager_from_env) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.job_manager = manager_factory()
        try:
            yield
        finally:
            await asyncio.to_thread(app.state.job_manager.shutdown)

    application = FastAPI(title="Fact Checker API", version="1", lifespan=lifespan)

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
    application.add_middleware(
        CORSMiddleware,
        allow_origins=origins if production else [],
        allow_origin_regex=None if production else r"^chrome-extension://[a-p]{32}$",
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Last-Event-ID"],
        allow_private_network=True,
    )

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

    def authorize(request: Request) -> None:
        expected = os.getenv(
            "API_BEARER_TOKEN",
            os.getenv("FACT_CHECKER_API_TOKEN", os.getenv("FACT_CHECK_API_TOKEN")),
        )
        if not expected:
            return
        supplied = request.headers.get("authorization", "")
        scheme, separator, token = supplied.partition(" ")
        valid = (
            bool(separator)
            and scheme.lower() == "bearer"
            and secrets.compare_digest(token.encode(), expected.encode())
        )
        if not valid:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized.",
                headers={"WWW-Authenticate": "Bearer"},
            )

    def manager(request: Request) -> JobManager:
        return request.app.state.job_manager

    def client_id(request: Request) -> str:
        return request.client.host if request.client else "unknown"

    @application.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.post(
        "/v1/checks",
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(authorize)],
    )
    async def create_check(
        body: CheckRequest,
        request: Request,
        jobs: JobManager = Depends(manager),
    ):
        try:
            return jobs.submit(body.type, body.job_payload(), client_id(request))
        except ActiveJobError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except CapacityError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from None
        except CreationRateLimitError as exc:
            raise HTTPException(
                status_code=429, detail=str(exc), headers={"Retry-After": "60"}
            ) from None

    @application.get(
        "/v1/checks/{job_id}", dependencies=[Depends(authorize)]
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
        dependencies=[Depends(authorize)],
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
        dependencies=[Depends(authorize)],
    )
    async def retry_claim(
        job_id: str,
        claim_index: int,
        request: Request,
        jobs: JobManager = Depends(manager),
    ):
        try:
            snapshot = jobs.retry_claim(job_id, claim_index, client_id(request))
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
        "/v1/checks/{job_id}/events", dependencies=[Depends(authorize)]
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
        heartbeat = float(os.getenv("SSE_HEARTBEAT_SECONDS", "15"))

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
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return application


app = create_app()


if __name__ == "__main__":
    uvicorn.run("api:app", host="127.0.0.1", port=8000, reload=False)
