from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy.orm import Session

from app.api.deps import get_current_merchant, get_db, get_settlement_service
from app.schemas import (
    ErrorResponse,
    ReconcileRequest,
    ReconciliationMismatch,
    ReconciliationResult,
    SettlementOut,
    SettlementRunRequest,
)
from app.services.settlements import SettlementService

router = APIRouter(
    prefix="/v1/settlements",
    tags=["settlements"],
    dependencies=[Depends(get_current_merchant)],
    responses={401: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
)


@router.get("", response_model=list[SettlementOut], summary="List settlement batches")
def list_settlements(
    response: Response,
    service: SettlementService = Depends(get_settlement_service),
    provider: str | None = Query(default=None),
    settlement_date: date | None = Query(default=None),
    status: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
) -> list[SettlementOut]:
    items, total = service.list(provider, settlement_date, status, limit, offset)
    response.headers["X-Total-Count"] = str(total)
    return [SettlementOut.from_model(s) for s in items]


@router.post(
    "/run",
    response_model=list[SettlementOut],
    status_code=201,
    summary="Generate settlement batches for a day (defaults to yesterday, UTC)",
)
def run_settlement(
    payload: SettlementRunRequest | None = None,
    db: Session = Depends(get_db),
    service: SettlementService = Depends(get_settlement_service),
) -> list[SettlementOut]:
    body = payload or SettlementRunRequest()
    settlements = service.run(body.settlement_date, body.providers)
    db.commit()
    return [SettlementOut.from_model(s, include_lines=True) for s in settlements]


@router.get(
    "/{settlement_id}", response_model=SettlementOut, summary="Fetch a settlement with lines"
)
def get_settlement(
    settlement_id: str, service: SettlementService = Depends(get_settlement_service)
) -> SettlementOut:
    return SettlementOut.from_model(service.get(settlement_id), include_lines=True)


@router.get("/{settlement_id}/export.csv", summary="Download the settlement as CSV")
def export_settlement(
    settlement_id: str, service: SettlementService = Depends(get_settlement_service)
) -> Response:
    settlement = service.get(settlement_id)
    content = service.export_csv(settlement)
    filename = f"settlement-{settlement.provider}-{settlement.settlement_date.isoformat()}.csv"
    return Response(
        content=content,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post(
    "/{settlement_id}/reconcile",
    response_model=ReconciliationResult,
    responses={409: {"model": ErrorResponse}},
    summary="Diff the settlement against a provider report",
)
def reconcile_settlement(
    settlement_id: str,
    payload: ReconcileRequest | None = None,
    db: Session = Depends(get_db),
    service: SettlementService = Depends(get_settlement_service),
) -> ReconciliationResult:
    body = payload or ReconcileRequest()
    settlement = service.get(settlement_id)
    result = service.reconcile(settlement, body.rows)
    db.commit()
    return ReconciliationResult(
        settlement_id=result.settlement_id,
        matched=result.matched,
        amount_mismatches=[ReconciliationMismatch(**vars(m)) for m in result.amount_mismatches],
        missing_in_provider=result.missing_in_provider,
        unexpected_in_provider=result.unexpected_in_provider,
        ledger_net_minor=result.ledger_net_minor,
        provider_net_minor=result.provider_net_minor,
        reconciled=result.reconciled,
    )


@router.post(
    "/{settlement_id}/mark-paid",
    response_model=SettlementOut,
    responses={409: {"model": ErrorResponse}},
    summary="Mark a reconciled settlement as paid out",
)
def mark_paid(
    settlement_id: str,
    db: Session = Depends(get_db),
    service: SettlementService = Depends(get_settlement_service),
) -> SettlementOut:
    settlement = service.mark_paid(service.get(settlement_id))
    db.commit()
    return SettlementOut.from_model(settlement)
