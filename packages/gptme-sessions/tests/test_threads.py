"""Behaviour of the session-threading core on small synthetic fleets."""

import pytest

from gptme_sessions import threads as L

ALIASES = {"erik": "erbot"}


def S(i, agent="bob", start=None, dur=600, **kw):
    start = 1_000_000 + i * 3600 if start is None else start
    return {
        "agent": agent,
        "id": f"{agent}-{i}",
        "start": start,
        "end": start + dur,
        "t": "s",
        **kw,
    }


def kinds(edges, a, b):
    return {k for (x, y, k) in edges if (x, y) == (a, b)}


def test_parent_resolves_across_agents():
    nodes = [S(0, "erbot"), S(1, "alice", parent="erbot-0")]
    edges, _ = L.mine_edges(nodes)
    assert kinds(edges, 0, 1) == {"parent"}


def test_journal_mention_resolves_short_label():
    nodes = [S(0, short="6b68"), S(1, sessions_mentioned=["6b68"])]
    edges, _ = L.mine_edges(nodes)
    assert "mention" in kinds(edges, 0, 1)


def test_trailer_commit_then_citation_links_sessions():
    nodes = [S(0), S(1, shas_mentioned=["abcdef1234"])]
    st = L.attach_commits(
        nodes,
        [{"agent": "bob", "sha": "abcdef1234", "ts": nodes[0]["start"] + 60, "sids": ["bob-0"]}],
    )
    assert st["attached"] == 1
    edges, _ = L.mine_edges(nodes)
    assert "commit" in kinds(edges, 0, 1)


def test_attach_commits_accepts_null_optional_deliverables():
    nodes = [S(0, delivered_shas=None), S(1, shas_mentioned=["abcdef1234"])]
    commit = {"agent": "bob", "sha": "abcdef1234", "ts": nodes[0]["end"], "sids": ["bob-0"]}
    assert L.attach_commits(nodes, [commit])["attached"] == 1
    assert L.attach_commits(nodes, [commit])["attached"] == 0
    assert nodes[0]["delivered_shas"] == ["abcdef1234"]
    edges, _ = L.mine_edges(nodes)
    assert kinds(edges, 0, 1) == {"commit"}


def test_plural_trailer_is_not_attributed():
    nodes = [S(0), S(1)]
    st = L.attach_commits(
        nodes, [{"agent": "bob", "sha": "abc1234", "ts": 0, "sids": ["bob-0", "bob-1"]}]
    )
    assert st["plural"] == 1 and not nodes[0].get("delivered_shas")


@pytest.mark.parametrize("kind", ["ref", "commit"])
@pytest.mark.parametrize(
    "gap, factor",
    [(-200000, 1.0), (-1, 1.0), (0, 1.0), (43199, 1.0), (43200, 0.85), (172800, 0.7)],
)
def test_weak_evidence_decays_only_across_idle_gaps(kind, gap, factor):
    work = {"task": "t-x"} if kind == "ref" else {"delivered_shas": ["abcdef1234"]}
    citation = {"tasks_mentioned": ["t-x"]} if kind == "ref" else {"shas_mentioned": ["abcdef1234"]}
    nodes = [S(0, start=0, dur=200001, **work), S(1, start=200001 + gap, **citation)]
    edges, _ = L.mine_edges(nodes)
    assert edges[(0, 1, kind)]["w"] == pytest.approx(L.WEIGHT[kind] * factor)
    if factor == 1.0:
        assert L.best_predecessors(nodes, edges) == [-1, 0]


def test_consecutive_sessions_on_same_task_chain():
    nodes = [S(0, task="t-x"), S(1, task="t-y"), S(2, task="t-x")]
    edges, _ = L.mine_edges(nodes)
    assert "task" in kinds(edges, 0, 2) and not kinds(edges, 0, 1)


