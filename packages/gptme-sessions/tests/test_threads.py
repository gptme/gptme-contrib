"""Behaviour of the session-threading core on small synthetic fleets."""

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


def test_plural_trailer_is_not_attributed():
    nodes = [S(0), S(1)]
    st = L.attach_commits(
        nodes, [{"agent": "bob", "sha": "abc1234", "ts": 0, "sids": ["bob-0", "bob-1"]}]
    )
    assert st["plural"] == 1 and not nodes[0].get("delivered_shas")


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
