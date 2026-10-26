"""FastAPI application factory."""

from __future__ import annotations

import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app import __version__
from app.api import health, payments, settlements, webhooks
from app.config import Settings, get_settings
from app.core.idempotency import IdempotencyConflictError, IdempotencyInProgressError
from app.core.routing import PaymentRouter
from app.core.security import SignatureError
from app.core.state_machine import InvalidTransitionError
from app.database import init_db, make_engine, make_session_factory
from app.logging_config import configure_logging, request_id_var
from app.providers import build_providers
from app.services.errors import ServiceError
from app.services.webhooks import WebhookSender

logger = logging.getLogger("app")


def _error(status_code: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status_code)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db(app.state.engine)
        logger.info(
            "gateway started env=%s providers=%s", settings.app_env, sorted(app.state.providers)
        )
        yield
        app.state.engine.dispose()

    app = FastAPI(
        title="Payment Gateway",
        version=__version__,
        description=(
            "Multi-provider payment gateway: bKash, SSLCommerz and Stripe sandbox adapters, "
            "idempotent payment API, signed merchant webhooks and daily settlement."
        ),
        lifespan=lifespan,
    )

    providers = build_providers(settings)
    app.state.settings = settings
    app.state.engine = make_engine(settings.database_url)
    app.state.session_factory = make_session_factory(app.state.engine)
    app.state.providers = providers
    app.state.router = PaymentRouter(providers)
    app.state.webhook_sender = WebhookSender(
        timeout=settings.webhook_timeout_seconds,
        max_attempts=settings.webhook_max_attempts,
        backoff_seconds=settings.webhook_retry_backoff_seconds,
    )

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            request_id_var.reset(token)
        response.headers["X-Request-ID"] = request_id
        logger.info(
            "%s %s -> %s in %.1fms",
            request.method,
            request.url.path,
            response.status_code,
            (time.perf_counter() - started) * 1000,
        )
        return response

    @app.exception_handler(ServiceError)
    async def _service_error(_: Request, exc: ServiceError) -> JSONResponse:
        return _error(exc.status_code, exc.code, exc.message)

    @app.exception_handler(InvalidTransitionError)
    async def _transition_error(_: Request, exc: InvalidTransitionError) -> JSONResponse:
        return _error(409, "invalid_transition", str(exc))

    @app.exception_handler(IdempotencyInProgressError)
    async def _idempotency_in_progress(_: Request, exc: IdempotencyInProgressError) -> JSONResponse:
        response = _error(409, "idempotency_key_in_progress", str(exc))
        response.headers["Retry-After"] = str(exc.retry_after)
        return response

    @app.exception_handler(IdempotencyConflictError)
    async def _idempotency_error(_: Request, exc: IdempotencyConflictError) -> JSONResponse:
        return _error(422, "idempotency_key_reused", str(exc))

    @app.exception_handler(SignatureError)
    async def _signature_error(_: Request, exc: SignatureError) -> JSONResponse:
        return _error(400, "invalid_signature", str(exc))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            {
                "error": {
                    "code": "validation_error",
                    "message": "Invalid request",
                    "details": jsonable_encoder(exc.errors()),
                }
            },
            status_code=422,
        )

    app.include_router(health.router)
    app.include_router(payments.router)
    app.include_router(webhooks.router)
    app.include_router(settlements.router)
    return app


app = create_app()