def test_message_reply_links_sender_and_replier_sessions():
    nodes = [S(0, "erbot"), S(1, "bob")]
    msgs = [
        {"id": "m1", "from": "erik", "ts": nodes[0]["start"] + 100, "subj": "ops review"},
        {
            "id": "m2",
            "from": "bob",
            "ts": nodes[1]["start"] + 100,
            "subj": "Re: ops review",
            "in_reply_to": "m1",
        },
    ]
    edges, _ = L.mine_edges(nodes, msgs, agent_aliases=ALIASES)
    assert "message" in kinds(edges, 0, 1)


def test_ambiguous_concurrent_sender_is_skipped():
    t = 1_000_000
    nodes = [S(0, "erbot", start=t - 7200), S(1, "bob", start=t), S(2, "bob", start=t + 5)]
    msgs = [
        {"id": "m1", "from": "erik", "ts": t - 7000, "subj": "x"},
        {"id": "m2", "from": "bob", "ts": t + 100, "subj": "Re: x", "in_reply_to": "m1"},
    ]
    edges, st = L.mine_edges(nodes, msgs, agent_aliases=ALIASES)
    assert not any(k == "message" for (_a, _b, k) in edges) and st["message_unresolved"] == 1


def test_best_predecessor_prefers_stronger_then_on_topic():
    nodes = [S(0, task="t"), S(1), S(2, task="t", parent="bob-1")]
    edges, _ = L.mine_edges(nodes)
    assert L.best_predecessors(nodes, edges)[2] == 1  # parent (1.0) beats task (0.8)
    sims = {(1, 2): 0.0, (0, 2): 0.3}
    # with coherence, an off-topic parent (1.0 -> 0.5) loses to the on-topic task link
    assert L.best_predecessors(nodes, edges, sims, coherence=1.0)[2] == 0


def test_unknown_similarity_is_not_penalised():
    assert L.effective_weight("parent", 1.0, -1.0, 1.0) == 1.0
    assert L.effective_weight("message", 0.9, 0.0, 1.0) == 0.9
    assert L.effective_weight("commit", 0.85, 0.0, 1.0) < 0.5


def test_threads_follow_forest_roots():
    assert L.threads([-1, 0, 1, -1, 3]) == [0, 0, 0, 3, 3]


@pytest.mark.parametrize("count", [2, 5, 10, 20])
def test_filtered_topic_vectors_are_unknown_and_keep_evidence(count):
    text = "parser socket timeout retry transport failure"
    nodes = [S(i, title=text) for i in range(count)]
    sims = L.topic_similarity(nodes, [(0, 1)])
    assert sims[(0, 1)] == -1.0
    for kind in ("mention", "commit", "chain"):
        edges = {(0, 1, kind): {"w": L.WEIGHT[kind]}}
        assert L.best_predecessors(nodes, edges, sims, coherence=1.0)[1] == 0


def test_topic_similarity_distinguishes_real_overlap_from_disagreement():
    nodes = [
        S(0, title="parser socket timeout retry transport failure"),
        S(1, title="parser socket timeout retry transport failure"),
        S(2, title="garden carrot potato onion tomato spinach"),
        *[
            S(i, title=f"unrelated{i} filler{i} document{i} token{i} other{i} unique{i}")
            for i in range(3, 20)
        ],
    ]
    sims = L.topic_similarity(nodes, [(0, 1), (0, 2)])
    assert sims == {(0, 1): 1.0, (0, 2): 0.0}


@pytest.mark.parametrize(
    "gap, linked", [(-96400, False), (-1, False), (0, True), (1799, True), (1800, False)]
)
def test_chain_requires_a_nonnegative_bounded_gap(gap, linked):
    nodes = [S(0, start=100, dur=100000), S(1, start=100100 + gap, trigger="chained")]
    edges, _ = L.mine_edges(nodes)
    assert ((0, 1, "chain") in edges) is linked


def test_concurrent_last_run_leaves_chain_unresolved():
    nodes = [
        S(0, start=0, dur=3500),
        S(1, start=100, dur=99900),
        S(2, start=3600, trigger="chained"),
    ]
    edges, _ = L.mine_edges(nodes)
    # Without explicit parent evidence, do not guess which concurrent run spawned it.
    assert not any(kind == "chain" for _, _, kind in edges)


