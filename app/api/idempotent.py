"""Helper that wraps a mutating handler with Idempotency-Key semantics."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.core.idempotency import IdempotencyStore, fingerprint
from app.models import Merchant

REPLAY_HEADER = "Idempotent-Replayed"


def run_idempotent(
    *,
    db: Session,
    store: IdempotencyStore,
    merchant: Merchant,
    key: str | None,
    request_payload: Any,
    produce: Callable[[], tuple[int, dict[str, Any]]],
) -> JSONResponse:
    """Execute `produce` once per (merchant, key, payload); replay the stored response otherwise.

    The key is reserved (and committed) before `produce` runs, so a concurrent duplicate
    is answered with 409 instead of calling the provider twice. `produce` must flush but
    not commit; this helper commits so the stored response and the business change land
    in the same transaction.
    """
    if key is None:
        status_code, body = produce()
        db.commit()
        return JSONResponse(body, status_code=status_code)

    reservation = store.begin(merchant.id, key, fingerprint(request_payload))
    if reservation.replay is not None:
        record = reservation.replay
        return JSONResponse(
            json.loads(record.response_body or "{}"),
            status_code=record.status_code or 200,
            headers={REPLAY_HEADER: "true"},
        )
    assert reservation.record_id is not None
    try:
        status_code, body = produce()
        store.complete(reservation.record_id, status_code, body)
        db.commit()
    except BaseException:
        # Undo the business change and free the key so the client can retry it.
        db.rollback()
        store.release(reservation.record_id)
        raise
    return JSONResponse(body, status_code=status_code, headers={REPLAY_HEADER: "false"})
