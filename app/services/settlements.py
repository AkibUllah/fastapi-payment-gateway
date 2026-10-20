"""Daily settlement batches, CSV export and reconciliation against provider reports."""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.core.state_machine import SETTLEABLE_STATES
from app.models import Payment, Settlement, SettlementLine
from app.providers.base import PaymentProvider
from app.schemas import ProviderReportRowIn
from app.services.errors import ConflictError, NotFoundError


@dataclass
class Mismatch:
    provider_ref: str
    ledger_amount_minor: int | None
    provider_amount_minor: int | None
    ledger_fee_minor: int | None
    provider_fee_minor: int | None


@dataclass
class Reconciliation:
    settlement_id: str
    matched: int = 0
    amount_mismatches: list[Mismatch] = field(default_factory=list)
    missing_in_provider: list[str] = field(default_factory=list)
    unexpected_in_provider: list[str] = field(default_factory=list)
    ledger_net_minor: int = 0
    provider_net_minor: int = 0

    @property
    def reconciled(self) -> bool:
        return not (
            self.amount_mismatches or self.missing_in_provider or self.unexpected_in_provider
        )


class SettlementService:
    def __init__(self, db: Session, providers: Mapping[str, PaymentProvider]) -> None:
        self.db = db
        self.providers = providers

    def run(
        self, settlement_date: date | None, providers: Iterable[str] | None = None
    ) -> list[Settlement]:
        """Create one settlement per (provider, currency) for payments captured that day."""
        target_date = settlement_date or (datetime.now(timezone.utc).date() - timedelta(days=1))
        start = datetime(target_date.year, target_date.month, target_date.day, tzinfo=timezone.utc)
        end = start + timedelta(days=1)
        wanted = set(providers) if providers else set(self.providers)

        stmt = select(Payment).where(
            Payment.status.in_([s.value for s in SETTLEABLE_STATES]),
            Payment.settlement_id.is_(None),
            Payment.captured_at >= start,
            Payment.captured_at < end,
        )
        payments = self.db.scalars(stmt).all()

        groups: dict[tuple[str, str], list[Payment]] = {}
        for payment in payments:
            if payment.provider not in wanted:
                continue
            groups.setdefault((payment.provider, payment.currency), []).append(payment)

        created: list[Settlement] = []
        for (provider, currency), items in sorted(groups.items()):
            settlement = Settlement(
                provider=provider,
                currency=currency,
                settlement_date=target_date,
                status="generated",
            )
            self.db.add(settlement)
            self.db.flush()
            for payment in items:
                net = payment.amount_minor - payment.fee_minor - payment.refunded_minor
                self.db.add(
                    SettlementLine(
                        settlement_id=settlement.id,
                        payment_id=payment.id,
                        provider_ref=payment.provider_ref,
                        gross_minor=payment.amount_minor,
                        fee_minor=payment.fee_minor,
                        refunded_minor=payment.refunded_minor,
                        net_minor=net,
                    )
                )
                payment.settlement_id = settlement.id
                settlement.gross_minor += payment.amount_minor
                settlement.fee_minor += payment.fee_minor
                settlement.refunded_minor += payment.refunded_minor
                settlement.net_minor += net
                settlement.payment_count += 1
            self.db.flush()
            created.append(self.get(settlement.id))
        return created

    def get(self, settlement_id: str) -> Settlement:
        settlement = self.db.scalar(
            select(Settlement)
            .options(selectinload(Settlement.lines))
            .where(Settlement.id == settlement_id)
        )
        if settlement is None:
            raise NotFoundError(f"Settlement {settlement_id} not found")
        return settlement

    def list(
        self,
        provider: str | None = None,
        settlement_date: date | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Settlement], int]:
        stmt = select(Settlement)
        count_stmt = select(func.count(Settlement.id))
        if provider:
            stmt = stmt.where(Settlement.provider == provider)
            count_stmt = count_stmt.where(Settlement.provider == provider)
        if settlement_date:
            stmt = stmt.where(Settlement.settlement_date == settlement_date)
            count_stmt = count_stmt.where(Settlement.settlement_date == settlement_date)
        if status:
            stmt = stmt.where(Settlement.status == status)
            count_stmt = count_stmt.where(Settlement.status == status)
        stmt = (
            stmt.order_by(Settlement.settlement_date.desc(), Settlement.provider)
            .limit(limit)
            .offset(offset)
        )
        return list(self.db.scalars(stmt).all()), int(self.db.scalar(count_stmt) or 0)

    @staticmethod
    def export_csv(settlement: Settlement) -> str:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            [
                "settlement_id",
                "settlement_date",
                "provider",
                "currency",
                "payment_id",
                "provider_ref",
                "gross_minor",
                "fee_minor",
                "refunded_minor",
                "net_minor",
            ]
        )
        for line in settlement.lines:
            writer.writerow(
                [
                    settlement.id,
                    settlement.settlement_date.isoformat(),
                    settlement.provider,
                    settlement.currency,
                    line.payment_id,
                    line.provider_ref or "",
                    line.gross_minor,
                    line.fee_minor,
                    line.refunded_minor,
                    line.net_minor,
                ]
            )
        writer.writerow(
            [
                settlement.id,
                settlement.settlement_date.isoformat(),
                settlement.provider,
                settlement.currency,
                "TOTAL",
                "",
                settlement.gross_minor,
                settlement.fee_minor,
                settlement.refunded_minor,
                settlement.net_minor,
            ]
        )
        return buffer.getvalue()

    def reconcile(
        self, settlement: Settlement, rows: list[ProviderReportRowIn] | None
    ) -> Reconciliation:
        if settlement.status == "paid":
            raise ConflictError("Settlement already paid; reconciliation is closed")
        if rows is None:
            provider = self.providers.get(settlement.provider)
            if provider is None:
                raise NotFoundError(f"Provider {settlement.provider} is not configured")
            rows = [
                ProviderReportRowIn(
                    provider_ref=r.provider_ref, amount_minor=r.amount_minor, fee_minor=r.fee_minor
                )
                for r in provider.fetch_report(settlement.settlement_date)
                if r.currency == settlement.currency
            ]

        report = {row.provider_ref: row for row in rows}
        result = Reconciliation(settlement_id=settlement.id)
        seen: set[str] = set()
        for line in settlement.lines:
            ref = line.provider_ref or ""
            result.ledger_net_minor += line.gross_minor - line.fee_minor
            row = report.get(ref)
            if row is None:
                result.missing_in_provider.append(ref)
                continue
            seen.add(ref)
            if row.amount_minor != line.gross_minor or row.fee_minor != line.fee_minor:
                result.amount_mismatches.append(
                    Mismatch(
                        provider_ref=ref,
                        ledger_amount_minor=line.gross_minor,
                        provider_amount_minor=row.amount_minor,
                        ledger_fee_minor=line.fee_minor,
                        provider_fee_minor=row.fee_minor,
                    )
                )
            else:
                result.matched += 1
        for ref, row in report.items():
            result.provider_net_minor += row.amount_minor - row.fee_minor
            if ref not in seen and ref not in result.missing_in_provider:
                result.unexpected_in_provider.append(ref)

        settlement.status = "reconciled" if result.reconciled else "discrepancy"
        settlement.reconciled_at = datetime.now(timezone.utc)
        self.db.flush()
        return result

    def mark_paid(self, settlement: Settlement) -> Settlement:
        if settlement.status != "reconciled":
            raise ConflictError("Only reconciled settlements can be marked paid")
        settlement.status = "paid"
        self.db.flush()
        return settlement
