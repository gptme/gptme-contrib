from __future__ import annotations

from pathlib import Path

import pytest
from gptme_subscription.config import Config
from gptme_subscription.manager import SubscriptionManager


@pytest.fixture
def make_manager(tmp_path: Path):
    def _make(
        subscriptions=("bob", "alice", "erik"), primary=None, fallback=None, **kw
    ):
        creds_dir = tmp_path / "creds"
        creds_dir.mkdir(exist_ok=True)
        state_dir = tmp_path / "state"
        state_dir.mkdir(exist_ok=True)
        subs = list(subscriptions)
        config = Config(
            subscriptions=subs,
            primary=primary or subs[0],
            fallback_order=list(fallback)
            if fallback
            else [s for s in subs if s != (primary or subs[0])],
            creds_dir=creds_dir,
            state_dir=state_dir,
            usage_script=kw.pop("usage_script", None),
            **kw,
        )
        return SubscriptionManager(config)

    return _make
