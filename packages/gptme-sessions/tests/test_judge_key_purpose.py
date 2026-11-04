"""Working judge payloads cannot borrow isolated evaluation credentials."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gptme_sessions import judge


@pytest.mark.parametrize("context", ["JUDGE", "LLM_JUDGE"])
@pytest.mark.parametrize("location", ["env", "config"])
@pytest.mark.parametrize("shared", [False, True])
def test_working_resolution_excludes_eval(tmp_path, context, location, shared):
    env = {"OPENROUTER_API_KEY": "synthetic-shared"} if shared else {}
    config = tmp_path / "synthetic.toml"
    config.write_text('[env]\nOPENROUTER_API_KEY_EVAL="synthetic-eval"\n')
    if location == "env":
        env["OPENROUTER_API_KEY_EVAL"] = "synthetic-eval"
    paths = (config,) if location == "config" else ()
    assert judge._resolve_openrouter_api_key(context, environ=env, config_paths=paths) == (
        "synthetic-shared" if shared else None
    )


@pytest.mark.parametrize(
    ("context", "name", "expected"),
    [
        ("JUDGE", "LLM_JUDGE", "synthetic-working"),
        ("LLM_JUDGE", "JUDGE", "synthetic-working"),
        ("EVAL", "JUDGE", "synthetic-working"),
        ("EVAL", "EVAL", "synthetic-eval"),
    ],
)
def test_permitted_key_purpose_controls(context, name, expected):
    assert (
        judge._resolve_openrouter_api_key(
            context, environ={f"OPENROUTER_API_KEY_{name}": expected}, config_paths=()
        )
        == expected
    )


def test_environment_precedence_remains_compatible(tmp_path):
    config = tmp_path / "synthetic.toml"
    config.write_text('[env]\nOPENROUTER_API_KEY_JUDGE="synthetic-config"\n')
    assert (
        judge._resolve_openrouter_api_key(
            "JUDGE", environ={"OPENROUTER_API_KEY": "synthetic-env"}, config_paths=(config,)
        )
        == "synthetic-env"
    )


@pytest.mark.parametrize("available", [False, True])
@pytest.mark.parametrize("location", ["env", "config"])
def test_delegated_openrouter_boundary(monkeypatch, tmp_path, available, location):
    """Exercise context admission before mocked initialization and reply."""
    pytest.importorskip("gptme")
    import gptme.init
    import gptme.llm

    env = {"OPENROUTER_API_KEY_EVAL": "synthetic-eval"} if location == "env" else {}
    if available and location == "env":
        env["OPENROUTER_API_KEY_JUDGE"] = "synthetic-working"
    monkeypatch.setattr(judge, "os", SimpleNamespace(environ=env))
    real_resolve = judge._resolve_openrouter_api_key
    config = tmp_path / "synthetic.toml"
    config.write_text(
        '[env]\nOPENROUTER_API_KEY_EVAL="synthetic-eval"\n'
        + ('OPENROUTER_API_KEY_JUDGE="synthetic-working"\n' if available else "")
    )
    monkeypatch.setattr(
        judge,
        "_resolve_openrouter_api_key",
        lambda context: real_resolve(context, environ=env, config_paths=(config,)),
    )
    seen = []

    def init(**kwargs):
        seen.append(env.get("OPENROUTER_API_KEY"))

    initialize = Mock(side_effect=init)
    reply = Mock(return_value=SimpleNamespace(content='{"score": 0.7, "reason": "ok"}'))
    monkeypatch.setattr(gptme.init, "init", initialize)
    monkeypatch.setattr(gptme.llm, "reply", reply)
    result = judge.judge_session("synthetic journal", model="openrouter/synthetic/model")
    if available:
        assert result is not None
        assert seen == ["synthetic-working"]
        reply.assert_called_once()
    else:
        assert result is None
        initialize.assert_not_called()
        reply.assert_not_called()
    assert "OPENROUTER_API_KEY" not in env


@pytest.mark.parametrize("raises", [False, True])
def test_context_restores_shared_key(monkeypatch, raises):
    env = {"OPENROUTER_API_KEY": "synthetic-old"}
    monkeypatch.setattr(judge, "os", SimpleNamespace(environ=env))
    monkeypatch.setattr(judge, "_resolve_openrouter_api_key", lambda context: "synthetic-working")
    try:
        with judge._judge_openrouter_env("openrouter/synthetic/model"):
            assert env["OPENROUTER_API_KEY"] == "synthetic-working"
            if raises:
                raise RuntimeError("synthetic failure")
    except RuntimeError:
        assert raises
    assert env["OPENROUTER_API_KEY"] == "synthetic-old"


def test_non_openrouter_context_does_not_resolve(monkeypatch):
    resolve = Mock(side_effect=AssertionError("must not resolve"))
    monkeypatch.setattr(judge, "_resolve_openrouter_api_key", resolve)
    with judge._judge_openrouter_env("lmstudio/synthetic/model"):
        pass
    resolve.assert_not_called()
