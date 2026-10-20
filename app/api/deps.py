"""FastAPI dependencies: DB session, merchant authentication, service construction."""

from __future__ import annotations

from collections.abc import Generator

from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.core.idempotency import IdempotencyStore
from app.core.security import api_key_prefix, verify_api_key
from app.models import Merchant
from app.services.errors import UnauthorizedError
from app.services.payments import PaymentService
from app.services.settlements import SettlementService
from app.services.webhooks import WebhookOutbox


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request) -> Generator[Session, None, None]:
    session: Session = request.app.state.session_factory()
    try:
        yield session
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_current_merchant(
    db: Session = Depends(get_db),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> Merchant:
    if not x_api_key:
        raise UnauthorizedError("Missing X-API-Key header")
    prefix = api_key_prefix(x_api_key)
    if prefix is None:
        raise UnauthorizedError("Malformed API key")
    merchant = db.scalar(select(Merchant).where(Merchant.api_key_prefix == prefix))
    if (
        merchant is None
        or not merchant.is_active
        or not verify_api_key(x_api_key, merchant.api_key_hash)
    ):
        raise UnauthorizedError("Invalid API key")
    return merchant


def get_outbox(db: Session = Depends(get_db)) -> WebhookOutbox:
    return WebhookOutbox(db)


def get_payment_service(
    request: Request,
    db: Session = Depends(get_db),
    outbox: WebhookOutbox = Depends(get_outbox),
) -> PaymentService:
    return PaymentService(
        db=db,
        router=request.app.state.router,
        providers=request.app.state.providers,
        outbox=outbox,
    )


def get_settlement_service(request: Request, db: Session = Depends(get_db)) -> SettlementService:
    return SettlementService(db=db, providers=request.app.state.providers)


def get_idempotency_store(
    db: Session = Depends(get_db), settings: Settings = Depends(get_settings_dep)
) -> IdempotencyStore:
    return IdempotencyStore(db, ttl_hours=settings.idempotency_ttl_hours)
