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

    `produce` must flush but not commit; this helper commits so the idempotency record and
    the business change land in the same transaction.
    """
    if key is None:
        status_code, body = produce()
        db.commit()
        return JSONResponse(body, status_code=status_code)

    request_hash = fingerprint(request_payload)
    record = store.lookup(merchant.id, key, request_hash)
    if record is not None:
        return JSONResponse(
            json.loads(record.response_body),
            status_code=record.status_code,
            headers={REPLAY_HEADER: "true"},
        )
    status_code, body = produce()
    store.save(merchant.id, key, request_hash, status_code, body)
    db.commit()
    return JSONResponse(body, status_code=status_code, headers={REPLAY_HEADER: "false"})
