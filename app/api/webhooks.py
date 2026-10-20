"""Inbound provider notifications and outgoing-delivery inspection."""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_current_merchant, get_db, get_payment_service
from app.core.security import SignatureError
from app.models import Merchant, WebhookDelivery
from app.schemas import ErrorResponse, WebhookAck, WebhookDeliveryOut
from app.services.errors import NotFoundError, ValidationFailed
from app.services.payments import PaymentService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])


@router.post(
    "/{provider}",
    response_model=WebhookAck,
    status_code=202,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    summary="Receive a signed provider notification",
)
async def receive_provider_webhook(
    provider: str,
    request: Request,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    service: PaymentService = Depends(get_payment_service),
) -> WebhookAck:
    adapter = request.app.state.providers.get(provider)
    if adapter is None:
        raise NotFoundError(f"Unknown provider '{provider}'")
    body = await request.body()
    adapter.verify_webhook(dict(request.headers), body)  # raises SignatureError -> 400
    try:
        event = adapter.parse_webhook(body)
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc

    result, payment = service.apply_provider_event(provider, event)
    db.commit()
    logger.info(
        "provider webhook provider=%s event=%s ref=%s result=%s",
        provider,
        event.event_type,
        event.provider_ref,
        result,
    )
    if service.outbox.pending:
        background.add_task(
            request.app.state.webhook_sender.deliver,
            request.app.state.session_factory,
            list(service.outbox.pending),
        )
    return WebhookAck(
        provider=provider,
        event_type=event.event_type,
        provider_ref=event.provider_ref,
        result=result,
        payment_id=payment.id if payment else None,
    )


@router.get(
    "/deliveries",
    response_model=list[WebhookDeliveryOut],
    summary="List outgoing webhook deliveries for the authenticated merchant",
)
def list_deliveries(
    merchant: Merchant = Depends(get_current_merchant),
    db: Session = Depends(get_db),
    payment_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[WebhookDeliveryOut]:
    stmt = select(WebhookDelivery).where(WebhookDelivery.merchant_id == merchant.id)
    if payment_id:
        stmt = stmt.where(WebhookDelivery.payment_id == payment_id)
    if status:
        stmt = stmt.where(WebhookDelivery.status == status)
    stmt = stmt.order_by(WebhookDelivery.created_at.desc()).limit(limit)
    return [WebhookDeliveryOut.from_model(d) for d in db.scalars(stmt).all()]


__all__ = ["router", "SignatureError"]
