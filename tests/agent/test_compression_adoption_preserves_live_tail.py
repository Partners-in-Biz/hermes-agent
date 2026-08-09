"""Regression: preflight durable-snapshot adoption must not drop the live
turn's un-persisted user input.

``compress_context`` re-reads the durable parent after acquiring the
per-session compression lock. When another writer commits in that window,
``len(durable_parent) > len(messages)`` and preflight adopts the snapshot.
The in-memory transcript also carries this turn's un-persisted user tail.
The parent snapshot does not contain it yet, so adopting it verbatim silently
drops the live instruction from both the summary and durable history.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_state import SessionDB


def _build_agent_with_db(db: SessionDB, session_id: str):
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            session_db=db,
            session_id=session_id,
            skip_context_files=True,
            skip_memory=True,
        )

    compressor = MagicMock()
    compressor.compress.side_effect = lambda *_a, **_kw: [
        {"role": "user", "content": "[CONTEXT COMPACTION] summary"},
        {"role": "user", "content": "tail"},
    ]
    compressor.compression_count = 1
    compressor.last_prompt_tokens = 0
    compressor.last_completion_tokens = 0
    compressor._last_summary_error = None
    compressor._last_compress_aborted = False
    compressor._last_aux_model_failure_model = None
    compressor._last_aux_model_failure_error = None
    agent.context_compressor = compressor
    agent._compression_feasibility_checked = True
    agent.compression_in_place = False
    return agent


def _contents(rows):
    return [r.get("content") for r in rows]


def _seed_drifted_session(db: SessionDB, session_id: str):
    db.create_session(session_id, source="desktop")
    db.append_message(session_id, "user", "persisted question")
    db.append_message(session_id, "assistant", "persisted answer")

    loaded = db.get_messages_as_conversation(session_id)
    messages = [*loaded, {"role": "user", "content": "LIVE USER INSTRUCTION"}]
    agent = _build_agent_with_db(db, session_id)
    agent._persist_user_message_idx = len(messages) - 1

    db.append_message(session_id, "assistant", "concurrent row 1")
    db.append_message(session_id, "assistant", "concurrent row 2")
    assert len(db.get_messages_as_conversation(session_id)) > len(messages)
    return agent, messages


def test_adoption_preserves_unpersisted_live_user_tail(tmp_path: Path) -> None:
    """A valid contention winner becomes the canonical parent exactly once."""
    db = SessionDB(db_path=tmp_path / "state.db")
    agent, messages = _seed_drifted_session(db, "PREFLIGHT_ADOPT_PARENT")

    agent._compress_context(messages, "sys", approx_tokens=120_000)

    parent_rows = db.get_messages_as_conversation(
        "PREFLIGHT_ADOPT_PARENT", include_inactive=True
    )
    assert _contents(parent_rows) == [
        "persisted question",
        "persisted answer",
        "concurrent row 1",
        "concurrent row 2",
        "LIVE USER INSTRUCTION",
    ]
    assert _contents(agent.context_compressor.compress.call_args.args[0]) == _contents(parent_rows)
    assert agent._persist_user_message_idx == len(parent_rows)
    assert db.get_compression_lock_holder("PREFLIGHT_ADOPT_PARENT") is None
    assert db._conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE parent_session_id = ?",
        ("PREFLIGHT_ADOPT_PARENT",),
    ).fetchone()[0] == 1


def _assert_failed_preflush_leaves_parent_live(db: SessionDB, agent, messages, parent_id: str) -> None:
    """Failure may not create a continuation that omits a live turn."""
    assert agent.context_compressor.compress.call_count == 0
    assert agent.session_id == parent_id
    assert db.get_session(parent_id)["end_reason"] is None
    assert db.get_compression_lock_holder(parent_id) is None
    assert db._conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE parent_session_id = ?", (parent_id,)
    ).fetchone()[0] == 0
    assert _contents(db.get_messages_as_conversation(parent_id)) == [
        "persisted question",
        "persisted answer",
        "concurrent row 1",
        "concurrent row 2",
    ]
    assert _contents(messages)[-1] == "LIVE USER INSTRUCTION"


@pytest.mark.parametrize("flush_failure", ["exception", False, None])
def test_adoption_fails_closed_when_live_tail_preflush_does_not_succeed(
    flush_failure: object, tmp_path: Path
) -> None:
    """A rejected/exhausted append aborts before summary or session rotation."""
    db = SessionDB(db_path=tmp_path / "state.db")
    parent_id = "PREFLIGHT_ADOPT_FLUSH_FAIL"
    agent, messages = _seed_drifted_session(db, parent_id)

    def _failing_flush(*_a, **_kw):
        if flush_failure == "exception":
            raise RuntimeError("flush boom")
        return flush_failure

    with patch.object(agent, "_flush_messages_to_session_db", side_effect=_failing_flush):
        returned, _ = agent._compress_context(messages, "sys", approx_tokens=120_000)

    assert returned is messages
    _assert_failed_preflush_leaves_parent_live(db, agent, messages, parent_id)


@pytest.mark.parametrize("invalid_boundary", [None, -1, 4, True, False])
def test_adoption_fails_closed_when_live_tail_boundary_is_unknown(
    invalid_boundary: object, tmp_path: Path
) -> None:
    """Unknown boundary is never evidence that a live tail is safe to drop."""
    db = SessionDB(db_path=tmp_path / "state.db")
    parent_id = "PREFLIGHT_ADOPT_UNKNOWN_BOUNDARY"
    agent, messages = _seed_drifted_session(db, parent_id)
    agent._persist_user_message_idx = invalid_boundary

    returned, _ = agent._compress_context(messages, "sys", approx_tokens=120_000)

    assert returned is messages
    _assert_failed_preflush_leaves_parent_live(db, agent, messages, parent_id)
