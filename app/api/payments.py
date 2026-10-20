from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, Header, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.deps import (
    get_current_merchant,
    get_db,
    get_idempotency_store,
    get_outbox,
    get_payment_service,
)
from app.api.idempotent import run_idempotent
from app.core.idempotency import IdempotencyStore
from app.models import Merchant
from app.schemas import (
    CaptureRequest,
    ErrorResponse,
    PaymentCreate,
    PaymentOut,
    RefundOut,
    RefundRequest,
    RefundResponse,
)
from app.services.payments import PaymentService
from app.services.webhooks import WebhookOutbox

router = APIRouter(prefix="/v1/payments", tags=["payments"])

_ERRORS = {
    401: {"model": ErrorResponse},
    404: {"model": ErrorResponse},
    409: {"model": ErrorResponse},
}


def _schedule_webhooks(
    request: Request, background: BackgroundTasks, outbox: WebhookOutbox
) -> None:
    if outbox.pending:
        background.add_task(
            request.app.state.webhook_sender.deliver,
            request.app.state.session_factory,
            list(outbox.pending),
        )


@router.post(
    "",
    status_code=201,
    response_model=PaymentOut,
    responses={**_ERRORS, 422: {"model": ErrorResponse}},
    summary="Create a payment (routed to the cheapest capable provider)",
)
def create_payment(
    payload: PaymentCreate,
    request: Request,
    background: BackgroundTasks,
    merchant: Merchant = Depends(get_current_merchant),
    db: Session = Depends(get_db),
    service: PaymentService = Depends(get_payment_service),
    outbox: WebhookOutbox = Depends(get_outbox),
    store: IdempotencyStore = Depends(get_idempotency_store),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
) -> JSONResponse:
    def produce() -> tuple[int, dict]:
        payment = service.create_payment(merchant, payload)
        return 201, PaymentOut.from_model(payment).model_dump(mode="json")

    response = run_idempotent(
        db=db,
        store=store,
        merchant=merchant,
        key=idempotency_key,
        request_payload={"op": "create", **payload.model_dump(mode="json")},
        produce=produce,
    )
    _schedule_webhooks(request, background, service.outbox)
    return response


@router.get("", response_model=list[PaymentOut], responses=_ERRORS, summary="List payments")
def list_payments(
    merchant: Merchant = Depends(get_current_merchant),
    service: PaymentService = Depends(get_payment_service),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[PaymentOut]:
    return [
        PaymentOut.from_model(p, include_history=False)
        for p in service.list_payments(merchant, status, limit, offset)
    ]


@router.get(
    "/{payment_id}", response_model=PaymentOut, responses=_ERRORS, summary="Fetch a payment"
)
def get_payment(
    payment_id: str,
    merchant: Merchant = Depends(get_current_merchant),
    service: PaymentService = Depends(get_payment_service),
) -> PaymentOut:
    return PaymentOut.from_model(service.get_payment(merchant, payment_id))


@router.post(
    "/{payment_id}/capture",
    response_model=PaymentOut,
    responses=_ERRORS,
    summary="Capture an authorised payment",
)
def capture_payment(
    payment_id: str,
    request: Request,
    background: BackgroundTasks,
    payload: CaptureRequest | None = None,
    merchant: Merchant = Depends(get_current_merchant),
    db: Session = Depends(get_db),
    service: PaymentService = Depends(get_payment_service),
    store: IdempotencyStore = Depends(get_idempotency_store),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
) -> JSONResponse:
    body = payload or CaptureRequest()

    def produce() -> tuple[int, dict]:
        payment = service.capture(merchant, payment_id, body.amount_minor)
        return 200, PaymentOut.from_model(payment).model_dump(mode="json")

    response = run_idempotent(
        db=db,
        store=store,
        merchant=merchant,
        key=idempotency_key,
        request_payload={"op": "capture", "payment_id": payment_id, **body.model_dump()},
        produce=produce,
    )
    _schedule_webhooks(request, background, service.outbox)
    return response


@router.post(
    "/{payment_id}/refund",
    response_model=RefundResponse,
    responses={**_ERRORS, 422: {"model": ErrorResponse}},
    summary="Refund a captured payment (full or partial)",
)
def refund_payment(
    payment_id: str,
    request: Request,
    background: BackgroundTasks,
    payload: RefundRequest | None = None,
    merchant: Merchant = Depends(get_current_merchant),
    db: Session = Depends(get_db),
    service: PaymentService = Depends(get_payment_service),
    store: IdempotencyStore = Depends(get_idempotency_store),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key", max_length=255),
) -> JSONResponse:
    body = payload or RefundRequest()

    def produce() -> tuple[int, dict]:
        payment, refund = service.refund(merchant, payment_id, body.amount_minor, body.reason)
        out = RefundResponse(
            refund=RefundOut.from_model(refund, payment.currency),
            payment=PaymentOut.from_model(payment),
        )
        return 200, out.model_dump(mode="json")

    response = run_idempotent(
        db=db,
        store=store,
        merchant=merchant,
        key=idempotency_key,
        request_payload={"op": "refund", "payment_id": payment_id, **body.model_dump()},
        produce=produce,
    )
    _schedule_webhooks(request, background, service.outbox)
    return response


@router.post(
    "/{payment_id}/cancel",
    response_model=PaymentOut,
    responses=_ERRORS,
    summary="Cancel a created/authorised payment",
)
def cancel_payment(
    payment_id: str,
    request: Request,
    background: BackgroundTasks,
    merchant: Merchant = Depends(get_current_merchant),
    db: Session = Depends(get_db),
    service: PaymentService = Depends(get_payment_service),
) -> PaymentOut:
    payment = service.cancel(merchant, payment_id)
    db.commit()
    _schedule_webhooks(request, background, service.outbox)
    return PaymentOut.from_model(payment)
