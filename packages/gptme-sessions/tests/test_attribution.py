from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from gptme_sessions import SessionRecord
from gptme_sessions import attribution


def write_jsonl(path: Path, *rows: object) -> None:
    path.write_text(
        "\n".join(json.dumps(row) if isinstance(row, dict) else str(row) for row in rows) + "\n",
        encoding="utf-8",
    )


def test_load_session_records_counts_malformed_jsonl(tmp_path: Path) -> None:
    records = tmp_path / "session-records.jsonl"
    write_jsonl(
        records,
        {
            "session_id": "good",
            "timestamp": "2026-09-05T10:00:00+00:00",
            "harness": "gptme",
            "model": "openrouter/deepseek/deepseek-v4-flash@deepseek",
            "input_tokens": 100,
            "output_tokens": 20,
        },
        "{not-json",
    )

    loaded = attribution.load_session_records(records)

    assert loaded.malformed_rows == 1
    assert [source.record.session_id for source in loaded.records] == ["good"]


def test_missing_token_and_cost_data_is_unknown_not_billable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = tmp_path / "session-records.jsonl"
    write_jsonl(
        records,
        {
            "session_id": "missing-cost",
            "timestamp": "2026-09-05T11:00:00+00:00",
            "harness": "gptme",
            "model": "unknown-model",
        },
    )
    monkeypatch.setattr(attribution, "estimate_record_cost", lambda _record: None)

    report = attribution.build_report(records_path=records, tenant_map_path=None)

    row = report.records[0]
    assert row["cost"] == {
        "amount_usd": None,
        "basis": "unknown",
        "pricing_status": "unknown",
        "pricing_source": "gptme_sessions.cost.estimate_record_cost",
    }
    assert row["billing"]["billable_status"] == "unknown"
    assert row["coverage"] == {
        "status": "partial",
        "missing_fields": [
            "cost.amount_usd",
            "usage.token_data",
            "tenant.org_id",
            "tenant.project_id",
            "billing.billable_status",
        ],
    }
    assert report.coverage["unpriced"] == 1
    assert report.coverage["billable_unknown"] == 1


def test_subscription_backed_cost_is_labeled_subscription_equivalent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = tmp_path / "session-records.jsonl"
    write_jsonl(
        records,
        {
            "session_id": "codex-sub",
            "timestamp": "2026-09-05T12:00:00+00:00",
            "harness": "codex",
            "model": "gpt-5.5",
            "input_tokens": 1_000,
            "output_tokens": 100,
        },
    )
    monkeypatch.setattr(attribution, "estimate_record_cost", lambda _record: 1.25)

    report = attribution.build_report(records_path=records, tenant_map_path=None)

    assert report.records[0]["cost"]["amount_usd"] == "1.25"
    assert report.records[0]["cost"]["pricing_status"] == "estimated"
    assert report.records[0]["cost"]["basis"] == "subscription_equivalent"


def test_tenant_map_resolution_and_explicit_metadata_precedence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_path = str(tmp_path / "customer-project")
    records = tmp_path / "session-records.jsonl"
    write_jsonl(
        records,
        {
            "session_id": "mapped",
            "timestamp": "2026-09-05T13:00:00+00:00",
            "project": project_path,
            "harness": "gptme",
            "model": "openrouter/deepseek/deepseek-v4-flash@deepseek",
            "provider": "openrouter",
            "category": "code",
            "input_tokens": 1_000,
            "output_tokens": 100,
            "tenant": {"org_id": "explicit-org"},
        },
    )
    tenant_map = tmp_path / "tenants.yaml"
    tenant_map.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "defaults": {
                    "billing": {
                        "billable_status": "non_billable",
                        "policy_id": "default-policy",
                    }
                },
                "mappings": [
                    {
                        "name": "customer",
                        "match": {"project": project_path, "harness": "gptme"},
                        "tenant": {
                            "org_id": "mapped-org",
                            "user_id": "erik",
                            "project_id": "gptme-ai",
                            "environment": "prod",
                        },
                        "billing": {
                            "billable_status": "promotional",
                            "policy_id": "promo-v1",
                        },
                        "work": {"feature": "managed-agent-session"},
                    }
                ],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(attribution, "estimate_record_cost", lambda _record: 2.5)

    report = attribution.build_report(records_path=records, tenant_map_path=tenant_map)

    row = report.records[0]
    assert row["tenant"] == {
        "org_id": "explicit-org",
        "user_id": "erik",
        "project_id": "gptme-ai",
        "environment": "prod",
    }
    assert row["billing"]["billable_status"] == "promotional"
    assert row["billing"]["policy_id"] == "promo-v1"
    assert row["billing"]["invoice_group"] == "2026-09"
    assert row["work"]["feature"] == "managed-agent-session"
    assert row["coverage"]["status"] == "complete"
    assert report.coverage["tenant_known"] == 1
    assert report.coverage["billable_known"] == 1


def test_build_attribution_record_accepts_session_record_directly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record = SessionRecord.from_dict(
        {
            "session_id": "direct",
            "timestamp": "2026-09-05T14:00:00+00:00",
            "harness": "gptme",
            "model": "openrouter/deepseek/deepseek-v4-flash@deepseek",
            "input_tokens": 100,
            "output_tokens": 20,
            "tenant": {"org_id": "org", "project_id": "project"},
            "billing": {"billable_status": "billable"},
        }
    )
    monkeypatch.setattr(attribution, "estimate_record_cost", lambda _record: 0.5)

    row = attribution.build_attribution_record(record)

    assert row["session_id"] == "direct"
    assert row["tenant"]["org_id"] == "org"
    assert row["cost"]["amount_usd"] == "0.5"
    assert row["coverage"]["status"] == "complete"


def test_render_jsonl_and_markdown_exports(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = tmp_path / "session-records.jsonl"
    write_jsonl(
        records,
        {
            "session_id": "exported",
            "timestamp": "2026-09-05T14:00:00+00:00",
            "harness": "gptme",
            "model": "openrouter/deepseek/deepseek-v4-flash@deepseek",
            "input_tokens": 1_000,
            "output_tokens": 100,
        },
        "{bad",
    )
    monkeypatch.setattr(attribution, "estimate_record_cost", lambda _record: 0.75)

    report = attribution.build_report(
        records_path=records,
        tenant_map_path=tmp_path / "missing.yaml",
    )
    jsonl_out = tmp_path / "derived.jsonl"
    markdown_out = tmp_path / "summary.md"
    attribution.write_jsonl(jsonl_out, report.records)
    attribution.write_markdown(markdown_out, report)

    exported = [json.loads(line) for line in jsonl_out.read_text().splitlines()]
    assert exported[0]["session_id"] == "exported"
    assert "# Cost Attribution Report" in attribution.render_markdown(report)
    assert "Subscription equivalent is opportunity cost" in markdown_out.read_text()
