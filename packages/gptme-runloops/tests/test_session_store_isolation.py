"""Recording tests must never inherit an operator's session store."""

from pathlib import Path
from unittest.mock import MagicMock

from gptme_runloops.base import BaseRunLoop
from gptme_runloops.utils.execution import ExecutionResult
from gptme_sessions.store import SessionStore


class RecordingLoop(BaseRunLoop):
    def generate_prompt(self) -> str:
        return "test"


def test_default_store_is_test_local(tmp_path: Path):
    # The autouse fixture, not each recording test, owns the environment seam.
    store = SessionStore()
    assert store.sessions_dir.is_relative_to(tmp_path)
    executor = MagicMock()
    executor.name = "test-executor"
    loop = RecordingLoop(tmp_path, "test", executor=executor)
    loop._record_session(ExecutionResult(exit_code=0))
    records = store.load_all()
    assert len(records) == 1
    assert records[0].harness == "test-executor"


def test_mock_executor_identity_is_not_recorded(tmp_path: Path, caplog):
    loop = RecordingLoop(tmp_path, "test", executor=MagicMock())
    loop._record_session(ExecutionResult(exit_code=0))
    assert SessionStore().load_all() == []
    assert "Session recording failed (non-fatal)" in caplog.text
    assert "harness must be a str" in caplog.text