@pytest.mark.parametrize("sid", ["abcd", "abcdef12", "shared-full-session-id"])
def test_exact_session_id_collision_prefers_own_agent(sid):
    nodes = [S(0, "alice", id=sid), S(1, "bob", id=sid)]
    st = L.attach_commits(
        nodes, [{"agent": "alice", "sha": "abcdef1000", "ts": nodes[1]["end"], "sids": [sid]}]
    )
    assert st["attached"] == 1
    assert nodes[0]["delivered_shas"] == ["abcdef1000"]
    assert not nodes[1].get("delivered_shas")


def test_foreign_exact_id_does_not_shadow_own_short_commit_label():
    nodes = [S(0, "alice", id="abcd"), S(1, "bob", id="abcd1234-rest")]
    st = L.attach_commits(
        nodes, [{"agent": "bob", "sha": "abcdef1000", "ts": nodes[1]["end"], "sids": ["abcd"]}]
    )
    assert st["attached"] == 1
    assert nodes[1]["delivered_shas"] == ["abcdef1000"]
    assert not nodes[0].get("delivered_shas")


def test_repeated_exact_id_within_agent_uses_reference_time():
    nodes = [S(0, id="abcd"), S(1, id="abcd")]
    L.attach_commits(
        nodes, [{"agent": "bob", "sha": "abcdef1000", "ts": nodes[0]["end"], "sids": ["abcd"]}]
    )
    assert nodes[0]["delivered_shas"] == ["abcdef1000"]
    assert not nodes[1].get("delivered_shas")


def test_ambiguous_cross_agent_exact_parent_is_unresolved():
    nodes = [
        S(0, "alice", id="shared-id"),
        S(1, "bob", id="shared-id"),
        S(2, "gordon", parent="shared-id"),
    ]
    edges, st = L.mine_edges(nodes)
    assert st["parent_unresolved"] == 1
    assert not edges


def test_linked_journal_still_resolves():
    nodes = [S(0, journal="journal/first.md"), S(1, journal_links=["journal/first.md"])]
    edges, _ = L.mine_edges(nodes)
    assert "mention" in kinds(edges, 0, 1)


@pytest.mark.parametrize("citation", ["abcdef1000", "abcdef1000" + "a" * 30])
def test_precise_commit_citation_disambiguates_shared_prefix(citation):
    nodes = [
        S(0, delivered_shas=["abcdef1000"]),
        S(1, delivered_shas=["abcdef1111"]),
        S(2, shas_mentioned=[citation]),
    ]
    edges, _ = L.mine_edges(nodes)
    assert "commit" in kinds(edges, 0, 2)
    assert "commit" not in kinds(edges, 1, 2)


def test_ambiguous_short_commit_citation_is_skipped():
    nodes = [
        S(0, delivered_shas=["abcdef1000"]),
        S(1, delivered_shas=["abcdef1111"]),
        S(2, shas_mentioned=["abcdef1"]),
    ]
    edges, _ = L.mine_edges(nodes)
    assert not edges


def test_own_colliding_commit_does_not_hide_precise_external_citation():
    nodes = [
        S(0, delivered_shas=["abcdef1000"]),
        S(1, delivered_shas=["abcdef1111"], shas_mentioned=["abcdef1000"]),
    ]
    edges, _ = L.mine_edges(nodes)
    assert "commit" in kinds(edges, 0, 1)


def test_unique_short_commit_citation_still_links():
    nodes = [S(0, delivered_shas=["abcdef1000"]), S(1, shas_mentioned=["abcdef1"])]
    edges, _ = L.mine_edges(nodes)
    assert "commit" in kinds(edges, 0, 1)


def test_attach_commits_preserves_full_hash_and_is_idempotent():
    nodes = [S(0)]
    sha = "abcdef1234" + "5" * 30
    commit = {"agent": "bob", "sha": sha, "ts": nodes[0]["end"], "sids": ["bob-0"]}
    assert L.attach_commits(nodes, [commit])["attached"] == 1
    assert L.attach_commits(nodes, [commit])["attached"] == 0
    assert nodes[0]["delivered_shas"] == [sha]


