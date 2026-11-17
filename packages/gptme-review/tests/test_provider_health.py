"""Qualified routing slugs and reported names share provider-health identity."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from gptme_review import core

MODEL = "deepseek/deepseek-v4.1-flash-1017"
NOW = datetime(2026, 11, 17, 4, tzinfo=UTC)


def _failures(path: Path, names: list[str]) -> None:
    rows = [
        {
            "ts": (NOW - timedelta(seconds=i + 1)).isoformat(),
            "provider": name,
            "model": MODEL,
            "outcome": "timeout",
        }
        for name in names
        for i in range(core.PROVIDER_QUARANTINE_STRIKES)
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


@pytest.mark.parametrize(
    ("slug", "reported"),
    [("deepinfra/fp8", "DeepInfra"), ("parasail/fp8", "Parasail")],
)
def test_quarantined_display_name_defers_qualified_route(
    tmp_path: Path, slug: str, reported: str
) -> None:
    path = tmp_path / "usage.jsonl"
    _failures(path, [reported])
    ignored = core.quarantined_provider_names(MODEL, path=path, now=NOW)
    assert not core.primary_model_exhausted(MODEL, path=path, now=NOW)
    other = next(p for p in core.DEEPSEEK_V4_FLASH_1017_GATE_PROVIDERS if p != slug)
    assert core.prefer_healthy_provider_order([slug, other], ignored) == [other, slug]
    block, _ = core.build_provider_routing(MODEL, structured=True, ignore=ignored)
    assert block is not None
    assert block["order"] == [other, slug]
    assert block["only"] == list(core.DEEPSEEK_V4_FLASH_1017_GATE_PROVIDERS)


@pytest.mark.parametrize(
    "names",
    [["DeepInfra", "Parasail"], ["deepinfra/fp8", "parasail/fp8"]],
)
def test_all_qualified_hosts_exhausted(tmp_path: Path, names: list[str]) -> None:
    path = tmp_path / "usage.jsonl"
    _failures(path, names)
    assert core.primary_model_exhausted(MODEL, path=path, now=NOW)
    assert core._pin_blocks_reroute(MODEL, names[0], [names[1]])
    block, _ = core.build_provider_routing(MODEL, structured=True, ignore=names)
    assert block is not None
    assert "ignore" not in block  # Do not send an empty effective allowlist.


@pytest.mark.parametrize(
    ("failed", "recovered"),
    [
        ("deepinfra/fp8", "DeepInfra"),
        ("DeepInfra", "deepinfra/fp8"),
        ("parasail/fp8", "Parasail"),
        ("Parasail", "parasail/fp8"),
    ],
)
def test_success_clears_strikes_across_provider_spellings(
    tmp_path: Path, failed: str, recovered: str
) -> None:
    path = tmp_path / "usage.jsonl"
    _failures(path, [failed])
    assert core.provider_quarantine_state(path, now=NOW)
    with path.open("a") as handle:
        handle.write(
            json.dumps(
                {
                    "ts": NOW.isoformat(),
                    "provider": recovered,
                    "model": MODEL,
                    "outcome": "ok",
                }
            )
            + "\n"
        )
    assert core.provider_quarantine_state(path, now=NOW) == {}


def test_unknown_provider_variants_remain_distinct() -> None:
    assert core._fold_provider(" DeepInfra/FP8 ") == core._fold_provider("DeepInfra")
    assert core._fold_provider("other/fp8") != core._fold_provider("other")
    assert core._fold_provider("deepinfra/fp16") != core._fold_provider("deepinfra/fp8")
