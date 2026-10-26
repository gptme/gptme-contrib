"""Host selection regressions using ActivityWatch's real datastore and query engine."""

from datetime import date, datetime, timedelta, timezone

import pytest
from aw_core.models import Event
from aw_datastore import Datastore
from aw_datastore.storages.memory import MemoryStorage
from aw_query import query

from gptme_activity_summary import aw_data

DAY = date(2026, 10, 21)


class MemoryClient:
    def __init__(self):
        self.store = Datastore(MemoryStorage, testing=True)
        self.bucket_reads = 0
        self.queries = []

    def add(self, bucket_id, event_type, hostname, duration, data):
        bucket = self.store.create_bucket(bucket_id, event_type, "test", hostname)
        bucket.insert(
            Event(
                timestamp=datetime(2026, 10, 21, 9, tzinfo=timezone.utc),
                duration=timedelta(seconds=duration),
                data=data,
            )
        )

    def get_info(self):
        return {"hostname": "server-host"}

    def get_buckets(self):
        self.bucket_reads += 1
        return self.store.buckets()

    def query(self, source, periods):
        self.queries.append(source)
        return [
            [
                {**event, "duration": event["duration"].total_seconds()}
                for event in query("test", source, start, end, self.store)
            ]
            for start, end in periods
        ]


@pytest.fixture
def client(monkeypatch):
    client = MemoryClient()
    monkeypatch.setattr(aw_data, "_get_client", lambda: client)
    monkeypatch.setattr(aw_data, "_fetch_category_rules", lambda: [])
    return client


@pytest.mark.parametrize("reverse", [False, True])
def test_auto_host_does_not_borrow_other_host_watchers(client, reverse):
    sources = [
        ("aw-watcher-window_A", "currentwindow", "A", 3600, {"app": "Code"}),
        ("aw-watcher-afk_B", "afkstatus", "B", 600, {"status": "not-afk"}),
        ("aw-watcher-afk_A", "afkstatus", "A", 3600, {"status": "not-afk"}),
        ("aw-watcher-web-chrome_B", "web.tab.current", "B", 600, {"$domain": "other.test"}),
        ("aw-watcher-web-chrome_A", "web.tab.current", "A", 1800, {"$domain": "selected.test"}),
    ]
    for source in reversed(sources) if reverse else sources:
        client.add(*source)

    activity = aw_data.fetch_aw_activity(DAY, DAY)

    assert activity.total_active_seconds == 3600
    assert [(x.domain, x.duration) for x in activity.top_domains] == [("selected.test", 1800)]
    assert [(x.name, x.duration) for x in activity.categories] == [("Uncategorized", 3600)]
    assert activity.hostname == "A"
    assert activity.afk_filtered
    assert client.bucket_reads == 1
    assert all('_B"' not in source for source in client.queries)


@pytest.mark.parametrize("reverse", [False, True])
def test_multiple_hosts_require_explicit_selection(client, reverse):
    hosts = ["A", "B"]
    for host in reversed(hosts) if reverse else hosts:
        client.add(f"window-{host}", "currentwindow", host, 3600, {"app": host})
        client.add(f"afk-{host}", "afkstatus", host, 1200, {"status": "not-afk"})

    ambiguous = aw_data.fetch_aw_activity(DAY, DAY)
    assert not ambiguous.top_apps
    assert not client.queries
    assert "--aw-host: A, B" in aw_data.format_aw_activity_for_prompt(ambiguous)

    # Imported bucket names need not follow the watcher's normal prefix/suffix convention.
    selected = aw_data.fetch_aw_activity(DAY, DAY, hostname="B")
    assert selected.hostname == "B"
    assert [(x.app, x.duration) for x in selected.top_apps] == [("B", 1200)]
    assert all('"window-A"' not in source and '"afk-A"' not in source for source in client.queries)
    assert "Host: B" in aw_data.format_aw_activity_for_prompt(selected)