@pytest.mark.parametrize("label", ["abcd", "abcdef12"])
def test_uuid_journal_mention_without_optional_short(label):
    nodes = [
        S(0, id="abcdef12-0000-0000-0000-000000000001"),
        S(1, sessions_mentioned=[label]),
    ]
    edges, _ = L.mine_edges(nodes)
    assert "mention" in kinds(edges, 0, 1)


def test_uuid_four_character_mention_stays_with_own_agent():
    nodes = [
        S(0, "alice", id="abcdef12-0000-0000-0000-000000000001"),
        S(1, "bob", id="abcdef34-0000-0000-0000-000000000002"),
        S(2, "bob", sessions_mentioned=["abcd"]),
    ]
    edges, _ = L.mine_edges(nodes)
    assert "mention" in kinds(edges, 1, 2)
    assert "mention" not in kinds(edges, 0, 2)


@pytest.mark.parametrize(
    "missing",
    [
        "abcdef12-0000-0000-0000-000000000002",
        "abcdef129999",
        "abcdef99",
        "abcdef1f",
        "journal/absent.md",
    ],
)
def test_absent_identifier_does_not_fall_back_to_a_shorter_prefix(missing):
    nodes = [
        S(0, id="abcdef12-0000-0000-0000-000000000001"),
        S(1, parent=missing, journal_links=[missing]),
    ]
    st = L.attach_commits(
        nodes, [{"agent": "bob", "sha": "abcdef1000", "ts": nodes[0]["end"], "sids": [missing]}]
    )
    assert st["unresolved"] == 1
    assert not nodes[0].get("delivered_shas")
    edges, st = L.mine_edges(nodes)
    assert st["parent_unresolved"] == 1
    assert not edges


@pytest.mark.parametrize("label", ["abcd", "abcde", "abcdef", "abcdef1", "abcdef12"])
def test_short_session_trailer_matches_all_supplied_characters(label):
    nodes = [S(0, id="abcdef12-0000-0000-0000-000000000001")]
    st = L.attach_commits(
        nodes, [{"agent": "bob", "sha": "abcdef1000", "ts": nodes[0]["end"], "sids": [label]}]
    )
    assert st["attached"] == 1
    assert nodes[0]["delivered_shas"] == ["abcdef1000"]


@pytest.mark.parametrize("citation_length", [7, 10, 40])
def test_abbreviated_and_full_delivered_hash_are_one_commit(citation_length):
    sha = "abcdef1234" + "5" * 30
    nodes = [S(0, delivered_shas=[sha[:10]]), S(1, shas_mentioned=[sha[:citation_length]])]
    L.attach_commits(
        nodes, [{"agent": "bob", "sha": sha, "ts": nodes[0]["end"], "sids": ["bob-0"]}]
    )
    edges, _ = L.mine_edges(nodes)
    assert "commit" in kinds(edges, 0, 1)


def test_distinct_full_commits_remain_ambiguous_even_with_one_owner():
    sha = "abcdef1234" + "5" * 30
    other = "abcdef1234" + "6" * 30
    nodes = [S(0, delivered_shas=[sha[:10], sha, other]), S(1, shas_mentioned=[sha[:10]])]
    edges, _ = L.mine_edges(nodes)
    assert not edges


def test_mixed_hash_representations_preserve_distinct_commit_owners():
    sha = "abcdef1234" + "5" * 30
    other = "abcdef1234" + "6" * 30
    nodes = [
        S(0, delivered_shas=[sha[:10], sha]),
        S(1, delivered_shas=[other]),
        S(2, shas_mentioned=[sha]),
    ]
    edges, _ = L.mine_edges(nodes)
    assert "commit" in kinds(edges, 0, 2)
    assert "commit" not in kinds(edges, 1, 2)


def test_ambiguous_delivered_abbreviation_does_not_invent_an_owner():
    sha = "abcdef1234" + "5" * 30
    other = "abcdef1234" + "6" * 30
    nodes = [
        S(0, delivered_shas=[sha[:10]]),
        S(1, delivered_shas=[sha]),
        S(2, delivered_shas=[other]),
        S(3, shas_mentioned=[sha]),
    ]
    edges, _ = L.mine_edges(nodes)
    assert "commit" in kinds(edges, 1, 3)
    assert "commit" not in kinds(edges, 0, 3)
    assert "commit" not in kinds(edges, 2, 3)


