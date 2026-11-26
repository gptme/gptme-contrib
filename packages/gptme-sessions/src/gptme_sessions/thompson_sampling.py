"""Thompson sampling bandit engine for session optimization.

Uses Beta-Bernoulli bandit models to learn which work categories, session modes,
or lessons are most productive. Agent-agnostic: any agent can use this to
optimize any discrete choice problem.

Key concepts:
- alpha: successes + 1 prior
- beta: failures + 1 prior
- Sampling: Draw from Beta(alpha, beta) to get effectiveness score
- Exploration: Uncertain arms get sampled more due to wider distribution
- Decay: Exponential decay toward prior handles non-stationarity
- Contextual arms: Learn separate posteriors per (category, model) context

Typical uses:
    # Work category optimization (CASCADE-style)
    bandit = Bandit(state_dir="state/category-bandit")
    scores = bandit.sample(["code", "triage", "infrastructure", "content"])
    recommended = max(scores, key=scores.get)
    ...
    bandit.update(["infrastructure"], outcome=0.7, context=("infrastructure", "opus"))

    # Lesson effectiveness optimization
    bandit = Bandit(state_dir="state/lesson-bandit")
    scores = bandit.sample(lesson_paths)
    selected = sorted(lesson_paths, key=lambda p: scores[p], reverse=True)[:5]

CLI:
    python3 -m gptme_sessions.thompson_sampling status
    python3 -m gptme_sessions.thompson_sampling sample --arms code triage infra
    python3 -m gptme_sessions.thompson_sampling update --outcome 0.7 --arms infra
    python3 -m gptme_sessions.thompson_sampling dashboard
"""

import contextlib
import json
import math
import os
import random
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, ClassVar, Iterator, Protocol

import click