def test_unknown_explicit_host_does_not_fall_back(client):
    client.add("aw-watcher-window_laptop", "currentwindow", "laptop", 3600, {"app": "Code"})
    activity = aw_data.fetch_aw_activity(DAY, DAY, hostname="typo")
    assert not activity.top_apps
    assert not client.queries
    assert "No window source for host 'typo'" in aw_data.format_aw_activity_for_prompt(activity)


@pytest.mark.parametrize("duplicate", [False, True])
def test_missing_or_ambiguous_afk_never_borrows_other_host(client, duplicate):
    client.add("aw-watcher-window_A", "currentwindow", "A", 3600, {"app": "Code"})
    client.add("aw-watcher-afk_B", "afkstatus", "B", 600, {"status": "not-afk"})
    if duplicate:
        for name in ["afk-1", "afk-2"]:
            client.add(name, "afkstatus", "A", 1200, {"status": "not-afk"})
    activity = aw_data.fetch_aw_activity(DAY, DAY)
    assert activity.total_active_seconds == 3600
    assert not activity.afk_filtered
    assert all("afk_events" not in source for source in client.queries)
    text = aw_data.format_aw_activity_for_prompt(activity)
    assert "Total window time (unfiltered)" in text
    assert "Total active time" not in text
    assert ("Multiple AFK sources" if duplicate else "No AFK source") in text


@pytest.mark.parametrize("duplicate", [False, True])
def test_missing_or_ambiguous_browser_is_not_selected(client, duplicate):
    client.add("aw-watcher-window_A", "currentwindow", "A", 3600, {"app": "Code"})
    client.add("aw-watcher-web-chrome_B", "web.tab.current", "B", 600, {"$domain": "other.test"})
    if duplicate:
        for name in ["browser-1", "browser-2"]:
            client.add(name, "web.tab.current", "A", 1200, {"$domain": "ambiguous.test"})
    activity = aw_data.fetch_aw_activity(DAY, DAY)
    assert not activity.top_domains
    assert all("web_events" not in source for source in client.queries)
    text = aw_data.format_aw_activity_for_prompt(activity)
    assert ("Multiple browser sources" if duplicate else "No browser source") in text


def test_ambiguous_windows_on_selected_host_are_not_summed(client):
    for name in ["window-1", "window-2"]:
        client.add(name, "currentwindow", "A", 3600, {"app": "Code"})
    activity = aw_data.fetch_aw_activity(DAY, DAY, hostname="A")
    assert not activity.top_apps
    assert not client.queries
    assert "Multiple window sources" in aw_data.format_aw_activity_for_prompt(activity)


def test_missing_host_metadata_never_uses_hostname_suffix(client):
    client.add("aw-watcher-window_A", "currentwindow", "", 3600, {"app": "Code"})
    activity = aw_data.fetch_aw_activity(DAY, DAY)
    assert activity.hostname is None
    assert not client.queries
    assert "missing host metadata" in aw_data.format_aw_activity_for_prompt(activity)


def test_bucket_inventory_failure_is_distinct_from_empty_activity(client, monkeypatch):
    def fail():
        raise ConnectionError("test")

    monkeypatch.setattr(client, "get_buckets", fail)
    activity = aw_data.fetch_aw_activity(DAY, DAY)
    assert activity.available
    assert "Could not read ActivityWatch sources" in aw_data.format_aw_activity_for_prompt(activity)


def test_metadata_hostname_wins_over_misleading_bucket_id(client):
    client.add("aw-watcher-window_wrong", "currentwindow", "selected", 3600, {"app": "Code"})
    client.add("aw-watcher-afk_selected", "afkstatus", "wrong", 600, {"status": "not-afk"})
    client.add("aw-watcher-afk_wrong", "afkstatus", "selected", 1800, {"status": "not-afk"})
    activity = aw_data.fetch_aw_activity(DAY, DAY)
    assert activity.hostname == "selected"
    assert activity.total_active_seconds == 1800
    assert activity.afk_filtered


