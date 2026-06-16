"""PM dispatch model-routing bandit.

Replaces the static fast/slow model split (resolve_lane_model()) with a
Thompson-sampling bandit that learns per-work-type model assignments from
actual dispatch outcomes.

Each work type (strategy-reply, ci-fix, greptile-fix, etc.) has a
separate Beta-Bernoulli arm per available model. The bandit samples posterior
distributions at dispatch time and picks the highest-scoring model, naturally
balancing exploration and exploitation.

Usage:

    from gptme_runloops.pm_bandit import PmModelBandit

    bandit = PmModelBandit()
    model = bandit.resolve_model("ci-fix", ["haiku", "sonnet"])
    bandit.record_outcome("ci-fix", model, "productive")

State persists to state/pm-dispatch/bandit-state.json with atomic writes
and .bak recovery (mirrors LessonBandit persistence). record_outcome() holds
an exclusive fcntl lock during load+update+save to prevent lost updates when
multiple dispatch workers run concurrently.

Reference: https://github.com/gptme/gptme-contrib/pull/1075
"""

from __future__ import annotations

import fcntl
import json
import os
import random
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PM_WORK_TYPES: set[str] = {
    "strategy-reply",
    "ci-fix",
    "greptile-fix",
    "pr-review",
    "issue-triage",
    "merge-conflict",
    "assigned-issue",
    "notification-triage",
}


DEFAULT_STATE_DIR = "state/pm-dispatch"
DEFAULT_STATE_FILE = "bandit-state.json"


@dataclass
class BanditArm:
    """A single Beta-Bernoulli arm: one (work_type, model) pair."""

    alpha: float = 1.0
    beta: float = 1.0
    total_selections: int = 0

    @property
    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)

    def sample(self, rng: random.Random | None = None) -> float:
        if rng is not None:
            return rng.betavariate(self.alpha, self.beta)
        return random.betavariate(self.alpha, self.beta)

    def update(self, reward: float) -> None:
        self.alpha += reward
        self.beta += 1.0 - reward
        self.total_selections += 1


def _arm_id(work_type: str, model: str) -> str:
    return f"pm-model:{work_type}:{model}"


def _parse_arm_id(arm_id: str) -> tuple[str, str] | None:
    parts = arm_id.split(":", 2)
    if len(parts) == 3 and parts[0] == "pm-model":
        return parts[1], parts[2]
    return None


class PmModelBandit:
    """Thompson-sampling bandit for PM dispatch model routing."""

    def __init__(
        self,
        state_dir: str | Path | None = None,
        state_file: str = DEFAULT_STATE_FILE,
    ):
        self.state_dir = Path(
            state_dir or os.environ.get("PM_BANDIT_STATE_DIR") or DEFAULT_STATE_DIR
        )
        self.state_file = self.state_dir / state_file
        self.arms: dict[str, BanditArm] = {}
        self._load()

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
        """Record a dispatch outcome and update the posterior.

        Holds an exclusive fcntl lock during load+update+save so concurrent
        dispatch workers don't overwrite each other's posterior updates.
        """
        if isinstance(outcome, str):
            reward = 1.0 if outcome == "productive" else 0.0
        else:
            reward = max(0.0, min(1.0, float(outcome)))
        self.state_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.state_file.with_suffix(".json.lock")
        with open(lock_path, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            self._load()  # re-read under lock to pick up concurrent updates
            aid = _arm_id(work_type, model)
            if aid not in self.arms:
                self.arms[aid] = BanditArm()
            self.arms[aid].update(reward)
            self._save()

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
        for aid, arm in self.arms.items():
            parsed = _parse_arm_id(aid)
            if parsed and parsed[0] == work_type:
                models.add(parsed[1])
        return models

    def _load(self) -> None:
        candidates = [self.state_file, self.state_file.with_suffix(".json.bak")]
        for path in candidates:
            if not path.exists():
                continue
            try:
                data = json.loads(path.read_text())
                for aid, arm_data in data.get("arms", {}).items():
                    self.arms[aid] = BanditArm(**arm_data)
                return
            except (json.JSONDecodeError, TypeError, KeyError):
                continue

    def _save(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {
            "arms": {
                aid: {
                    "alpha": arm.alpha,
                    "beta": arm.beta,
                    "total_selections": arm.total_selections,
                }
                for aid, arm in self.arms.items()
            },
        }
        if self.state_file.exists():
            bak = self.state_file.with_suffix(".json.bak")
            try:
                shutil.copy2(self.state_file, bak)
            except OSError:
                pass
        fd, tmp_path = tempfile.mkstemp(
            dir=str(self.state_dir), suffix=".tmp", prefix="bandit-"
        )
        try:
            with open(fd, "w") as f:
                json.dump(data, f, indent=2)
                f.write("\n")
            Path(tmp_path).replace(self.state_file)
        except BaseException:
            Path(tmp_path).unlink(missing_ok=True)
            raise
