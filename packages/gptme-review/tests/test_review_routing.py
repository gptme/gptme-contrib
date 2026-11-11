"""Route admission is portable: a host without a subscription panel still routes."""

from dataclasses import dataclass

import pytest
from gptme_review import core, routing
from gptme_review.routing import OpenRouterBackend, ReviewRoute


@dataclass(frozen=True)
class Rev:
    key: str
    family: str
    transport: str = "cli"


class FakeBackend(OpenRouterBackend):
    def __init__(
        self,
        roster: tuple[Rev, ...] = (),
        *,
        blocked: bool = False,
        sensitive: bool = False,
        overflow: tuple[Rev, ...] = (),
    ) -> None:
        self._roster = roster
        self._blocked = blocked
        self._sensitive = sensitive
        self._overflow = overflow

    def openrouter_unavailable(self) -> bool:
        return self._blocked

    def repo_is_sensitive(self, repo: str) -> bool:
        return self._sensitive

    def reviewer_for_model_spec(self, spec: str) -> Rev | None:
        return next((r for r in self._roster if r.key == spec), None)

    def roster(self) -> tuple[Rev, ...]:
        return self._roster

    def available_reviewers(self, reviewers, *, repo, require_copilot_opt_in):
        return list(reviewers), {}

    def private_fallback_reviewers(self, reviewers):
        return list(reviewers), "ok"

    def overflow_reviewers(self, repo):
        return list(self._overflow), {}


A, B, C = Rev("a", "fa"), Rev("b", "fb"), Rev("c", "fc")


class _Resolved:
    def cli_args(self) -> list[str]:
        return ["--force", "--passes", "3", "--min-agreement", "2"]


class _Production:
    def resolve(self, state: object) -> _Resolved:
        return _Resolved()


@pytest.fixture(autouse=True)
def _production_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--consensus`` expands through PRODUCTION, which reads workspace config."""
    monkeypatch.setattr(routing, "PRODUCTION", _Production())


def test_default_backend_routes_llm_and_agent() -> None:
    be = OpenRouterBackend()
    assert routing.resolve_route("o/r", backend=be).engine == "llm"
    assert routing.resolve_route("o/r", backend=be, engine="agent").engine == "agent"


def test_default_backend_refuses_subscription() -> None:
    class _SensitiveBackend(OpenRouterBackend):
        def repo_is_sensitive(self, repo: str) -> bool:
            return True

    with pytest.raises(core.ReviewError, match="no subscription reviewers configured"):
        routing.resolve_route("o/r", backend=_SensitiveBackend(), engine="subscription")


def test_outage_with_no_panel_refuses_instead_of_llm() -> None:
    be = FakeBackend(blocked=True)
    with pytest.raises(core.ReviewError):
        routing.resolve_route("o/r", backend=be)
    with pytest.raises(core.ReviewError, match="block active"):
        routing.resolve_route("o/r", backend=be, engine="llm")


def test_outage_falls_back_to_subscription() -> None:
    route = routing.resolve_route("o/r", backend=FakeBackend((A, B), blocked=True))
    assert route.engine == "subscription"
    assert route.reason == "OpenRouter outage"
    assert [r.key for r in route.reviewers] == ["a"]


def test_same_family_counts_once() -> None:
    be = FakeBackend((A, Rev("a2", "fa")), blocked=True)
    with pytest.raises(core.ReviewError, match="only 1 independent"):
        routing.resolve_route("o/r", backend=be, passes=2, min_agreement=2)


def test_overflow_adds_third_family_for_two_of_three() -> None:
    be = FakeBackend((A, B), blocked=True, overflow=(C,))
    route = routing.resolve_route("o/r", backend=be, passes=3, min_agreement=2)
    assert [r.key for r in route.reviewers] == ["a", "b", "c"]


def test_unknown_pinned_model_refused() -> None:
    with pytest.raises(core.ReviewError, match="unknown subscription reviewer"):
        routing.resolve_route(
            "o/r", backend=FakeBackend((A,)), engine="subscription", model="zz"
        )


def test_bad_agreement_refused() -> None:
    with pytest.raises(core.ReviewError, match="agreement"):
        routing.resolve_route(
            "o/r", backend=OpenRouterBackend(), passes=1, min_agreement=2
        )


def _cli(be: FakeBackend):
    def resolve(*a, **k):
        return routing.resolve_route(*a, backend=be, **k)

    return lambda argv: routing.resolve_cli_route(argv, resolve=resolve)


def test_admission_full_degraded_and_held() -> None:
    full = routing.consensus_admission(
        "o/r", resolve_cli=_cli(FakeBackend((A, B, C), blocked=True))
    )
    assert (full.mode, full.merge_authority) == ("full", True)

    degraded = routing.consensus_admission(
        "o/r", resolve_cli=_cli(FakeBackend((A,), blocked=True))
    )
    assert degraded.mode == "degraded-1"
    assert degraded.families == ("fa",)
    assert not degraded.merge_authority

    held = routing.consensus_admission(
        "o/r", resolve_cli=_cli(FakeBackend((), blocked=True))
    )
    assert held.mode == "held-until-T"
    assert not held.dispatchable


def test_admission_probe_failure_fails_open() -> None:
    def boom(argv: list[str]) -> ReviewRoute:
        raise RuntimeError("probe")

    assert routing.consensus_admission("o/r", resolve_cli=boom).mode == "unknown"


def test_admission_config_error_does_not_grant_merge_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ReviewError from PRODUCTION config must not produce full/merge_authority.

    Alice's report: when gptme-usage is present but harness-quota.toml has no
    [model_routes] review, PRODUCTION.resolve raises ReviewError. The degraded
    probe then resolves to llm (OpenRouter available). Without the re-probe fix
    this returned ConsensusAdmission('full', merge_authority=True).
    """

    class _BrokenProduction:
        def resolve(self, state: object) -> object:
            raise core.ReviewError("harness-quota.toml missing [model_routes] 'review'")

    monkeypatch.setattr(routing, "PRODUCTION", _BrokenProduction())

    # OpenRouter is available (no blockers, no subscription panel) so the
    # single-pass degraded probe will return llm.
    be = FakeBackend()
    result = routing.consensus_admission("o/r", resolve_cli=_cli(be))
    assert result.mode == "unknown", f"expected unknown, got {result.mode}"
    assert not result.merge_authority, "config error must not grant merge authority"
    assert result.cause == "probe_error"


def test_refusal_none_when_admitted() -> None:
    cli = _cli(FakeBackend((A, B, C), blocked=True))
    assert routing.consensus_route_refusal("o/r", resolve_cli=cli) is None


def test_main_exit_codes(capsys: pytest.CaptureFixture[str]) -> None:
    ok = _cli(FakeBackend())
    assert routing.main(["o/r", "--passes", "1"], resolve_cli=ok) == 0
    assert capsys.readouterr().out.strip() == "llm"
    refused = _cli(FakeBackend(blocked=True))
    assert routing.main(["o/r", "--passes", "1"], resolve_cli=refused) == 75
