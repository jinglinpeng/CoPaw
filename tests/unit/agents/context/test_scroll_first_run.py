# -*- coding: utf-8 -*-
# pylint: disable=too-few-public-methods,protected-access
"""The scroll first-run notice.

Scroll became the DEFAULT context strategy, so agents that never set
``strategy`` are switched to it silently and get a durable ``history.db`` in
their workspace. ``build_scroll_components`` must log a one-time notice the
first time it wires scroll in a workspace (the db file's absence is the
signal), and must stay silent on every later run.
"""

import logging
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import qwenpaw.agents.context as context_mod
import qwenpaw.agents.context.scroll.manager as scroll_manager_mod
import qwenpaw.agents.context.scroll.sync as scroll_sync_mod
from qwenpaw.agents.context import build_scroll_components
from qwenpaw.config.config import LightContextConfig


class _DummyModel:
    """A stand-in model; scroll only stores it, never calls it at wiring."""


def _agent_config(strategy: str = "scroll") -> SimpleNamespace:
    lcc = LightContextConfig(strategy=strategy)
    return SimpleNamespace(running=SimpleNamespace(light_context_config=lcc))


def _build(workspace: Path):
    return build_scroll_components(
        agent_config=_agent_config(),
        workspace_dir=str(workspace),
        model=_DummyModel(),
        session_id="s1",
        agent_id="ag1",
    )


def _notice_records(caplog) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "DEFAULT context strategy" in r.msg
    ]


def test_removed_eviction_headline_settings_are_ignored():
    config = LightContextConfig(
        strategy="scroll",
        scroll_config={
            "summarize_unheadlined_evictions": True,
            "summarize_eviction_timeout_seconds": 45,
        },
    )
    assert not hasattr(
        config.scroll_config,
        "summarize_unheadlined_evictions",
    )
    assert not hasattr(
        config.scroll_config,
        "summarize_eviction_timeout_seconds",
    )


@pytest.mark.usefixtures("capture_qwenpaw_logs")
def test_first_run_logs_notice_once(tmp_path: Path, caplog):
    db = tmp_path / "history.db"
    assert not db.exists()

    with caplog.at_level(logging.WARNING, logger="qwenpaw.agents.context"):
        components = _build(tmp_path)
    # Scroll actually wired and created the durable store.
    assert components is not None
    assert db.exists()
    # Exactly one notice, and it points at the rollback path.
    records = _notice_records(caplog)
    assert len(records) == 1
    msg = records[0].getMessage()
    assert str(db) in msg
    assert "native" in msg


def test_notice_does_not_repeat_when_db_exists(tmp_path: Path, caplog):
    # Pre-create the store so this looks like a second startup.
    _build(tmp_path)
    assert (tmp_path / "history.db").exists()

    caplog.clear()  # drop the first run's notice; only watch the second
    with caplog.at_level(logging.WARNING, logger="qwenpaw.agents.context"):
        _build(tmp_path)
    assert _notice_records(caplog) == []


def _size_records(caplog) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "history_retention_days" in r.msg
    ]


@pytest.mark.usefixtures("capture_qwenpaw_logs")
def test_large_existing_db_warns_about_retention(tmp_path: Path, caplog):
    # First build creates a small store (no size warning, just first-run).
    _build(tmp_path)
    db = tmp_path / "history.db"
    assert db.exists()

    # Drop the warn threshold below the tiny db so the next build trips it,
    # and clear the process-level dedupe set so the test is order-independent.
    monkey = db.stat().st_size - 1
    context_mod._DB_SIZE_WARNED.discard(str(db))
    orig = context_mod._DB_SIZE_WARN_BYTES
    context_mod._DB_SIZE_WARN_BYTES = monkey
    try:
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="qwenpaw.agents.context"):
            _build(tmp_path)
            records = _size_records(caplog)
            assert len(records) == 1
            assert "history_retention_days" in records[0].getMessage()
            # Deduped: a second build in the same process does not re-warn.
            caplog.clear()
            _build(tmp_path)
            assert _size_records(caplog) == []
    finally:
        context_mod._DB_SIZE_WARN_BYTES = orig
        context_mod._DB_SIZE_WARNED.discard(str(db))


def test_no_notice_when_strategy_is_native(tmp_path: Path, caplog):
    with caplog.at_level(logging.WARNING, logger="qwenpaw.agents.context"):
        components = build_scroll_components(
            agent_config=_agent_config("native"),
            workspace_dir=str(tmp_path),
            model=_DummyModel(),
            session_id="s1",
            agent_id="ag1",
        )
    # Native is unaffected: nothing wired, no db, no notice.
    assert components is None
    assert not (tmp_path / "history.db").exists()
    assert _notice_records(caplog) == []


def test_wiring_waits_for_startup_history(tmp_path: Path, monkeypatch):
    db_path = tmp_path / "history.db"
    waiting = threading.Event()
    built = threading.Event()
    components = []
    original_wait = scroll_sync_mod.wait_for_startup_history

    def observed_wait(path):
        waiting.set()
        original_wait(path)

    def build():
        components.append(_build(tmp_path))
        built.set()

    scroll_sync_mod.begin_startup_history_sync()
    scroll_sync_mod._publish_startup_history_paths([db_path])
    monkeypatch.setattr(
        scroll_sync_mod,
        "wait_for_startup_history",
        observed_wait,
    )
    worker = threading.Thread(target=build)
    worker.start()
    try:
        assert waiting.wait(timeout=2)
        assert not built.wait(timeout=0.05)
    finally:
        scroll_sync_mod.finish_startup_history_sync()
        worker.join(timeout=2)

    assert built.is_set()
    assert components[0] is not None
    components[0].context_manager.close()


def test_wiring_failure_closes_history_store(tmp_path: Path, monkeypatch):
    histories = []

    def fail_manager(**kwargs):
        histories.append(kwargs["history"])
        raise RuntimeError("wiring failed")

    monkeypatch.setattr(
        scroll_manager_mod,
        "ScrollContextManager",
        fail_manager,
    )

    assert _build(tmp_path) is None
    assert len(histories) == 1
    assert histories[0].closed is True
