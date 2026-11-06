"""Operational CLI: create merchants, run settlements, purge keys, dispatch webhooks.

gateway create-merchant "Dhaka Mart" --webhook-url https://shop.example/webhooks
gateway run-settlement --date 2026-10-19
gateway purge-idempotency
gateway dispatch-webhooks          # one pass, for cron
gateway dispatch-webhooks --loop   # long-running worker
"""

from __future__ import annotations

import argparse
import logging
import secrets
import signal
import sys
import threading
from datetime import date

from app.config import get_settings
from app.core.idempotency import IdempotencyStore
from app.core.security import generate_api_key
from app.database import init_db, make_engine, make_session_factory
from app.logging_config import configure_logging
from app.models import Merchant
from app.providers import build_providers
from app.services.settlements import SettlementService
from app.services.webhooks import build_webhook_sender

logger = logging.getLogger(__name__)


def _session_factory():
    settings = get_settings()
    engine = make_engine(settings.database_url)
    init_db(engine)
    return settings, make_session_factory(engine)


def cmd_create_merchant(args: argparse.Namespace) -> int:
    settings, factory = _session_factory()
    plaintext, prefix, key_hash = generate_api_key("live" if settings.is_production else "test")
    with factory() as db:
        merchant = Merchant(
            name=args.name,
            api_key_prefix=prefix,
            api_key_hash=key_hash,
            webhook_url=args.webhook_url,
            webhook_secret=args.webhook_secret or f"whsec_{secrets.token_urlsafe(24)}",
        )
        db.add(merchant)
        db.commit()
        print(f"merchant_id:     {merchant.id}")
        print(f"api_key:         {plaintext}   (shown once, store it safely)")
        print(f"webhook_secret:  {merchant.webhook_secret}")
    return 0


def cmd_run_settlement(args: argparse.Namespace) -> int:
    settings, factory = _session_factory()
    target = date.fromisoformat(args.date) if args.date else None
    with factory() as db:
        service = SettlementService(db, build_providers(settings))
        settlements = service.run(target, args.provider or None)
        db.commit()
        if not settlements:
            print("no settleable payments found")
        for s in settlements:
            print(
                f"{s.id} {s.provider} {s.currency} {s.settlement_date} "
                f"payments={s.payment_count} gross={s.gross_minor} "
                f"fee={s.fee_minor} net={s.net_minor}"
            )
    return 0


def cmd_purge_idempotency(args: argparse.Namespace) -> int:
    settings, factory = _session_factory()
    with factory() as db:
        removed = IdempotencyStore(db, ttl_hours=settings.idempotency_ttl_hours).purge_expired()
        db.commit()
        print(f"removed {removed} expired idempotency records")
    return 0


def cmd_dispatch_webhooks(args: argparse.Namespace) -> int:
    settings, factory = _session_factory()
    configure_logging(settings.log_level)
    sender = build_webhook_sender(settings)
    batch_size = args.batch_size or settings.webhook_dispatch_batch_size
    if not args.loop:
        print(sender.dispatch_due(factory, limit=batch_size))
        return 0

    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    logger.info("webhook worker started")
    sender.run_forever(
        factory,
        stop,
        poll_seconds=settings.webhook_dispatch_poll_seconds,
        batch_size=batch_size,
    )
    logger.info("webhook worker stopped")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gateway", description="Payment gateway operations")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-merchant", help="Create a merchant and print its API key")
    create.add_argument("name")
    create.add_argument("--webhook-url", default=None)
    create.add_argument("--webhook-secret", default=None)
    create.set_defaults(func=cmd_create_merchant)

    settle = sub.add_parser("run-settlement", help="Generate settlement batches")
    settle.add_argument("--date", default=None, help="YYYY-MM-DD (default: yesterday UTC)")
    settle.add_argument("--provider", action="append", help="Restrict to provider(s)")
    settle.set_defaults(func=cmd_run_settlement)

    purge = sub.add_parser("purge-idempotency", help="Delete expired idempotency keys")
    purge.set_defaults(func=cmd_purge_idempotency)

    dispatch = sub.add_parser("dispatch-webhooks", help="Send due merchant webhook retries")
    dispatch.add_argument("--loop", action="store_true", help="Keep polling until stopped")
    dispatch.add_argument("--batch-size", type=int, default=None, help="Rows per pass")
    dispatch.set_defaults(func=cmd_dispatch_webhooks)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
