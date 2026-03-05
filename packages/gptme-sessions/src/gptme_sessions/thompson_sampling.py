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

import argparse
import json
import random
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path


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

    def get_or_create_arm(self, arm_id: str) -> BanditArm:
        """Get existing arm or create new one with uniform prior."""
        if arm_id not in self.arms:
            self.arms[arm_id] = BanditArm(arm_id=arm_id)
        return self.arms[arm_id]

    def get_or_create_contextual_arm(
        self, arm_id: str, context: tuple[str, ...] | None
    ) -> BanditArm:
        """Get or create a contextual arm."""
        key = _context_key(context)
        if arm_id not in self.contextual_arms:
            self.contextual_arms[arm_id] = {}
        if key not in self.contextual_arms[arm_id]:
            self.contextual_arms[arm_id][key] = BanditArm(arm_id=arm_id)
        return self.contextual_arms[arm_id][key]

    def _resolve_arm_for_sampling(self, arm_id: str, context: tuple[str, ...] | None) -> BanditArm:
        """Find the best arm for sampling using hierarchical fallback."""
        if context is None:
            return self.get_or_create_arm(arm_id)

        for ctx in _context_fallback_chain(context):
            key = _context_key(ctx)
            if arm_id in self.contextual_arms and key in self.contextual_arms[arm_id]:
                arm = self.contextual_arms[arm_id][key]
                if ctx is None or arm.total_selections >= CONTEXTUAL_MIN_OBSERVATIONS:
                    return arm

        return self.get_or_create_arm(arm_id)

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
                - str: 'productive' (1.0), 'noop'/'failed' (0.0)
                - float in [0, 1]: Graded reward for nuanced learning.
            context: Optional context tuple. When provided, updates both
                     the contextual arm AND the global arm.
            per_arm_rewards: Optional per-arm rewards for salience-weighted
                credit assignment. Missing arms fall back to base reward.

        Returns:
            Dict mapping arm_id → bool (True if reward > 0.5).
        """
        if isinstance(outcome, str):
            base_reward = 1.0 if outcome == "productive" else 0.0
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

    def apply_decay(self, gamma: float) -> int:
        """Apply exponential decay to all arms (global + contextual).

        Args:
            gamma: Decay rate in (0, 1). Higher = slower decay.

        Returns:
            Number of arms decayed.
        """
        count = 0
        for arm in self.arms.values():
            arm.apply_decay(gamma)
            count += 1
        for contexts in self.contextual_arms.values():
            for arm in contexts.values():
                arm.apply_decay(gamma)
                count += 1
        return count

    def prune_stale(self, min_selections: int = 0, max_age_days: int = 90) -> int:
        """Remove arms that haven't been selected recently.

        Prunes both global arms and contextual arms.

        Args:
            min_selections: Only prune arms with fewer than this many selections.
            max_age_days: Prune arms not updated in this many days.

        Returns:
            Number of arms pruned.
        """
        now = datetime.now(timezone.utc)
        pruned = 0

        def _is_stale(arm: "BanditArm") -> bool:
            if arm.total_selections > min_selections:
                return False
            if arm.last_updated:
                last = datetime.fromisoformat(arm.last_updated)
                return (now - last).days > max_age_days
            return arm.total_selections == 0

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

    def __init__(
        self,
        state_dir: str | Path | None = None,
        state_file: str = DEFAULT_STATE_FILE,
    ):
        self.state_dir = Path(state_dir or self.DEFAULT_STATE_DIR)
        self.state_file = self.state_dir / state_file
        self.state = self._load_state()

    def _load_state(self) -> BanditState:
        """Load bandit state from disk."""
        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text())
                state = BanditState(
                    total_sessions=data.get("total_sessions", 0),
                    created=data.get("created", ""),
                    last_updated=data.get("last_updated", ""),
                )
                for arm_id, arm_data in data.get("arms", {}).items():
                    # Support old format where key was 'lesson_path'
                    arm_data = dict(arm_data)
                    if "lesson_path" in arm_data and "arm_id" not in arm_data:
                        arm_data["arm_id"] = arm_data.pop("lesson_path")
                    state.arms[arm_id] = BanditArm(**arm_data)
                for arm_id, contexts in data.get("contextual_arms", {}).items():
                    state.contextual_arms[arm_id] = {}
                    for ctx_key, arm_data in contexts.items():
                        arm_data = dict(arm_data)
                        if "lesson_path" in arm_data and "arm_id" not in arm_data:
                            arm_data["arm_id"] = arm_data.pop("lesson_path")
                        state.contextual_arms[arm_id][ctx_key] = BanditArm(**arm_data)
                return state
            except (json.JSONDecodeError, TypeError, KeyError) as e:
                print(f"Warning: corrupted state file, starting fresh: {e}")
        return BanditState()

    def save(self) -> None:
        """Persist bandit state to disk."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
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
        self.state_file.write_text(json.dumps(data, indent=2) + "\n")

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
    ) -> dict[str, bool]:
        """Update after a session and save state.

        Args:
            active_arms: Arm IDs that were active.
            outcome: Session outcome: 'productive'/1.0, 'noop'/'failed'/0.0,
                     or a graded float in [0, 1].
            context: Optional context tuple for contextual arms.
            decay_rate: If set, apply exponential decay before updating.
            per_arm_rewards: Optional per-arm rewards for salience-weighted credit.
        """
        if decay_rate is not None:
            self.state.apply_decay(decay_rate)
        result = self.state.update_session(
            active_arms, outcome, context=context, per_arm_rewards=per_arm_rewards
        )
        self.save()
        return result

    def decay(self, gamma: float) -> int:
        """Apply exponential decay to all arms and save.

        Args:
            gamma: Decay rate in (0, 1). Higher = slower decay.

        Returns:
            Number of arms decayed.
        """
        count = self.state.apply_decay(gamma)
        self.save()
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
    if context is not None:
        for ctx in _context_fallback_chain(context):
            key = _context_key(ctx)
            if arm_id in state.contextual_arms and key in state.contextual_arms[arm_id]:
                arm = state.contextual_arms[arm_id][key]
                if ctx is None or arm.total_selections >= CONTEXTUAL_MIN_OBSERVATIONS:
                    return arm.mean
    existing = state.arms.get(arm_id)
    return existing.mean if existing else 0.5


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


