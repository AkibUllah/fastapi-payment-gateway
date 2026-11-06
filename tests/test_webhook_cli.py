"""`dispatch-webhooks`: one pass for cron, or a worker that stops on a signal."""

from __future__ import annotations

import os
import signal
import threading

import pytest

from app import cli
from app.config import Settings
from tests.conftest import create_payment


@pytest.fixture
def run_cli(app, monkeypatch):
    monkeypatch.setattr(
        cli, "_session_factory", lambda: (app.state.settings, app.state.session_factory)
    )
    monkeypatch.setattr(cli, "build_webhook_sender", lambda _settings: app.state.webhook_sender)
    monkeypatch.setattr(cli, "configure_logging", lambda _level: None)
    return cli.main


def test_one_pass_sends_due_rows_and_reports(run_cli, client, auth, sink, clock, capsys):
    sink.fail_next = 3
    create_payment(client, auth)
    clock.advance(60)

    assert run_cli(["dispatch-webhooks"]) == 0

    assert "claimed=1 delivered=1 rescheduled=0 failed=0 lost=0" in capsys.readouterr().out


def test_batch_size_flag_overrides_the_setting(run_cli, client, auth, sink, clock, capsys):
    sink.fail_next = 1000
    create_payment(client, auth)
    create_payment(client, auth)
    sink.fail_next = 0
    clock.advance(60)

    assert run_cli(["dispatch-webhooks", "--batch-size", "1"]) == 0

    assert "claimed=1 " in capsys.readouterr().out


def test_batch_size_defaults_to_the_setting(app, run_cli, client, auth, sink, clock, capsys):
    sink.fail_next = 1000
    create_payment(client, auth)
    create_payment(client, auth)
    sink.fail_next = 0
    clock.advance(60)
    app.state.settings.webhook_dispatch_batch_size = 1

    assert run_cli(["dispatch-webhooks"]) == 0

    assert "claimed=1 " in capsys.readouterr().out


def test_loop_stops_cleanly_on_sigterm(app, run_cli, monkeypatch):
    previous = signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)
    seen: dict[str, object] = {}
    sender = app.state.webhook_sender

    def fake_run_forever(_factory, stop: threading.Event, poll_seconds, batch_size) -> None:
        seen.update(poll_seconds=poll_seconds, batch_size=batch_size)
        os.kill(os.getpid(), signal.SIGTERM)
        seen["stopped"] = stop.wait(5)

    monkeypatch.setattr(sender, "run_forever", fake_run_forever)
    try:
        assert run_cli(["dispatch-webhooks", "--loop"]) == 0
    finally:
        signal.signal(signal.SIGTERM, previous[0])
        signal.signal(signal.SIGINT, previous[1])

    defaults = Settings(_env_file=None)
    assert seen == {
        "poll_seconds": defaults.webhook_dispatch_poll_seconds,
        "batch_size": defaults.webhook_dispatch_batch_size,
        "stopped": True,
    }