@pytest.mark.parametrize("label", ["abcd", "abcde", "abcdef", "abcdef1", "abcdef12"])
def test_parent_label_excludes_child_before_choosing_nearest(label):
    nodes = [
        S(0, id="abcdef12-0000-0000-0000-000000000001"),
        S(1, id="abcdef12-0000-0000-0000-000000000002", parent=label),
    ]
    edges, st = L.mine_edges(nodes)
    assert "parent" in kinds(edges, 0, 1)
    assert st["parent_unresolved"] == 0
    assert L.best_predecessors(nodes, edges) == [-1, 0]


def test_self_parent_is_unresolved_without_another_candidate():
    nodes = [S(0, id="abcdef12-0000-0000-0000-000000000001", parent="abcd")]
    edges, st = L.mine_edges(nodes)
    assert not edges
    assert st["parent_unresolved"] == 1


def test_exact_self_parent_does_not_fall_back_to_shared_label():
    sid = "abcdef12-0000-0000-0000-000000000001"
    nodes = [S(0, id="abcdef12-0000-0000-0000-000000000002"), S(1, id=sid, parent=sid)]
    edges, st = L.mine_edges(nodes)
    assert not edges
    assert st["parent_unresolved"] == 1


@pytest.mark.parametrize(
    "work", [{"task": "t-x"}, {"pr": "owner/project#12"}, {"prs_mentioned": ["owner/project#12"]}]
)
def test_topic_similarity_derives_work_keys_without_mining(work):
    nodes = [
        S(0, title="parser socket timeout retry transport failure", **work),
        S(1, title="garden carrot potato onion tomato spinach", **work),
        *[
            S(i, title=f"unrelated{i} filler{i} document{i} token{i} other{i} unique{i}")
            for i in range(2, 20)
        ],
    ]
    direct = L.topic_similarity(nodes, [(0, 1)])
    assert direct[(0, 1)] > 0
    L.mine_edges(nodes)
    assert L.topic_similarity(nodes, [(0, 1)]) == direct
    # A public-field update must not reuse the miner's now-stale private cache.
    nodes[1].pop(next(iter(work)))
    assert L.topic_similarity(nodes, [(0, 1)])[(0, 1)] == 0


@pytest.mark.parametrize(
    "value, expected",
    [
        ("Alice/Project#12", "alice/project#12"),
        ("https://github.com/Alice/Project/pull/12", "alice/project#12"),
        ("https://github.com/Alice/Project/issues/12", "alice/project#12"),
        ("Project#12", "project#12"),
        (None, None),
    ],
)
def test_pr_normalization_preserves_supplied_owner(value, expected):
    assert L.norm_pr(value) == expected


@pytest.mark.parametrize("field", ["pr", "delivered_prs", "prs_mentioned"])
def test_distinct_repository_owners_do_not_share_pr_edges(field):
    def work(pr):
        return {field: pr if field == "pr" else [pr]}

    nodes = [S(0, **work("alice/project#12")), S(1, **work("bob/project#12"))]
    edges, _ = L.mine_edges(nodes)
    assert not edges
    assert L.threads(L.best_predecessors(nodes, edges)) == [0, 1]


def test_qualified_pr_url_and_reference_share_a_work_key():
    nodes = [S(0, pr="alice/project#12"), S(1, pr="https://github.com/Alice/Project/pull/12")]
    edges, _ = L.mine_edges(nodes)
    assert kinds(edges, 0, 1) == {"pr"}
    assert edges[(0, 1, "pr")]["key"] == "pr:alice/project#12"


def test_unqualified_pr_does_not_guess_an_owner():
    nodes = [S(0, pr="alice/project#12"), S(1, pr="project#12")]
    edges, _ = L.mine_edges(nodes)
    assert not edges