def main() -> None:
    """CLI for Thompson sampling bandit management."""
    parser = argparse.ArgumentParser(description="Thompson Sampling Bandit")
    parser.add_argument(
        "--state-dir",
        default=None,
        help="State directory (default: state/bandit/)",
    )
    parser.add_argument(
        "--state-file",
        default="bandit-state.json",
        help="State filename (default: bandit-state.json)",
    )
    subparsers = parser.add_subparsers(dest="command", help="Command")

    # status
    subparsers.add_parser("status", help="Show bandit status report")

    # dashboard
    dash_p = subparsers.add_parser("dashboard", help="Unified dashboard across state files")
    dash_p.add_argument(
        "--base-dirs",
        nargs="+",
        help="Base directories to scan",
    )

    # sample
    sample_p = subparsers.add_parser("sample", help="Sample scores for arms")
    sample_p.add_argument("--arms", nargs="+", help="Arm IDs to sample (default: all known)")
    sample_p.add_argument("--seed", type=int, help="Random seed")
    sample_p.add_argument(
        "--context", nargs="+", help="Context tuple, e.g. --context infrastructure opus"
    )
    sample_p.add_argument(
        "--format", choices=["text", "json"], default="text", help="Output format"
    )

    # update
    update_p = subparsers.add_parser("update", help="Update after session")
    update_p.add_argument(
        "--outcome",
        required=True,
        help="Session outcome: productive/noop/failed or a float in [0,1]",
    )
    update_p.add_argument("--arms", nargs="+", required=True, help="Active arm IDs")
    update_p.add_argument(
        "--context", nargs="+", help="Context tuple, e.g. --context infrastructure opus"
    )
    update_p.add_argument(
        "--decay-rate", type=float, default=None, help="Apply decay before updating (e.g. 0.99)"
    )

    # decay
    decay_p = subparsers.add_parser("decay", help="Apply exponential decay to all arms")
    decay_p.add_argument(
        "--rate", type=float, default=0.99, help="Decay rate gamma (default: 0.99)"
    )

    args = parser.parse_args()

    if args.command == "dashboard":
        base_dirs = [Path(d) for d in args.base_dirs] if args.base_dirs else None
        state_files = discover_state_files(base_dirs)
        if not state_files:
            print("No Thompson sampling state files found.")
            sys.exit(0)
        print(dashboard(state_files))
        sys.exit(0)

    bandit = Bandit(state_dir=args.state_dir, state_file=args.state_file)

    if args.command == "status":
        print(bandit.status_report())

    elif args.command == "sample":
        arms = args.arms or list(bandit.state.arms.keys())
        if not arms:
            print("No arms to sample. Run `update` first.", file=sys.stderr)
            sys.exit(1)
        ctx = tuple(args.context) if args.context else None
        scores = bandit.sample(arms, seed=args.seed, context=ctx)
        if args.format == "json":
            print(json.dumps(scores, indent=2))
        else:
            print(f"{'Arm':<40} {'Score':>6}")
            print("-" * 48)
            for arm_id, score in sorted(scores.items(), key=lambda x: x[1], reverse=True):
                print(f"{arm_id[:38]:<40} {score:>5.3f}")

    elif args.command == "update":
        ctx = tuple(args.context) if args.context else None
        # Parse outcome: try float first, fall back to string
        outcome: str | float
        try:
            outcome = float(args.outcome)
            if not 0.0 <= outcome <= 1.0:
                print("Error: float outcome must be in [0, 1]", file=sys.stderr)
                sys.exit(1)
        except ValueError:
            if args.outcome not in ("productive", "noop", "failed"):
                print(
                    "Error: outcome must be productive/noop/failed or a float in [0,1]",
                    file=sys.stderr,
                )
                sys.exit(1)
            outcome = args.outcome
        result = bandit.update(args.arms, outcome, context=ctx, decay_rate=args.decay_rate)
        for arm_id, reward in result.items():
            symbol = "+" if reward else "-"
            print(f"  [{symbol}] {arm_id}")
        ctx_str = f" context={list(ctx)}" if ctx else ""
        print(f"\nUpdated {len(result)} arms (outcome: {args.outcome}{ctx_str})")

    elif args.command == "decay":
        if not (0 < args.rate < 1):
            print("Error: --rate must be between 0 and 1", file=sys.stderr)
            sys.exit(1)
        count = bandit.decay(args.rate)
        print(f"Applied decay (gamma={args.rate}) to {count} arms")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
