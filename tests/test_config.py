from __future__ import annotations

import pytest

from app.config import Settings


@pytest.mark.parametrize(
    "given",
    [
        "postgres://gateway:secret@db.render.internal:5432/gateway",
        "postgresql://gateway:secret@db.render.internal:5432/gateway",
    ],
)
def test_managed_postgres_urls_are_rewritten_to_psycopg3(given: str) -> None:
    settings = Settings(_env_file=None, database_url=given)
    assert settings.database_url == (
        "postgresql+psycopg://gateway:secret@db.render.internal:5432/gateway"
    )


@pytest.mark.parametrize(
    "given",
    ["sqlite://", "sqlite:///./gateway.db", "postgresql+psycopg://gateway:gateway@db:5432/gateway"],
)
def test_explicit_driver_urls_are_left_alone(given: str) -> None:
    assert Settings(_env_file=None, database_url=given).database_url == given
