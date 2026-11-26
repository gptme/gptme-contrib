"""PM dispatch model-routing adapter over the shared Thompson-sampling engine.

Each work type (strategy-reply, ci-fix, greptile-fix, etc.) has a separate
Beta-Bernoulli arm per available model. Consumers use resolve_model() to sample
and record_outcome() to learn from dispatch outcomes.

State remains at state/pm-dispatch/bandit-state.json in the legacy PM format.
The shared engine owns locking, reload-under-lock, atomic writes, valid-backup
recovery and mature-arm protection. No decay is enabled by this adapter: this
refactor preserves the existing PM reward and routing policy.

Reference: https://github.com/gptme/gptme-contrib/pull/1075
"""

from __future__ import annotations

import json
import os
import random
from pathlib import Path

from gptme_sessions.thompson_sampling import Bandit, BanditState
from gptme_sessions.thompson_sampling import BanditArm as SharedBanditArm

PM_WORK_TYPES: set[str] = {
    "strategy-reply",
    "ci-fix",
    "greptile-fix",
    "greptile-convergence",
    "pr-review",
    "automated-pr-review",
    "merge-conflict",
    "assigned-issue",
    "notification-triage",
}


DEFAULT_STATE_DIR = "state/pm-dispatch"
DEFAULT_STATE_FILE = "bandit-state.json"


class BanditArm(SharedBanditArm):
    """Keep the PM constructor API while sharing posterior operations."""

    def __init__(
        self,
        alpha: float = 1.0,
        beta: float = 1.0,
        total_selections: int = 0,
        *,
        arm_id: str = "",
        total_rewards: int = 0,
        last_updated: str = "",
    ):
        super().__init__(
            arm_id=arm_id,
            alpha=alpha,
            beta=beta,
            total_selections=total_selections,
            total_rewards=total_rewards,
            last_updated=last_updated,
        )


class _PmState(BanditState):
    arm_class = BanditArm


def _arm_id(work_type: str, model: str) -> str:
    return f"pm-model:{work_type}:{model}"


def _parse_arm_id(arm_id: str) -> tuple[str, str] | None:
    parts = arm_id.split(":", 2)
    if len(parts) == 3 and parts[0] == "pm-model":
        return parts[1], parts[2]
    return None


class PmModelBandit(Bandit):
    """PM naming, rewards and wire-format compatibility for the shared engine.

    Use resolve_model(), record_outcome() and summary() for PM routing. The
    legacy PM document persists only posterior parameters and selection counts;
    inherited contextual/session-reporting APIs are not a PM persistence API.
    """

    state_class = _PmState

    def __init__(
        self,
        state_dir: str | Path | None = None,
        state_file: str = DEFAULT_STATE_FILE,
    ):
        super().__init__(
            state_dir=state_dir
            or os.environ.get("PM_BANDIT_STATE_DIR")
            or DEFAULT_STATE_DIR,
            state_file=state_file,
        )

    @property
    def lock_file(self) -> Path:
        return self.state_file.with_suffix(".json.lock")

    @property
    def backup_file(self) -> Path:
        return self.state_file.with_suffix(".json.bak")

    @property
    def arms(self) -> dict[str, SharedBanditArm]:
        """Expose the current state even after the shared engine reloads it."""
        return self.state.arms

    @classmethod
    def _decode_state(cls, text: str) -> BanditState:
        data = json.loads(text)
        for aid, arm in data.get("arms", {}).items():
            # Old PM files identify arms only by the enclosing key.
            arm.setdefault("arm_id", aid)
        return super()._decode_state(json.dumps(data))

    def _serialize_state(self) -> dict:
        # Retain the old wire format so an already-running old worker can still
        # load a new worker's update. Both versions use bandit-state.json.lock.
        return {
            "arms": {
                aid: {
                    "alpha": arm.alpha,
                    "beta": arm.beta,
                    "total_selections": arm.total_selections,
                }
                for aid, arm in self.arms.items()
            },
        }

    def _load(self) -> None:
        """Compatibility alias for existing PM callers."""
        self.state = self._load_state()

    def _save(self) -> None:
        """Compatibility alias; persisted mutations should use record_outcome()."""
        self.save()

    def resolve_model(
        self,
        work_type: str,
        available_models: list[str] | None = None,
        rng: random.Random | None = None,
    ) -> str:
        """Select a model for work_type via Thompson sampling."""
        if available_models is None:
            known = self._known_models(work_type)
            available_models = list(known) if known else ["sonnet"]
        if not available_models:
            return "sonnet"
        if len(available_models) == 1:
            return available_models[0]
        samples = {}
        for model in available_models:
            arm = self.arms.get(_arm_id(work_type, model))
            if arm is None:
                arm = BanditArm()
            samples[model] = arm.sample(rng)
        return max(samples, key=samples.__getitem__)

    def record_outcome(
        self,
        work_type: str,
        model: str,
        outcome: str | float,
    ) -> None:
        """Update under the shared engine's exclusive reload/update/save lock."""
        if isinstance(outcome, str):
            reward = 1.0 if outcome == "productive" else 0.0
        else:
            reward = max(0.0, min(1.0, float(outcome)))
        self.update([_arm_id(work_type, model)], reward)

    def known_work_types(self) -> set[str]:
        types: set[str] = set()
        for aid, arm in self.arms.items():
            parsed = _parse_arm_id(aid)
            if parsed and arm.total_selections > 0:
                types.add(parsed[0])
        return types

    def summary(self) -> dict[str, dict[str, dict[str, float | int]]]:
        result: dict[str, dict[str, dict[str, float | int]]] = {}
        for aid, arm in self.arms.items():
            parsed = _parse_arm_id(aid)
            if parsed:
                wt, model = parsed
                if wt not in result:
                    result[wt] = {}
                result[wt][model] = {
                    "alpha": round(arm.alpha, 2),
                    "beta": round(arm.beta, 2),
                    "mean": round(arm.mean, 3),
                    "selections": arm.total_selections,
                }
        return result

    def _known_models(self, work_type: str) -> set[str]:
        models: set[str] = set()
        for aid in self.arms:
            parsed = _parse_arm_id(aid)
            if parsed and parsed[0] == work_type:
                models.add(parsed[1])
        return models