def test_empty_period_retains_selected_host(client):
    client.add("window", "currentwindow", "A", 3600, {"app": "Code"})
    client.add("afk", "afkstatus", "A", 3600, {"status": "not-afk"})
    client.add("browser", "web.tab.current", "A", 600, {"$domain": "selected.test"})
    activity = aw_data.fetch_aw_activity(date(2026, 10, 20), date(2026, 10, 20))
    assert not activity.top_apps
    assert not activity.warnings
    text = aw_data.format_aw_activity_for_prompt(activity)
    assert "Host: A" in text
    assert "No window activity reported" in text


@pytest.mark.parametrize("with_afk", [False, True])
def test_ordinary_browser_urls_reduce_to_domains_in_raw_human_report(client, monkeypatch, with_afk):
    from click.testing import CliRunner

    import gptme_activity_summary.cc_backend as backend
    import gptme_activity_summary.cli as cli_module

    client.add("window-A", "currentwindow", "A", 3600, {"app": "Firefox"})
    client.add("afk-B", "afkstatus", "B", 300, {"status": "not-afk"})
    if with_afk:
        client.add("afk-A", "afkstatus", "A", 1800, {"status": "not-afk"})
    client.add(
        "browser-B",
        "web.tab.current",
        "B",
        3600,
        {"url": "https://foreign.test/FOREIGN_PATH", "title": "FOREIGN_TITLE"},
    )
    client.add(
        "browser-A",
        "web.tab.current",
        "A",
        1200,
        {
            "url": "https://www.example.test/PRIVATE_PATH?key=QUERY_SENTINEL#FRAGMENT_SENTINEL",
            "title": "TITLE_SENTINEL",
        },
    )
    client.store["browser-A"].insert(
        Event(
            timestamp=datetime(2026, 10, 21, 9, 20, tzinfo=timezone.utc),
            duration=timedelta(seconds=1200),
            data={"url": "http://example.test/SECOND_PATH", "title": "SECOND_TITLE"},
        )
    )

    def unexpected_model(*args, **kwargs):
        pytest.fail("raw report must not invoke a model")

    monkeypatch.setattr(backend, "summarize_human_day_with_cc", unexpected_model)
    monkeypatch.setattr(backend, "summarize_github_activity_with_cc", unexpected_model)

    activity = aw_data.fetch_aw_activity(DAY, DAY, hostname="A")
    expected_seconds = 1800 if with_afk else 2400
    assert [(domain.domain, domain.duration) for domain in activity.top_domains] == [
        ("example.test", expected_seconds)
    ]
    assert activity.afk_filtered is with_afk
    formatted = aw_data.format_aw_activity_for_prompt(activity)
    result = CliRunner().invoke(
        cli_module.cli,
        ["daily", "--mode", "human", "--date", str(DAY), "--aw-host", "A", "--raw"],
    )
    assert result.exit_code == 0, result.output
    for output in [formatted, result.output]:
        assert "Top Websites" in output
        assert "example.test" in output
        assert "Host: A" in output
        for private_detail in [
            "PRIVATE_PATH",
            "QUERY_SENTINEL",
            "FRAGMENT_SENTINEL",
            "TITLE_SENTINEL",
            "SECOND_PATH",
            "SECOND_TITLE",
            "foreign.test",
            "FOREIGN_PATH",
            "FOREIGN_TITLE",
        ]:
            assert private_detail not in output
    assert all('"browser-B"' not in source for source in client.queries)
    assert all('"afk-B"' not in source for source in client.queries)


@pytest.mark.parametrize("url", ["", "not a URL", "about:blank"])
def test_browser_urls_without_a_domain_are_omitted(client, url):
    client.add("window-A", "currentwindow", "A", 3600, {"app": "Firefox"})
    client.add("browser-A", "web.tab.current", "A", 1200, {"url": url, "title": "PRIVATE_TITLE"})
    activity = aw_data.fetch_aw_activity(DAY, DAY)
    assert not activity.top_domains
    assert "Top Websites" not in aw_data.format_aw_activity_for_prompt(activity)
    assert "PRIVATE_TITLE" not in aw_data.format_aw_activity_for_prompt(activity)
