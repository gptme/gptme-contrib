"""Coverage-aware cost attribution over canonical gptme session records."""

from __future__ import annotations

import json
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Mapping, Sequence, cast

import yaml
from .cost import estimate_record_cost
from .record import SessionRecord


DEFAULT_RECORDS = Path("state/sessions/session-records.jsonl")
DEFAULT_TENANT_MAP = Path("config/cost-attribution-tenants.yaml")

SCHEMA_VERSION = 1
EXTRACTOR_VERSION = "agent-cost-attribution-v1"
PRICING_SOURCE = "gptme_sessions.cost.estimate_record_cost"

CostBasis = Literal[
    "metered_cash",
    "metered_equivalent",
    "subscription_equivalent",
    "unknown",
]
PricingStatus = Literal["reported", "estimated", "unknown"]

TENANT_FIELDS = ("org_id", "user_id", "project_id", "environment")
BILLING_STATUSES = {"billable", "non_billable", "promotional", "unknown"}


@dataclass(frozen=True)
class SourceSessionRecord:
    record: SessionRecord


@dataclass(frozen=True)
class LoadedRecords:
    records: list[SourceSessionRecord]
    malformed_rows: int


@dataclass(frozen=True)
class TenantRule:
    name: str
    match: dict[str, Any]
    tenant: dict[str, Any]
    billing: dict[str, Any]
    work: dict[str, Any]


@dataclass(frozen=True)
class TenantConfig:
    defaults: dict[str, dict[str, Any]]
    rules: list[TenantRule]


@dataclass(frozen=True)
class CostAttributionReport:
    records: list[dict[str, Any]]
    malformed_rows: int
    source_path: Path
    tenant_map_path: Path | None
    since: date | None
    until: date | None

    @property
    def coverage(self) -> dict[str, int]:
        total = len(self.records)
        priced = sum(1 for row in self.records if row["cost"]["pricing_status"] != "unknown")
        tenant_known = sum(1 for row in self.records if tenant_is_known(row))
        billable_known = sum(
            1 for row in self.records if row["billing"]["billable_status"] != "unknown"
        )
        return {
            "records": total,
            "malformed_rows": self.malformed_rows,
            "priced": priced,
            "unpriced": total - priced,
            "tenant_known": tenant_known,
            "tenant_unknown": total - tenant_known,
            "billable_known": billable_known,
            "billable_unknown": total - billable_known,
        }


