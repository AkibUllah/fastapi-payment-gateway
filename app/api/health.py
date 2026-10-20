from __future__ import annotations

from fastapi import APIRouter, Request
from sqlalchemy import text

from app import __version__
from app.schemas import HealthOut

router = APIRouter(tags=["ops"])


@router.get("/health", response_model=HealthOut, summary="Liveness/readiness probe")
def health(request: Request) -> HealthOut:
    database = "ok"
    try:
        with request.app.state.engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:  # pragma: no cover - exercised only when the DB is down
        database = "error"
    return HealthOut(
        status="ok" if database == "ok" else "degraded",
        version=__version__,
        environment=request.app.state.settings.app_env,
        database=database,
        providers=sorted(request.app.state.providers),
    )