def test_eight_character_mention_uses_cross_agent_fallback():
    nodes = [
        S(0, "alice", id="abcdef12-0000-0000-0000-000000000001"),
        S(1, "bob", sessions_mentioned=["abcdef12"]),
    ]
    edges, _ = L.mine_edges(nodes)
    assert "mention" in kinds(edges, 0, 1)


def test_four_character_mention_does_not_use_cross_agent_fallback():
    nodes = [
        S(0, "alice", id="abcdef12-0000-0000-0000-000000000001"),
        S(1, "bob", sessions_mentioned=["abcd"]),
    ]
    edges, _ = L.mine_edges(nodes)
    assert not edges


@pytest.mark.parametrize("label", ["abcd", "abcde", "abcdef", "abcdef1", "abcdef12"])
def test_mention_label_excludes_self_before_choosing_nearest(label):
    nodes = [
        S(0, id="abcdef12-0000-0000-0000-000000000001", short=label),
        S(1, id="abcdef12-0000-0000-0000-000000000002", short=label, sessions_mentioned=[label]),
    ]
    edges, _ = L.mine_edges(nodes)
    assert kinds(edges, 0, 1) == {"mention"}
    assert L.best_predecessors(nodes, edges) == [-1, 0]


def test_self_only_mention_does_not_make_an_edge():
    sid = "abcdef12-0000-0000-0000-000000000001"
    nodes = [S(0, id=sid, short="abcd", sessions_mentioned=["abcd", "abcdef12", sid])]
    edges, _ = L.mine_edges(nodes)
    assert not edges


@pytest.mark.parametrize("sid", ["shared-id", "abcdef12"])
@pytest.mark.parametrize("label_length", [None, 4, 8])
def test_commit_ownership_never_falls_back_to_another_agent(sid, label_length):
    reference = sid if label_length is None else "abcdef12"[:label_length]
    loaded_id = sid if label_length is None else "abcdef12-0000-0000-0000-000000000001"
    nodes = [S(0, "alice", id=loaded_id)]
    st = L.attach_commits(
        nodes,
        [{"agent": "bob", "sha": "abcdef1000", "ts": nodes[0]["end"], "sids": [reference]}],
    )
    assert st["unresolved"] == 1
    assert not nodes[0].get("delivered_shas")


@pytest.mark.parametrize("reference", ["shared-id", "abcdef12"])
def test_message_sender_never_falls_back_to_another_agent(reference):
    nodes = [S(0, "alice", id=reference), S(1, "gordon")]
    msgs = [
        {"id": "m1", "from": "bob", "session_id": reference, "ts": nodes[0]["start"] + 100},
        {
            "id": "m2",
            "from": "gordon",
            "session_id": "gordon-1",
            "ts": nodes[1]["start"] + 100,
            "in_reply_to": "m1",
        },
    ]
    edges, st = L.mine_edges(nodes, msgs)
    assert not edges
    assert st["message_unresolved"] == 1


def test_missing_explicit_sender_can_use_own_agent_temporal_fallback():
    nodes = [S(0, "alice", id="shared-id"), S(1, "bob"), S(2, "gordon")]
    msgs = [
        {"id": "m1", "from": "bob", "session_id": "shared-id", "ts": nodes[1]["start"] + 100},
        {
            "id": "m2",
            "from": "gordon",
            "session_id": "gordon-2",
            "ts": nodes[2]["start"] + 100,
            "in_reply_to": "m1",
        },
    ]
    edges, _ = L.mine_edges(nodes, msgs)
    assert kinds(edges, 1, 2) == {"message"}
    assert not kinds(edges, 0, 2)
    assert edges[(1, 2, "message")]["w"] == pytest.approx(0.9 * 0.9)


def test_exact_cross_agent_mention_and_linked_journal_still_resolve():
    nodes = [
        S(0, "alice", id="shared-id", journal="journal/alice.md"),
        S(1, "bob", sessions_mentioned=["shared-id"]),
        S(2, "bob", journal_links=["journal/alice.md"]),
    ]
    edges, _ = L.mine_edges(nodes)
    assert kinds(edges, 0, 1) == {"mention"}
    assert kinds(edges, 0, 2) == {"mention"}