def _atomic_write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` so readers never see a partial file.

    Writes a sibling temp file, fsyncs it and ``os.replace``s it over the
    target, so a crash mid-write leaves the previous file intact.
    """
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


# An arm selected more than this many times carries real posterior evidence.
MATURE_ARM_MIN_SELECTIONS = 10


def removed_mature_arms(
    previous: dict,
    updated: dict,
    *,
    min_selections: int = MATURE_ARM_MIN_SELECTIONS,
) -> dict[str, int]:
    """Return arms present in ``previous`` but not ``updated`` with real evidence.

    Only top-level ``arms`` with ``total_selections > min_selections`` count:
    dropping a fresh arm is cheap, dropping a mature one erases history that
    cannot be rebuilt.  Missing or malformed ``arms`` maps yield ``{}``.
    """
    previous_arms = previous.get("arms")
    updated_arms = updated.get("arms")
    if not isinstance(previous_arms, dict) or not isinstance(updated_arms, dict):
        return {}
    removed: dict[str, int] = {}
    for arm_id, arm in previous_arms.items():
        if arm_id in updated_arms or not isinstance(arm, dict):
            continue
        selections = arm.get("total_selections", 0)
        if isinstance(selections, (int, float)) and selections > min_selections:
            removed[arm_id] = int(selections)
    return removed


def _has_mature_arm(document: object) -> bool:
    """Whether a raw (possibly undecodable) state document holds a mature arm."""
    if not isinstance(document, dict):
        return False
    arm_maps = [document.get("arms")]
    contextual = document.get("contextual_arms")
    if isinstance(contextual, dict):
        arm_maps.extend(contextual.values())
    for arms in arm_maps:
        if not isinstance(arms, dict):
            continue
        for arm in arms.values():
            if not isinstance(arm, dict):
                continue
            if _arm_evidence(arm) > MATURE_ARM_MIN_SELECTIONS:
                return True
    return False


def _document_holds_mature_history(text: str) -> bool:
    """Whether serialized state that strict decoding rejects still holds mature arms."""
    try:
        document = json.loads(text)
    except ValueError:
        return False
    return _has_mature_arm(document)


def _arm_evidence(arm: dict) -> float:
    """Observations a raw arm document claims, whatever its writer called the counter.

    A newer ``arm_class`` may rename or drop ``total_selections``.  Unknown
    maturity must read as mature, so a missing or non-numeric counter counts as
    unbounded evidence.  The posterior is no proxy for it: decay keeps
    ``alpha + beta - 2`` below ``1 / (1 - gamma)`` however many observations
    the arm has.  It only adds evidence when the counter is known.  An arm
    that records nothing but its id holds no history to lose.
    """
    if not arm.keys() - {"arm_id"}:
        return 0.0
    selections = arm.get("total_selections")
    if not isinstance(selections, (int, float)) or isinstance(selections, bool):
        return math.inf
    evidence = float(selections)
    alpha, beta = arm.get("alpha"), arm.get("beta")
    if (
        isinstance(alpha, (int, float))
        and isinstance(beta, (int, float))
        and not isinstance(alpha, bool)
        and not isinstance(beta, bool)
    ):
        evidence = max(evidence, float(alpha) + float(beta) - 2.0)
    return evidence


@dataclass
class BanditArm:
    """A single arm modeled as a Beta-Bernoulli bandit.

    Represents a work category, lesson, or any discrete option being optimized.
    """

    arm_id: str
    alpha: float = 1.0  # successes + prior
    beta: float = 1.0  # failures + prior
    total_selections: int = 0
    total_rewards: int = 0  # sessions where reward > 0.5
    last_updated: str = ""

    @property
    def mean(self) -> float:
        """Expected effectiveness (Beta distribution mean)."""
        return self.alpha / (self.alpha + self.beta)

    @property
    def variance(self) -> float:
        """Uncertainty about effectiveness (Beta distribution variance)."""
        a, b = self.alpha, self.beta
        return (a * b) / ((a + b) ** 2 * (a + b + 1))

    @property
    def ucb(self) -> float:
        """Upper confidence bound (mean + 1 std dev)."""
        return self.mean + float(self.variance**0.5)

    def sample(self, rng: random.Random | None = None) -> float:
        """Draw a sample from the Beta distribution.

        Higher uncertainty → wider distribution → more exploration.
        """
        if rng:
            return rng.betavariate(self.alpha, self.beta)
        return random.betavariate(self.alpha, self.beta)

    def update(self, reward: bool | float) -> None:
        """Update belief after observing outcome.

        Args:
            reward: Accepts bool or float in [0, 1].
                - True/1.0: alpha += 1 (success)
                - False/0.0: beta += 1 (failure)
                - 0.3: mostly unsuccessful → beta grows faster than alpha

        Raises:
            ValueError: If reward is outside [0, 1].
        """
        r = float(reward)
        if not 0.0 <= r <= 1.0:
            raise ValueError(f"reward must be in [0, 1], got {r}")
        self.alpha += r
        self.beta += 1.0 - r
        self.total_selections += 1
        if r > 0.5:
            self.total_rewards += 1
        self.last_updated = datetime.now(timezone.utc).isoformat()

    def apply_decay(self, gamma: float) -> None:
        """Apply exponential decay toward the prior Beta(1, 1).

        Handles non-stationarity: as agent capabilities change, older
        observations contribute less.

        Formula: alpha = 1 + gamma * (alpha - 1)
                 beta  = 1 + gamma * (beta  - 1)

        Args:
            gamma: Decay rate in (0, 1). gamma=0.99 → ~100-observation window.
        """
        self.alpha = 1.0 + gamma * (self.alpha - 1.0)
        self.beta = 1.0 + gamma * (self.beta - 1.0)


def _context_key(context: tuple[str, ...] | None) -> str:
    """Serialize a context tuple to a JSON string for use as dict key."""
    if context is None:
        return "null"
    return json.dumps(list(context))


def _parse_context_key(key: str) -> tuple[str, ...] | None:
    """Deserialize a context key back to a tuple or None."""
    if key == "null":
        return None
    return tuple(json.loads(key))


def _context_fallback_chain(
    context: tuple[str, ...] | None,
) -> list[tuple[str, ...] | None]:
    """Generate hierarchical fallback chain for a context.

    For (category, model): try exact → category-only → model-only → global.
    For (single,): try exact → global.
    For None: just global.
    """
    chain: list[tuple[str, ...] | None] = []
    if context is not None:
        chain.append(context)
        if len(context) == 2:
            chain.append((context[0],))
            chain.append((context[1],))
    chain.append(None)
    return chain


# Minimum observations before trusting a contextual arm over fallback
CONTEXTUAL_MIN_OBSERVATIONS = 3

# A contextual arm last updated this long before its global arm is stale. When
# writers move from a partial context like ``(run_type,)`` to ``(run_type,
# model)``, the old arm keeps its observations and would otherwise shadow the
# continuously updated global arm forever. Comparing against the global arm's
# timestamp, not wall time, keeps quiet but consistent snapshots usable.
CONTEXTUAL_MAX_GLOBAL_LAG_DAYS = 30


def _updated_at(arm: "BanditArm") -> datetime | None:
    try:
        updated = datetime.fromisoformat(arm.last_updated)
    except (TypeError, ValueError):
        return None
    return updated if updated.tzinfo is not None else updated.replace(tzinfo=timezone.utc)


def _lags_global(arm: "BanditArm", global_arm: "BanditArm | None") -> bool:
    """True when ``arm`` is proven stale relative to live global evidence.

    Missing or unparseable timestamps are accepted for backward compatibility.
    """
    if global_arm is None:
        return False
    contextual, latest = _updated_at(arm), _updated_at(global_arm)
    if contextual is None or latest is None:
        return False
    return latest - contextual > timedelta(days=CONTEXTUAL_MAX_GLOBAL_LAG_DAYS)


@dataclass
class BanditState:
    """Persistent state for the bandit system.

    Supports both unconditional (context=None) and contextual arms.
    Contextual arms are stored in a nested dict: contextual_arms[arm_id][context_key].
    The global (unconditional) arm is always at context_key="null".
    """

    arms: dict[str, BanditArm] = field(default_factory=dict)
    contextual_arms: dict[str, dict[str, BanditArm]] = field(default_factory=dict)
    total_sessions: int = 0
    created: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    last_updated: str = ""

    #: Arm type this state constructs.  A subclass that keeps a legacy arm API
    #: (e.g. a renamed identity field) overrides it; the engine only ever builds
    #: arms as ``self.arm_class(arm_id=...)``.
    #:
    #: Contract: ``arm_class`` is the *single* source of truth for the arm type.
    #: :meth:`Bandit._decode_state` builds loaded arms from
    #: ``state_class.arm_class`` and the state instance itself builds new arms
    #: from ``self.arm_class``; both are the same class attribute, so a state
    #: subclass can never disagree with the decoder.  To use a subclass arm, set
    #: ``arm_class`` on a :class:`BanditState` subclass and point
    #: ``Bandit.state_class`` at that subclass.  Overriding ``arm_class`` without
    #: also setting ``state_class`` on the bandit is inert: a plain ``Bandit``
    #: instantiates ``BanditState`` and never sees the override.
    arm_class: ClassVar[type[BanditArm]] = BanditArm

    def get_or_create_arm(self, arm_id: str) -> BanditArm:
        """Get existing arm or create new one with uniform prior."""
        if arm_id not in self.arms:
            self.arms[arm_id] = self.arm_class(arm_id=arm_id)
        return self.arms[arm_id]

    def get_or_create_contextual_arm(
        self, arm_id: str, context: tuple[str, ...] | None
    ) -> BanditArm:
        """Get or create a contextual arm."""
        key = _context_key(context)
        if arm_id not in self.contextual_arms:
            self.contextual_arms[arm_id] = {}
        if key not in self.contextual_arms[arm_id]:
            self.contextual_arms[arm_id][key] = self.arm_class(arm_id=arm_id)
        return self.contextual_arms[arm_id][key]

    def _resolve_arm_for_sampling(self, arm_id: str, context: tuple[str, ...] | None) -> BanditArm:
        """Find the best arm for sampling using hierarchical fallback.

        Read-only: never creates new arms. Returns a temporary BanditArm with
        uninformative prior Beta(1, 1) for unknown arms — sampling from it gives
        an expected score of 0.5, the correct uninformative default.
        """
        existing = self.arms.get(arm_id)
        if context is not None:
            for ctx in _context_fallback_chain(context):
                key = _context_key(ctx)
                if arm_id in self.contextual_arms and key in self.contextual_arms[arm_id]:
                    arm = self.contextual_arms[arm_id][key]
                    if ctx is None:
                        return arm
                    if arm.total_selections >= CONTEXTUAL_MIN_OBSERVATIONS and not _lags_global(
                        arm, existing
                    ):
                        return arm

        if existing is not None:
            return existing
        # Return a temporary arm with uninformative prior — not stored in state
        return self.arm_class(arm_id=arm_id)

    def sample_scores(
        self,
        arm_ids: list[str],
        seed: int | None = None,
        context: tuple[str, ...] | None = None,
    ) -> dict[str, float]:
        """Sample effectiveness scores for a set of candidate arms.

        Args:
            arm_ids: Arm IDs to score.
            seed: Optional random seed for reproducibility.
            context: Optional context tuple, e.g. ("infrastructure", "opus").
                     Uses hierarchical fallback if contextual data is sparse.

        Returns:
            Dict mapping arm_id → sampled score [0, 1].
        """
        rng = random.Random(seed)
        scores = {}
        for arm_id in arm_ids:
            arm = self._resolve_arm_for_sampling(arm_id, context)
            scores[arm_id] = arm.sample(rng)
        return scores

    def update_session(
        self,
        active_arms: list[str],
        outcome: str | float,
        context: tuple[str, ...] | None = None,
        per_arm_rewards: dict[str, float] | None = None,
    ) -> dict[str, bool]:
        """Update arms based on session outcome.

        Args:
            active_arms: Arm IDs that were active in this session.
            outcome: Session outcome. Accepts:
                - str: 'productive' (1.0), 'noop'/'failed' (0.0),
                  'violated_policy' (0.0, counted as policy violation)
                - float in [0, 1]: Graded reward for nuanced learning.
            context: Optional context tuple. When provided, updates both
                     the contextual arm AND the global arm.
            per_arm_rewards: Optional per-arm rewards for salience-weighted
                credit assignment. Missing arms fall back to base reward.

        Returns:
            Dict mapping arm_id → bool (True if reward > 0.5).
        """
        if isinstance(outcome, str):
            if outcome == "productive":
                base_reward = 1.0
            elif outcome in ("noop", "failed", "violated_policy"):
                base_reward = 0.0
            else:
                raise ValueError(
                    f"Unknown string outcome: {outcome!r}. "
                    "Expected 'productive', 'noop', 'failed', 'violated_policy', "
                    "or a float in [0, 1]."
                )
        else:
            base_reward = float(outcome)

        updates = {}
        for arm_id in active_arms:
            reward = per_arm_rewards.get(arm_id, base_reward) if per_arm_rewards else base_reward
            arm = self.get_or_create_arm(arm_id)
            arm.update(reward)
            updates[arm_id] = reward > 0.5

            if context is not None:
                ctx_arm = self.get_or_create_contextual_arm(arm_id, context)
                ctx_arm.update(reward)

        self.total_sessions += 1
        self.last_updated = datetime.now(timezone.utc).isoformat()
        return updates

    def rank_by_expected(self) -> list[tuple[str, float]]:
        """Rank arms by expected effectiveness (exploitation-only view)."""
        return sorted(
            [(arm_id, arm.mean) for arm_id, arm in self.arms.items()],
            key=lambda x: x[1],
            reverse=True,
        )

    def rank_by_ucb(self) -> list[tuple[str, float]]:
        """Rank arms by upper confidence bound (exploration-aware)."""
        return sorted(
            [(arm_id, arm.ucb) for arm_id, arm in self.arms.items()],
            key=lambda x: x[1],
            reverse=True,
        )

    def apply_decay(self, gamma: float, only: set[str] | None = None) -> int:
        """Apply exponential decay to arms (global + contextual).

        Args:
            gamma: Decay rate in (0, 1). Higher = slower decay.
            only: If given, decay just these arm IDs (their global arm and all
                contextual sub-arms); unknown IDs are ignored.

        Returns:
            Number of arms decayed.
        """
        count = 0
        for arm_id, arm in self.arms.items():
            if only is None or arm_id in only:
                arm.apply_decay(gamma)
                count += 1
        for arm_id, contexts in self.contextual_arms.items():
            if only is not None and arm_id not in only:
                continue
            for arm in contexts.values():
                arm.apply_decay(gamma)
                count += 1
        return count

    def prune_stale(self, min_selections: int = 0, max_age_days: int = 90) -> int:
        """Remove arms that haven't been selected recently.

        Prunes both global arms and contextual arms.

        Args:
            min_selections: Prune arms with at most this many selections (total_selections <= min_selections). Default 0 prunes only never-selected arms.
            max_age_days: Prune arms not updated in this many days.

        Returns:
            Number of arms pruned.
        """
        now = datetime.now(timezone.utc)
        pruned = 0

        def _is_stale(arm: "BanditArm") -> bool:
            # Age-based pruning applies to all arms, regardless of selection count
            if arm.last_updated:
                last = datetime.fromisoformat(arm.last_updated)
                if (now - last).days > max_age_days:
                    return True
            # Selection guard: protect arms above threshold from being pruned by count alone
            if arm.total_selections > min_selections:
                return False
            # Arms at or below min_selections threshold are stale
            return True

        # Prune global arms
        to_prune = [arm_id for arm_id, arm in self.arms.items() if _is_stale(arm)]
        for arm_id in to_prune:
            del self.arms[arm_id]
        pruned += len(to_prune)

        # Prune contextual arms
        for arm_id in list(self.contextual_arms.keys()):
            stale_keys = [k for k, arm in self.contextual_arms[arm_id].items() if _is_stale(arm)]
            for ctx_key in stale_keys:
                del self.contextual_arms[arm_id][ctx_key]
            pruned += len(stale_keys)
            # Remove empty arm_id entries
            if not self.contextual_arms[arm_id]:
                del self.contextual_arms[arm_id]

        return pruned


class UpdateHookError(RuntimeError):
    """A hook failed *after* the posterior was already persisted.

    Raised from :meth:`Bandit.update` when ``saved`` or ``complete`` raises.
    Unlike an exception from ``prepare``, this does **not** mean the update was
    rolled back: the state file and the in-memory state already reflect it.
    Callers must not retry ``update()`` on this error (that would apply the
    reward twice); reconcile the external ledger against the posterior instead.
    """


@dataclass(frozen=True)
class UpdatePlan:
    """What a :meth:`Bandit.update` call is about to apply, handed to an :class:`UpdateHook`."""

    active_arms: tuple[str, ...]
    outcome: str | float
    context: tuple[str, ...] | None
    decay_rate: float | None
    per_arm_decay: bool
    per_arm_rewards: dict[str, float] | None
    state_file: Path


class UpdateHook(Protocol):
    """Observer for the write-ahead / write-complete phases of :meth:`Bandit.update`.

    Lets a caller keep an external provenance ledger (receipts) coupled to the
    posterior write without the engine knowing the ledger's schema. Phases:

    1. ``prepare`` runs under the state lock, after state is re-loaded and before
       any mutation. It receives the plan and the loaded (unmodified) state and
       returns an opaque token. If it raises, nothing is mutated or saved.
    2. ``saved`` runs under the lock right after the atomic state save.
    3. ``complete`` runs after the lock is released.

    A ``prepare`` with no matching ``complete`` is **ambiguous**: either nothing
    was written (``prepare`` succeeded, then the caller or process aborted
    before the mutation) or the posterior was written and the process died
    before ``complete``. Never read it as "no write happened" — resolve it by
    comparing the ledger's prepared record against the posterior, which is the
    only evidence that distinguishes the two.

    Failure semantics differ per phase: an exception from ``prepare`` aborts
    before anything is mutated or saved, but an exception from ``saved`` or
    ``complete`` propagates as :class:`UpdateHookError` with the update already
    persisted. Callers must not retry ``update()`` in that case.

    ``prepare`` and ``saved`` run while the state lock is held, and that lock is
    a non-reentrant ``flock`` on a separate descriptor. A hook that calls a
    locked :class:`Bandit` method (``update``, ``decay``, ``prune``, ``save``)
    from either phase deadlocks the caller. Read the state passed to
    ``prepare`` instead; do not re-enter the bandit.
    """

    def prepare(self, plan: UpdatePlan, state: "BanditState") -> Any: ...

    def saved(self, token: Any) -> None: ...

    def complete(self, token: Any) -> None: ...


class Bandit:
    """Manager for a Thompson sampling bandit system.

    Handles persistence, initialization, and high-level operations.
    Use this for any discrete optimization problem: work categories,
    lesson selection, model routing, etc.

    Example:
        # Category optimization
        bandit = Bandit(state_dir="state/category-bandit")
        scores = bandit.sample(["code", "triage", "infrastructure"])
        # ... do work ...
        bandit.update(["infrastructure"], outcome=0.8)

        # With context (category × model)
        scores = bandit.sample(["code", "infra"], context=("autonomous", "opus"))
        bandit.update(["infra"], outcome=0.6, context=("autonomous", "opus"))
    """

    DEFAULT_STATE_DIR = "state/bandit"
    DEFAULT_STATE_FILE = "bandit-state.json"

    #: State type this bandit loads and persists.  This is the hook a subclass
    #: overrides to change the arm type: the arm class is read from
    #: ``state_class.arm_class`` when decoding and from the instantiated state
    #: when creating arms, so both paths always agree.  Setting ``arm_class`` on
    #: a state subclass without setting ``state_class`` here has no effect.
    state_class: ClassVar[type[BanditState]] = BanditState

    def __init__(
        self,
        state_dir: str | Path | None = None,
        state_file: str = DEFAULT_STATE_FILE,
    ):
        self.state_dir = Path(state_dir or self.DEFAULT_STATE_DIR)
        self.state_file = self.state_dir / state_file
        self.state = self._load_state()

    @contextlib.contextmanager
    def _state_lock(self) -> Iterator[None]:
        """Serialize read-modify-write cycles across processes.

        Several processes may share one bandit state file (session-end
        updaters, timers, CLI calls).  Loading at construction and saving
        later is a lost-update race: two instances that both loaded the same
        snapshot will clobber each other's arm updates on save.  Mutating
        methods must hold this flock *and* re-load the state from disk before
        applying their mutation (see ``update()``/``decay()``/``prune()``).

        ``save()`` must not take this lock: it runs inside the critical
        section and a second flock on the same file would deadlock.
        """
        # Keep read-only imports usable without POSIX support, but never silently
        # run a persisted mutation without the promised cross-process lock.
        import fcntl

        self.state_dir.mkdir(parents=True, exist_ok=True)
        with open(self.lock_file, "a", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)

    @property
    def lock_file(self) -> Path:
        """Sidecar lock path; adapters may retain a legacy filename."""
        return self.state_file.with_name(self.state_file.name + ".lock")

    @property
    def backup_file(self) -> Path:
        """Last known-good copy of the state file, kept by ``save()``."""
        return self.state_file.with_name(self.state_file.name + ".bak")

    @classmethod
    def _decode_state(cls, text: str) -> BanditState:
        """Deserialize a state document for both loading and backup validation."""
        data = json.loads(text)
        state = cls.state_class(
            total_sessions=data.get("total_sessions", 0),
            created=data.get("created", ""),
            last_updated=data.get("last_updated", ""),
        )
        arm_class = cls.state_class.arm_class

        def build(arm_data: dict) -> BanditArm:
            arm_data = dict(arm_data)
            # Support old format where key was 'lesson_path'
            if "lesson_path" in arm_data and "arm_id" not in arm_data:
                arm_data["arm_id"] = arm_data.pop("lesson_path")
            return arm_class(**arm_data)

        for arm_id, arm_data in data.get("arms", {}).items():
            state.arms[arm_id] = build(arm_data)
        for arm_id, contexts in data.get("contextual_arms", {}).items():
            state.contextual_arms[arm_id] = {
                ctx_key: build(arm_data) for ctx_key, arm_data in contexts.items()
            }
        return state

    def _load_state(self) -> BanditState:
        """Load bandit state from disk.

        A corrupt state file falls back to the ``.bak`` copy kept by
        ``save()``, so one bad write cannot discard accumulated history.
        """
        for path in (self.state_file, self.backup_file):
            if not path.exists():
                continue
            try:
                state = self._decode_state(path.read_text())
                if path != self.state_file:
                    print(
                        f"Warning: loaded bandit state from backup {path.name} "
                        f"({state.total_sessions} sessions recovered)",
                        file=sys.stderr,
                    )
                return state
            except (ValueError, TypeError, KeyError, AttributeError) as e:
                print(f"Warning: corrupted bandit state in {path.name}: {e}", file=sys.stderr)
        return self.state_class()

    def save(self, *, allow_arm_deletion: bool = False) -> None:
        """Persist bandit state to disk.

        Refuses to drop a mature arm (see :func:`removed_mature_arms`) that the
        persisted state still holds, so a caller that rebuilt or filtered
        ``state.arms`` cannot silently erase accumulated evidence.  Pass
        ``allow_arm_deletion=True`` only for deliberate removals such as
        :meth:`prune`.

        Also refuses to overwrite mature history that cannot be decoded; that
        check has no override.

        Raises:
            ValueError: if mature arms would be removed without the override, or
                neither copy of the persisted state can be decoded and one of
                them holds a mature arm.
        """
        self._refuse_overwriting_undecodable_history()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        data = self._serialize_state()
        if not allow_arm_deletion:
            self._refuse_mature_arm_removal(data)
        self._backup_current()
        self._set_aside_undecodable_state()
        _atomic_write_text(self.state_file, json.dumps(data, indent=2) + "\n")

    def _serialize_state(self) -> dict:
        """Build the persisted document; adapters may retain a legacy wire format."""
        data: dict = {
            "arms": {arm_id: asdict(arm) for arm_id, arm in self.state.arms.items()},
            "total_sessions": self.state.total_sessions,
            "created": self.state.created,
            "last_updated": self.state.last_updated,
        }
        if self.state.contextual_arms:
            data["contextual_arms"] = {
                arm_id: {ctx_key: asdict(arm) for ctx_key, arm in contexts.items()}
                for arm_id, contexts in self.state.contextual_arms.items()
            }
        return data

    def _set_aside_undecodable_state(self) -> None:
        """Keep a state file this writer cannot load instead of overwriting it.

        ``_refuse_overwriting_undecodable_history()`` only stops a save when no
        copy loads at all. When the backup does load, ``_load_state()`` recovers
        from it — and writing that recovered state back would drop everything a
        richer writer recorded in its newer state file. Moving the unreadable
        file aside keeps that evidence without blocking the save.

        If that move fails while the file still holds a mature arm, the save
        aborts: overwriting it would erase the history this path exists to keep.

        Bytes that are not valid UTF-8 are kept aside too, but never abort the
        save: nothing here can read them, so refusing would wedge every future
        ``update()``/``decay()``/``prune()`` on a file the next write repairs.
        """
        try:
            raw = self.state_file.read_bytes()
        except OSError:
            return
        try:
            text: str | None = raw.decode()
        except UnicodeDecodeError:
            text = None
        if text is not None:
            try:
                self._decode_state(text)
                return
            except (ValueError, TypeError, KeyError, AttributeError):
                pass
        target = self._undecodable_state_archive()
        try:
            self.state_file.replace(target)
        except OSError as e:
            # A rename can fail where a plain copy succeeds (e.g. the target is
            # on another filesystem). Preserving the bytes is what matters, not
            # the rename itself, so try a copy before giving up.
            try:
                target.write_bytes(raw)
            except OSError:
                if text is not None and _document_holds_mature_history(text):
                    raise ValueError(
                        f"refusing to overwrite bandit state that cannot be decoded: "
                        f"{self.state_file} holds mature arms and could not be kept "
                        f"aside at {target} ({e})"
                    ) from e
                print(
                    f"Warning: could not keep unreadable bandit state aside ({e}); overwriting it",
                    file=sys.stderr,
                )
                return
        print(
            f"Warning: kept unreadable bandit state as {target.name} "
            "(it may hold observations this reader cannot load)",
            file=sys.stderr,
        )

    def _undecodable_state_archive(self) -> Path:
        """Destination for a state file this writer cannot load.

        The stamp carries microseconds: a second-resolution name let a second
        archival within the same second silently overwrite the first archive,
        destroying the very observations this path exists to keep.
        """
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        return self.state_file.with_name(f"{self.state_file.name}.undecodable-{stamp}")

    def _undecodable_backup_archive(self) -> Path:
        """Destination for a ``.bak`` this writer cannot parse."""
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        return self.backup_file.with_name(f"{self.backup_file.name}.undecodable-{stamp}")

    def _persisted_document(self) -> dict:
        """Return the first loadable persisted state document, or ``{}``.

        Mirrors ``_load_state()``: a corrupt state file falls back to the
        backup, so the guard compares against the evidence that was recovered.
        """
        for path in (self.state_file, self.backup_file):
            try:
                text = path.read_text()
                self._decode_state(text)
                data = json.loads(text)
            except (OSError, ValueError, TypeError, KeyError, AttributeError):
                continue
            if isinstance(data, dict):
                return data
        return {}

    def _refuse_mature_arm_removal(self, data: dict) -> None:
        removed = removed_mature_arms(self._persisted_document(), data)
        if removed:
            details = ", ".join(f"{arm_id} (n={n})" for arm_id, n in sorted(removed.items()))
            raise ValueError(
                f"refusing to delete mature bandit arm(s): {details}; "
                "pass allow_arm_deletion=True only for a deliberate removal"
            )

    def _refuse_overwriting_undecodable_history(self) -> None:
        """Refuse to save when real evidence exists but no copy of it loads.

        A writer with a richer ``arm_class`` (or a newer engine) makes both files
        fail strict decoding. ``_load_state()`` then returns an empty state, and
        saving that would erase the history, first from the state file and on
        the next save from the backup. Files that are not valid JSON hold
        nothing recoverable here and are still repaired by saving.

        A decodable backup is not enough on its own when the state file is the
        newer of the two: ``_set_aside_undecodable_state()`` keeps that file
        before it is replaced, so the observations it holds survive.  That
        archival step aborts the save if it cannot move mature evidence aside.
        """
        unreadable: list[str] = []
        for path in (self.state_file, self.backup_file):
            try:
                text = path.read_text()
            except (OSError, UnicodeDecodeError):
                continue
            try:
                self._decode_state(text)
                return
            except (ValueError, TypeError, KeyError, AttributeError):
                pass
            try:
                document = json.loads(text)
            except ValueError:
                continue
            if _has_mature_arm(document):
                unreadable.append(str(path))
        if unreadable:
            raise ValueError(
                f"refusing to overwrite bandit state that cannot be decoded: "
                f"{', '.join(unreadable)} holds mature arms. Load it with the arm "
                "class that wrote it, or move the files aside to start over."
            )

    def _backup_current(self) -> None:
        """Copy the current state file to ``.bak`` if it can be loaded.

        A file that cannot be deserialized is never backed up: copying it would
        replace the good backup that ``_load_state()`` just recovered from.
        """
        try:
            current = self.state_file.read_text()
            self._decode_state(current)
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            return
        if self._backup_holds_undecodable_history():
            print(
                f"Warning: backup not rotated: {self.backup_file} holds mature arms "
                "that cannot be decoded; recover or move it aside",
                file=sys.stderr,
            )
            return
        self._set_aside_unparseable_backup()
        _atomic_write_text(self.backup_file, current)

    def _set_aside_unparseable_backup(self) -> None:
        """Keep a torn ``.bak`` before a good state is rotated over it.

        ``_backup_holds_undecodable_history()`` handles a backup that is valid
        JSON but not decodable by this arm class. Bytes that are not even JSON
        (a torn write) can still hold observations a richer writer could load,
        so they are moved aside rather than silently replaced.
        """
        try:
            raw = self.backup_file.read_bytes()
        except OSError:
            return
        try:
            text = raw.decode()
        except UnicodeDecodeError:
            text = None
        if text is not None:
            try:
                json.loads(text)
                return
            except ValueError:
                pass
        target = self._undecodable_backup_archive()
        try:
            self.backup_file.replace(target)
        except OSError:
            return
        print(
            f"Warning: kept unparseable bandit backup as {target.name} before rotating it",
            file=sys.stderr,
        )

    def _backup_holds_undecodable_history(self) -> bool:
        """Whether ``.bak`` is the only copy of mature history we cannot load.

        This is the half-erased case: the state file was already reset by a
        reader that could not decode it, and rotating would finish the job.
        """
        try:
            text = self.backup_file.read_text()
        except (OSError, UnicodeDecodeError):
            return False
        try:
            self._decode_state(text)
            return False
        except (ValueError, TypeError, KeyError, AttributeError):
            pass
        try:
            return _has_mature_arm(json.loads(text))
        except ValueError:
            return False

    def sample(
        self,
        arm_ids: list[str],
        seed: int | None = None,
        context: tuple[str, ...] | None = None,
    ) -> dict[str, float]:
        """Sample effectiveness scores for candidate arms.

        Args:
            arm_ids: Arm IDs to score.
            seed: Optional random seed for reproducibility.
            context: Optional context tuple for contextual bandit scoring.

        Returns:
            Dict mapping arm_id → sampled score [0, 1].
        """
        return self.state.sample_scores(arm_ids, seed, context=context)

    def update(
        self,
        active_arms: list[str],
        outcome: str | float,
        context: tuple[str, ...] | None = None,
        decay_rate: float | None = None,
        per_arm_rewards: dict[str, float] | None = None,
        per_arm_decay: bool = False,
        hook: UpdateHook | None = None,
    ) -> dict[str, bool]:
        """Update after a session and save state.

        Args:
            active_arms: Arm IDs that were active.
            outcome: Session outcome: 'productive'/1.0, 'noop'/'failed'/0.0,
                     'violated_policy'/0.0, or a graded float in [0, 1].
            context: Optional context tuple for contextual arms.
            decay_rate: If set, apply exponential decay before updating.
            per_arm_rewards: Optional per-arm rewards for salience-weighted credit.
            per_arm_decay: If True, ``decay_rate`` decays only ``active_arms``
                (global and every contextual sub-arm of each) instead of every arm.
                Use it when arms are only sometimes selected, so global decay would
                keep pulling the unselected ones back toward the prior.
            hook: Optional :class:`UpdateHook` notified before the mutation, after the
                save and after the lock is released (see its docstring).

        Raises:
            UpdateHookError: A post-save hook phase (``saved``/``complete``) failed.
                The posterior is already persisted; do **not** retry ``update()``
                (that would apply the reward twice). A ``prepare`` failure raises
                the hook's own exception and leaves the state untouched.
        """
        token: Any = None
        with self._state_lock():
            self.state = self._load_state()
            if hook is not None:
                token = hook.prepare(
                    UpdatePlan(
                        active_arms=tuple(active_arms),
                        outcome=outcome,
                        context=context,
                        decay_rate=decay_rate,
                        per_arm_decay=per_arm_decay,
                        per_arm_rewards=per_arm_rewards,
                        state_file=self.state_file,
                    ),
                    self.state,
                )
            if decay_rate is not None:
                if per_arm_decay:
                    self.state.apply_decay(decay_rate, only=set(active_arms))
                else:
                    self.state.apply_decay(decay_rate)
            result = self.state.update_session(
                active_arms, outcome, context=context, per_arm_rewards=per_arm_rewards
            )
            self.save()
            if hook is not None:
                try:
                    hook.saved(token)
                except Exception as exc:
                    raise UpdateHookError(
                        "post-save hook failed; the posterior update was already "
                        "persisted — do not retry update(), reconcile the ledger"
                    ) from exc
        if hook is not None:
            try:
                hook.complete(token)
            except Exception as exc:
                raise UpdateHookError(
                    "completion hook failed; the posterior update was already "
                    "persisted — do not retry update(), reconcile the ledger"
                ) from exc
        return result

    def decay(self, gamma: float) -> int:
        """Apply exponential decay to all arms and save.

        Args:
            gamma: Decay rate in (0, 1). Higher = slower decay.

        Returns:
            Number of arms decayed.
        """
        with self._state_lock():
            self.state = self._load_state()
            count = self.state.apply_decay(gamma)
            self.save()
        return count

    def prune(self, min_selections: int = 0, max_age_days: int = 90) -> int:
        """Remove stale arms and save.

        Args:
            min_selections: Prune arms with at most this many selections (total_selections <= min_selections).
            max_age_days: Prune arms not updated in this many days.

        Returns:
            Number of arms pruned.
        """
        with self._state_lock():
            self.state = self._load_state()
            count = self.state.prune_stale(min_selections=min_selections, max_age_days=max_age_days)
            # Pruning is the explicit, caller-requested way to delete arms.
            self.save(allow_arm_deletion=True)
        return count

    def status_report(self) -> str:
        """Generate human-readable status report."""
        lines = [
            "# Bandit Status",
            f"Total arms: {len(self.state.arms)}",
            f"Total sessions: {self.state.total_sessions}",
            f"State file: {self.state_file}",
            f"Created: {self.state.created}",
            f"Last updated: {self.state.last_updated or 'never'}",
            "",
        ]

        if not self.state.arms:
            lines.append("No arms yet.")
            return "\n".join(lines)

        ranked = self.state.rank_by_expected()
        lines.append("## Arms by Expected Value")
        lines.append(f"{'Arm':<40} {'E[p]':>6} {'α':>6} {'β':>6} {'N':>5} {'UCB':>6}")
        lines.append("-" * 72)
        for arm_id, mean_val in ranked:
            arm = self.state.arms[arm_id]
            short = arm_id[:38]
            lines.append(
                f"{short:<40} {mean_val:>5.3f} {arm.alpha:>6.1f} {arm.beta:>6.1f} "
                f"{arm.total_selections:>5} {arm.ucb:>5.3f}"
            )

        if self.state.contextual_arms:
            ctx_count = sum(len(v) for v in self.state.contextual_arms.values())
            lines.append(f"\nContextual arms: {ctx_count}")

        return "\n".join(lines)


def _resolve_mean_readonly(
    state: "BanditState", arm_id: str, context: tuple[str, ...] | None
) -> float:
    """Read-only version of arm resolution — never creates new arms.

    Returns the posterior mean for arm_id under context, or 0.5 (uninformative
    prior mean) if no matching arm exists.
    """
    # _resolve_arm_for_sampling never stores the Beta(1, 1) arm it returns for
    # unknown ids, so its mean (0.5) is the uninformative default here too.
    return state._resolve_arm_for_sampling(arm_id, context).mean


def load_bandit_means(
    state_dir: str | Path,
    state_file: str = "bandit-state.json",
    arm_ids: list[str] | None = None,
    context: tuple[str, ...] | None = None,
) -> dict[str, float]:
    """Load posterior means from a bandit state file.

    Convenience function for integrating Thompson posteriors into scoring systems.
    Returns {arm_id: mean} for all known arms or a specified subset.
    Unknown arms get 0.5 (uninformative prior mean).

    When context is provided, uses hierarchical fallback to find the best
    contextual arm for each arm_id. This function is read-only — it never
    mutates the bandit state.
    """
    bandit = Bandit(state_dir=state_dir, state_file=state_file)
    state = bandit.state
    means: dict[str, float] = {}
    if arm_ids:
        for arm_id in arm_ids:
            means[arm_id] = _resolve_mean_readonly(state, arm_id, context)
    else:
        for arm_id in state.arms:
            means[arm_id] = _resolve_mean_readonly(state, arm_id, context)
    return means


def _bar(value: float, width: int = 30, fill: str = "█", empty: str = "░") -> str:
    """Render a simple ASCII progress bar."""
    filled = int(value * width)
    return fill * filled + empty * (width - filled)


def dashboard(state_files: list[tuple[str, Path, str]]) -> str:
    """Generate a unified dashboard across multiple bandit state files.

    Args:
        state_files: List of (label, state_dir, state_file) tuples.

    Returns:
        Formatted dashboard string.
    """
    sections: list[str] = []
    sections.append("╔═══════════════════════════════════════════════════════════════╗")
    sections.append("║           Thompson Sampling Dashboard                        ║")
    sections.append("╚═══════════════════════════════════════════════════════════════╝")
    sections.append("")

    for label, state_dir, state_file in state_files:
        bandit = Bandit(state_dir=state_dir, state_file=state_file)
        state = bandit.state

        if not state.arms and not state.contextual_arms:
            sections.append(f"┌─ {label} ─── (no data)")
            sections.append("")
            continue

        total_obs = sum(a.total_selections for a in state.arms.values())
        sections.append(f"┌─ {label}")
        sections.append(
            f"│  Arms: {len(state.arms)}  |  "
            f"Sessions: {state.total_sessions}  |  "
            f"Observations: {total_obs}"
        )
        if state.last_updated:
            sections.append(f"│  Last updated: {state.last_updated[:19]}")
        sections.append("│")

        ranked = sorted(state.arms.items(), key=lambda x: x[1].mean, reverse=True)
        sections.append(f"│  {'Arm':<30} {'E[p]':>5}  {'Bar':<32} {'α':>5} {'β':>5} {'N':>4}")
        sections.append(f"│  {'─' * 30} {'─' * 5}  {'─' * 32} {'─' * 5} {'─' * 5} {'─' * 4}")
        for arm_id, arm in ranked:
            short = arm_id[:28]
            bar = _bar(arm.mean)
            sections.append(
                f"│  {short:<30} {arm.mean:>5.3f}  "
                f"{bar}  {arm.alpha:>5.1f} {arm.beta:>5.1f} {arm.total_selections:>4}"
            )

        if state.contextual_arms:
            ctx_count = sum(len(v) for v in state.contextual_arms.values())
            sections.append("│")
            sections.append(f"│  Contextual arms: {ctx_count}")
            for arm_id, contexts in state.contextual_arms.items():
                short_id = arm_id[:20]
                for ctx_key, ctx_arm in contexts.items():
                    ctx_label = ctx_key if ctx_key != "null" else "global"
                    sections.append(
                        f"│    {short_id:<20} × {ctx_label:<20} "
                        f"E[p]={ctx_arm.mean:.3f}  "
                        f"α={ctx_arm.alpha:.1f} β={ctx_arm.beta:.1f} "
                        f"N={ctx_arm.total_selections}"
                    )

        sections.append(f"└{'─' * 62}")
        sections.append("")

    return "\n".join(sections)


def discover_state_files(
    base_dirs: list[Path] | None = None,
) -> list[tuple[str, Path, str]]:
    """Discover all Thompson sampling state files.

    Returns list of (label, state_dir, state_file) tuples.
    """
    if base_dirs is None:
        base_dirs = [
            Path("state/bandit"),
            Path("state/lesson-thompson"),
            Path("state/thompson-control"),
        ]

    found: list[tuple[str, Path, str]] = []
    for base in base_dirs:
        if not base.exists():
            continue
        for f in sorted(base.glob("*.json")):
            label = f"{base.name}/{f.stem}"
            found.append((label, base, f.name))
    return found


@click.group("bandit")
@click.option("--state-dir", default=None, help="State directory (default: state/bandit/)")
@click.option(
    "--state-file", default="bandit-state.json", help="State filename (default: bandit-state.json)"
)
@click.pass_context
def bandit_cli(ctx: click.Context, state_dir: str | None, state_file: str) -> None:
    """Thompson Sampling Bandit."""
    ctx.ensure_object(dict)
    ctx.obj["state_dir"] = state_dir
    ctx.obj["state_file"] = state_file


@bandit_cli.command()
@click.pass_context
def status(ctx: click.Context) -> None:
    """Show bandit status report."""
    bandit = Bandit(state_dir=ctx.obj["state_dir"], state_file=ctx.obj["state_file"])
    print(bandit.status_report())


@bandit_cli.command(name="dashboard")
@click.option("--base-dirs", multiple=True, help="Base directories to scan")
def dashboard_cmd(base_dirs: tuple[str, ...]) -> None:
    """Unified dashboard across state files."""
    dirs = [Path(d) for d in base_dirs] if base_dirs else None
    state_files = discover_state_files(dirs)
    if not state_files:
        print("No Thompson sampling state files found.")
        return
    print(dashboard(state_files))


@bandit_cli.command()
@click.option("--arms", multiple=True, help="Arm IDs to sample (default: all known)")
@click.option("--seed", type=int, default=None, help="Random seed")
@click.option("--context", multiple=True, help="Context tuple, e.g. --context infrastructure opus")
@click.option(
    "--format",
    "output_format",
    type=click.Choice(["text", "json"]),
    default="text",
    help="Output format",
)
@click.pass_context
def sample(
    ctx: click.Context,
    arms: tuple[str, ...],
    seed: int | None,
    context: tuple[str, ...],
    output_format: str,
) -> None:
    """Sample scores for arms."""
    bandit = Bandit(state_dir=ctx.obj["state_dir"], state_file=ctx.obj["state_file"])
    arm_list = list(arms) or list(bandit.state.arms.keys())
    if not arm_list:
        print("No arms to sample. Run `update` first.", file=sys.stderr)
        raise click.exceptions.Exit(1)
    ctx_tuple = tuple(context) if context else None
    scores = bandit.sample(arm_list, seed=seed, context=ctx_tuple)
    if output_format == "json":
        print(json.dumps(scores, indent=2))
    else:
        print(f"{'Arm':<40} {'Score':>6}")
        print("-" * 48)
        for arm_id, score in sorted(scores.items(), key=lambda x: x[1], reverse=True):
            print(f"{arm_id[:38]:<40} {score:>5.3f}")


@bandit_cli.command()
@click.option(
    "--outcome",
    required=True,
    help="Session outcome: productive/noop/failed/violated_policy or a float in [0,1]",
)
@click.option("--arms", multiple=True, required=True, help="Active arm IDs")
@click.option("--context", multiple=True, help="Context tuple, e.g. --context infrastructure opus")
@click.option(
    "--decay-rate", type=float, default=None, help="Apply decay before updating (e.g. 0.99)"
)
@click.pass_context
def update(
    ctx: click.Context,
    outcome: str,
    arms: tuple[str, ...],
    context: tuple[str, ...],
    decay_rate: float | None,
) -> None:
    """Update after session."""
    bandit = Bandit(state_dir=ctx.obj["state_dir"], state_file=ctx.obj["state_file"])
    ctx_tuple = tuple(context) if context else None
    # Parse outcome: try float first, fall back to string
    parsed_outcome: str | float
    try:
        parsed_outcome = float(outcome)
        if not 0.0 <= parsed_outcome <= 1.0:
            print("Error: float outcome must be in [0, 1]", file=sys.stderr)
            raise click.exceptions.Exit(1)
    except ValueError:
        if outcome not in ("productive", "noop", "failed", "violated_policy"):
            print(
                "Error: outcome must be productive/noop/failed/violated_policy or a float in [0,1]",
                file=sys.stderr,
            )
            raise click.exceptions.Exit(1)
        parsed_outcome = outcome
    result = bandit.update(list(arms), parsed_outcome, context=ctx_tuple, decay_rate=decay_rate)
    for arm_id, reward in result.items():
        symbol = "+" if reward else "-"
        print(f"  [{symbol}] {arm_id}")
    ctx_str = f" context={list(ctx_tuple)}" if ctx_tuple else ""
    print(f"\nUpdated {len(result)} arms (outcome: {outcome}{ctx_str})")


@bandit_cli.command()
@click.option("--rate", type=float, default=0.99, help="Decay rate gamma (default: 0.99)")
@click.pass_context
def decay(ctx: click.Context, rate: float) -> None:
    """Apply exponential decay to all arms."""
    if not (0 < rate < 1):
        print("Error: --rate must be between 0 and 1", file=sys.stderr)
        raise click.exceptions.Exit(1)
    bandit = Bandit(state_dir=ctx.obj["state_dir"], state_file=ctx.obj["state_file"])
    count = bandit.decay(rate)
    print(f"Applied decay (gamma={rate}) to {count} arms")


@bandit_cli.command()
@click.option(
    "--min-selections",
    type=int,
    default=0,
    help="Prune arms with at most this many selections (default: 0)",
)
@click.option(
    "--max-age-days",
    type=int,
    default=90,
    help="Prune arms not updated in this many days (default: 90)",
)
@click.pass_context
def prune(ctx: click.Context, min_selections: int, max_age_days: int) -> None:
    """Remove stale arms from the bandit."""
    bandit = Bandit(state_dir=ctx.obj["state_dir"], state_file=ctx.obj["state_file"])
    count = bandit.prune(min_selections=min_selections, max_age_days=max_age_days)
    print(
        f"Pruned {count} stale arms (min_selections={min_selections}, max_age_days={max_age_days})"
    )


def main() -> int:
    """Backward-compatible entry point."""
    try:
        bandit_cli(standalone_mode=False)
        return 0
    except click.ClickException as e:
        e.show()
        return 1
    except click.exceptions.Exit as e:
        return e.exit_code
    except click.exceptions.Abort:
        return 1
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 0


if __name__ == "__main__":
    raise SystemExit(main())