def _parse_record_datetime(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _record_date(record: SessionRecord) -> date | None:
    parsed = _parse_record_datetime(record.timestamp or record.start_time)
    return parsed.date() if parsed is not None else None


def _in_date_window(
    record: SessionRecord,
    *,
    since: date | None,
    until: date | None,
) -> bool:
    if since is None and until is None:
        return True
    observed = _record_date(record)
    if observed is None:
        return False
    if since is not None and observed < since:
        return False
    if until is not None and observed > until:
        return False
    return True


def _load_jsonl_payloads(path: Path) -> tuple[list[dict[str, Any]], int]:
    payloads: list[dict[str, Any]] = []
    malformed_rows = 0
    with path.open(encoding="utf-8") as handle:
        for raw in handle:
            raw = raw.strip()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                malformed_rows += 1
                continue
            if isinstance(payload, dict):
                payloads.append(payload)
            else:
                malformed_rows += 1
    return payloads, malformed_rows


def load_session_records(
    path: Path,
    *,
    since: date | None = None,
    until: date | None = None,
) -> LoadedRecords:
    """Load SessionRecord objects from JSONL and count skipped malformed rows."""
    records: list[SourceSessionRecord] = []
    if not path.exists():
        return LoadedRecords(records=[], malformed_rows=0)

    payloads, malformed_rows = _load_jsonl_payloads(path)
    for payload in payloads:
        try:
            record = SessionRecord.from_dict(payload)
        except (TypeError, AttributeError, ValueError):
            malformed_rows += 1
            continue
        if _in_date_window(record, since=since, until=until):
            records.append(SourceSessionRecord(record=record))
    return LoadedRecords(records=records, malformed_rows=malformed_rows)


def load_tenant_config(path: Path | None) -> TenantConfig:
    """Load deterministic tenant/project attribution rules from YAML."""
    if path is None or not path.exists():
        return TenantConfig(defaults={}, rules=[])
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("tenant map root must be a mapping")

    defaults = _section_groups(cast("Mapping[str, Any]", raw.get("defaults") or {}))
    rules: list[TenantRule] = []
    raw_mappings = raw.get("mappings") or []
    if not isinstance(raw_mappings, list):
        raise ValueError("tenant map 'mappings' must be a list")
    for index, item in enumerate(raw_mappings, start=1):
        if not isinstance(item, dict):
            raise ValueError(f"tenant map rule {index} must be a mapping")
        match = item.get("match") or {}
        if not isinstance(match, dict):
            raise ValueError(f"tenant map rule {index} match must be a mapping")
        rules.append(
            TenantRule(
                name=str(item.get("name") or f"rule-{index}"),
                match=dict(match),
                tenant=_section_dict(item.get("tenant")),
                billing=_section_dict(item.get("billing")),
                work=_section_dict(item.get("work")),
            )
        )
    return TenantConfig(defaults=defaults, rules=rules)


def _section_groups(raw: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for name in ("tenant", "billing", "work"):
        groups[name] = _section_dict(raw.get(name))
    return groups


def _section_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _nested_get(payload: Mapping[str, Any], dotted_key: str) -> Any:
    current: Any = payload
    for part in dotted_key.split("."):
        if not isinstance(current, Mapping):
            return None
        current = current.get(part)
    return current


def _task_id(record: SessionRecord, payload: Mapping[str, Any]) -> str | None:
    intent = record.cascade_intent
    if isinstance(intent, Mapping):
        task_id = _text_or_none(intent.get("task_id"))
        if task_id:
            return task_id
    return _text_or_none(payload.get("task_id"))


def _match_lookup(
    record: SessionRecord,
    payload: Mapping[str, Any],
    key: str,
) -> Any:
    if key == "model_normalized":
        return record.model_normalized
    if key == "task_id":
        return _task_id(record, payload)
    if key == "environment":
        return _nested_get(payload, "tenant.environment") or payload.get("environment")
    return _nested_get(payload, key)


def _match_value(observed: Any, expected: Any) -> bool:
    if isinstance(expected, list):
        return any(_match_value(observed, item) for item in expected)
    if expected == "*":
        return observed not in (None, "")
    if isinstance(expected, str):
        expected = os.path.expandvars(expected)
    return str(observed) == str(expected)


def _rule_matches(
    rule: TenantRule,
    record: SessionRecord,
    payload: Mapping[str, Any],
) -> bool:
    return all(
        _match_value(_match_lookup(record, payload, key), expected)
        for key, expected in rule.match.items()
    )


def _merge_section(
    current: dict[str, Any],
    incoming: Mapping[str, Any],
    allowed_fields: Sequence[str],
) -> None:
    for field in allowed_fields:
        value = incoming.get(field)
        if _text_or_none(value) is not None:
            current[field] = value


def _explicit_tenant(payload: Mapping[str, Any]) -> dict[str, Any]:
    tenant = _section_dict(payload.get("tenant"))
    for field in TENANT_FIELDS:
        for key in (f"tenant_{field}", field):
            value = payload.get(key)
            if _text_or_none(value) is not None:
                tenant[field] = value
                break
    return tenant


def _explicit_billing(payload: Mapping[str, Any]) -> dict[str, Any]:
    billing = _section_dict(payload.get("billing"))
    for field in ("billable_status", "policy_id", "line_item_type", "invoice_group"):
        for key in (f"billing_{field}", field):
            value = payload.get(key)
            if _text_or_none(value) is not None:
                billing[field] = value
                break
    return billing


def _explicit_work(payload: Mapping[str, Any]) -> dict[str, Any]:
    work = _section_dict(payload.get("work"))
    for field in ("feature",):
        for key in (f"work_{field}", field):
            value = payload.get(key)
            if _text_or_none(value) is not None:
                work[field] = value
                break
    return work


def _invoice_group(record: SessionRecord) -> str:
    observed = _record_date(record)
    if observed is None:
        return "unknown"
    return f"{observed.year:04d}-{observed.month:02d}"


def resolve_attribution(
    record: SessionRecord,
    config: TenantConfig,
) -> tuple[dict[str, str], dict[str, str | None], dict[str, str]]:
    """Resolve tenant, billing, and work metadata.

    Precedence is defaults -> first matching rule -> explicit session metadata.
    The explicit-session layer is last because product telemetry should beat
    Bob-local fallback configuration.
    """
    payload = cast("dict[str, Any]", record.to_dict())
    tenant: dict[str, Any] = {}
    billing: dict[str, Any] = {}
    work: dict[str, Any] = {}

    _merge_section(tenant, config.defaults.get("tenant", {}), TENANT_FIELDS)
    _merge_section(
        billing,
        config.defaults.get("billing", {}),
        ("billable_status", "policy_id", "line_item_type", "invoice_group"),
    )
    _merge_section(work, config.defaults.get("work", {}), ("feature",))

    for rule in config.rules:
        if _rule_matches(rule, record, payload):
            _merge_section(tenant, rule.tenant, TENANT_FIELDS)
            _merge_section(
                billing,
                rule.billing,
                ("billable_status", "policy_id", "line_item_type", "invoice_group"),
            )
            _merge_section(work, rule.work, ("feature",))
            break

    _merge_section(tenant, _explicit_tenant(payload), TENANT_FIELDS)
    _merge_section(
        billing,
        _explicit_billing(payload),
        ("billable_status", "policy_id", "line_item_type", "invoice_group"),
    )
    _merge_section(work, _explicit_work(payload), ("feature",))

    billable_status = _text_or_none(billing.get("billable_status")) or "unknown"
    if billable_status not in BILLING_STATUSES:
        billable_status = "unknown"

    return (
        {field: _text_or_none(tenant.get(field)) or "unknown" for field in TENANT_FIELDS},
        {
            "billable_status": billable_status,
            "policy_id": _text_or_none(billing.get("policy_id")),
            "line_item_type": _text_or_none(billing.get("line_item_type")) or "llm_usage",
            "invoice_group": _text_or_none(billing.get("invoice_group")) or _invoice_group(record),
        },
        {"feature": _text_or_none(work.get("feature")) or "unknown"},
    )


def _has_usage(record: SessionRecord) -> bool:
    return any(
        value is not None
        for value in (
            record.input_tokens,
            record.output_tokens,
            record.cache_creation_tokens,
            record.cache_read_tokens,
            record.token_count,
        )
    )


def _is_subscription_equivalent(record: SessionRecord) -> bool:
    try:
        from gptme_usage.harness_models import (
            SUBSCRIPTION_BACKED_MODELS,
            load_quota_config,
            pricing_key_for_model,
        )
    except ImportError:
        model = record.model or ""
        provider = record.provider or ""
        harness = record.harness or ""
        return harness in {"claude-code", "codex", "copilot-cli", "grok-build"} or (
            "subscription" in model or "subscription" in provider
        )

    harness = record.harness or ""
    model = record.model or ""
    provider = record.provider or ""
    config = load_quota_config()
    candidates = {
        pricing_key_for_model(harness, model, config),
        (harness, model.rsplit("/", 1)[-1]),
        (harness, model),
    }
    return bool(
        candidates & SUBSCRIPTION_BACKED_MODELS
        or "subscription" in model
        or "subscription" in provider
    )


def _cost_basis(record: SessionRecord, pricing_status: PricingStatus) -> CostBasis:
    if pricing_status == "unknown":
        return "unknown"
    if _is_subscription_equivalent(record):
        return "subscription_equivalent"
    if record.cost_usd is not None:
        return "metered_cash"
    return "metered_equivalent"


def _decimal_text(value: float | Decimal) -> str:
    return format(Decimal(str(value)), "f")


def _display_path(path: Path) -> str:
    return str(path)


def build_attribution_record(
    source: SourceSessionRecord | SessionRecord,
    *,
    source_path: Path | None = None,
    config: TenantConfig | None = None,
) -> dict[str, Any]:
    record = source.record if isinstance(source, SourceSessionRecord) else source
    source_path = source_path or DEFAULT_RECORDS
    config = config or TenantConfig(defaults={}, rules=[])
    payload = cast("dict[str, Any]", record.to_dict())
    tenant, billing, work_overrides = resolve_attribution(record, config)

    amount = estimate_record_cost(record)
    pricing_status: PricingStatus
    if amount is None:
        pricing_status = "unknown"
    elif record.cost_usd is not None:
        pricing_status = "reported"
    else:
        pricing_status = "estimated"
    basis = _cost_basis(record, pricing_status)

    missing_fields: list[str] = []
    if amount is None:
        missing_fields.append("cost.amount_usd")
        if not _has_usage(record):
            missing_fields.append("usage.token_data")
    if tenant["org_id"] == "unknown":
        missing_fields.append("tenant.org_id")
    if tenant["project_id"] == "unknown":
        missing_fields.append("tenant.project_id")
    if billing["billable_status"] == "unknown":
        missing_fields.append("billing.billable_status")

    return {
        "schema_version": SCHEMA_VERSION,
        "session_id": record.session_id,
        "source": {
            "record_path": _display_path(source_path),
            "record_timestamp": record.timestamp,
            "extractor_version": EXTRACTOR_VERSION,
        },
        "tenant": tenant,
        "work": {
            "session_id": record.session_id,
            "parent_session_id": record.parent_session_id,
            "task_id": _task_id(record, payload),
            "category": record.category
            or record.recommended_category
            or record.run_type
            or "unknown",
            "feature": work_overrides["feature"],
            "intent_source": _text_or_none(payload.get("intent_source"))
            or record.trigger
            or record.run_type
            or "unknown",
        },
        "usage": {
            "harness": record.harness or "unknown",
            "provider": record.provider or "unknown",
            "model": record.model_normalized or record.model or "unknown",
            "model_raw": record.model or "unknown",
            "input_tokens": record.input_tokens,
            "output_tokens": record.output_tokens,
            "cache_creation_tokens": record.cache_creation_tokens,
            "cache_read_tokens": record.cache_read_tokens,
            "token_count": record.token_count,
            "duration_seconds": record.duration_seconds,
        },
        "cost": {
            "amount_usd": _decimal_text(amount) if amount is not None else None,
            "basis": basis,
            "pricing_status": pricing_status,
            "pricing_source": PRICING_SOURCE,
        },
        "billing": billing,
        "coverage": {
            "status": "complete" if not missing_fields else "partial",
            "missing_fields": missing_fields,
        },
    }


def build_report(
    *,
    records_path: Path = DEFAULT_RECORDS,
    tenant_map_path: Path | None = None,
    since: date | None = None,
    until: date | None = None,
) -> CostAttributionReport:
    tenant_map_path = DEFAULT_TENANT_MAP if tenant_map_path is None else tenant_map_path
    config = load_tenant_config(tenant_map_path)
    loaded = load_session_records(records_path, since=since, until=until)
    records = [
        build_attribution_record(source, source_path=records_path, config=config)
        for source in loaded.records
    ]
    return CostAttributionReport(
        records=records,
        malformed_rows=loaded.malformed_rows,
        source_path=records_path,
        tenant_map_path=tenant_map_path if tenant_map_path and tenant_map_path.exists() else None,
        since=since,
        until=until,
    )


def tenant_is_known(row: Mapping[str, Any]) -> bool:
    tenant = cast("Mapping[str, Any]", row["tenant"])
    return tenant.get("org_id") != "unknown" and tenant.get("project_id") != "unknown"


def _amount(row: Mapping[str, Any]) -> Decimal:
    cost = cast("Mapping[str, Any]", row["cost"])
    raw = cost.get("amount_usd")
    return Decimal(str(raw)) if raw is not None else Decimal("0")


def _sum_amount(rows: Sequence[Mapping[str, Any]]) -> Decimal:
    total = Decimal("0")
    for row in rows:
        total += _amount(row)
    return total


def _money(value: Decimal) -> str:
    return f"${value:.4f}"


def _group_value(row: Mapping[str, Any], dotted_key: str) -> str:
    value = _nested_get(row, dotted_key)
    return _text_or_none(value) or "unknown"


def _group_rows(
    records: Sequence[Mapping[str, Any]],
    dotted_key: str,
) -> list[tuple[str, int, int, Decimal]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in records:
        groups[_group_value(row, dotted_key)].append(row)
    result: list[tuple[str, int, int, Decimal]] = []
    for label, rows in groups.items():
        priced = sum(1 for row in rows if row["cost"]["pricing_status"] != "unknown")
        result.append((label, len(rows), priced, _sum_amount(rows)))
    return sorted(result, key=lambda item: (-item[3], item[0]))


def _render_group_table(
    records: Sequence[Mapping[str, Any]],
    *,
    title: str,
    dotted_key: str,
) -> list[str]:
    lines = [
        f"## {title}",
        "",
        "| Key | Sessions | Priced | Amount |",
        "|---|---:|---:|---:|",
    ]
    rows = _group_rows(records, dotted_key)
    if not rows:
        lines.append("| none | 0 | 0 | $0.0000 |")
        return lines
    for label, count, priced, total in rows:
        lines.append(f"| {label} | {count} | {priced} | {_money(total)} |")
    return lines


def render_markdown(report: CostAttributionReport) -> str:
    coverage = report.coverage
    total = _sum_amount(report.records)
    by_basis = {
        basis: total for basis, _count, _priced, total in _group_rows(report.records, "cost.basis")
    }
    lines = [
        "# Cost Attribution Report",
        "",
        f"Source: `{_display_path(report.source_path)}`",
        f"Tenant map: `{_display_path(report.tenant_map_path)}`"
        if report.tenant_map_path
        else "Tenant map: none",
    ]
    if report.since or report.until:
        lines.append(
            "Window: "
            f"{report.since.isoformat() if report.since else 'beginning'} to "
            f"{report.until.isoformat() if report.until else 'end'}"
        )
    lines.extend(
        [
            "",
            "## Coverage",
            "",
            f"- Records: {coverage['records']} accepted, {coverage['malformed_rows']} malformed skipped",
            f"- Pricing: {coverage['priced']} priced, {coverage['unpriced']} unknown",
            f"- Tenant: {coverage['tenant_known']} known, {coverage['tenant_unknown']} unknown",
            f"- Billable policy: {coverage['billable_known']} known, {coverage['billable_unknown']} unknown",
            "",
            "## Totals",
            "",
            f"- Total USD-equivalent: {_money(total)}",
            f"- Metered cash: {_money(by_basis.get('metered_cash', Decimal('0')))}",
            f"- Metered equivalent: {_money(by_basis.get('metered_equivalent', Decimal('0')))}",
            f"- Subscription equivalent: {_money(by_basis.get('subscription_equivalent', Decimal('0')))}",
            "- Subscription equivalent is opportunity cost, not cash spend.",
            "",
        ]
    )

    for title, key in (
        ("By Tenant", "tenant.org_id"),
        ("By Project", "tenant.project_id"),
        ("By Category", "work.category"),
        ("By Model", "usage.model"),
        ("By Cost Basis", "cost.basis"),
    ):
        lines.extend(_render_group_table(report.records, title=title, dotted_key=key))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def render_jsonl(records: Sequence[Mapping[str, Any]]) -> str:
    return "\n".join(json.dumps(record, sort_keys=True) for record in records) + (
        "\n" if records else ""
    )


def write_jsonl(path: Path, records: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_jsonl(records), encoding="utf-8")


def write_markdown(path: Path, report: CostAttributionReport) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_markdown(report), encoding="utf-8")
