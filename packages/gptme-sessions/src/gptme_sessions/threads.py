"""Session threading: mine typed evidence links between agent sessions and thread them.

The agent-independent core: stdlib only, no I/O. Callers hand in session dicts;
this module never reads files and knows nothing about any particular fleet.

The two fleet-specific knobs are parameters on :func:`mine_edges`:
``agent_aliases`` (message-sender name -> agent id) and
``agents_without_session_records`` (agents whose nodes are not per-session
records, which must still be eligible as message senders).

A *session* is a dict with at least ``agent``, ``id``, ``start``, ``end`` (epoch
seconds), sorted by start. Optional fields, each of which enables some evidence:

    short                4-8 hex label used in journals ("session 6b68")
    parent               parent_session_id / dispatch_cause.parent_session_id
    task, pr             what the session was dispatched for (dispatch_cause / cascade_intent)
    delivered_shas       commits it made (deliverables + Git-Session-Id trailers)
    delivered_prs/tasks  PRs/tasks named in its own commit subjects
    shas_mentioned, prs_mentioned, tasks_mentioned, sessions_mentioned   from its journal
    journal, journal_links   its journal path and journal paths it links to
    trigger              "chained" marks a run that continues the previous one
    outcome_line/title/task_label/name/why/commits   free text, used for topic similarity

Evidence kinds and base weights (strongest first): see WEIGHT. Each session later
keeps only its single strongest link to an earlier session (``best_predecessors``),
which turns the dense evidence graph into a forest; each tree is a thread.
"""

from __future__ import annotations

import bisect
import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping

WEIGHT = {
    "parent": 1.0,  # record names its parent session
    "mention": 0.95,  # journal names the other session, or links its journal
    "message": 0.9,  # session replied to a message the other session sent
    "commit": 0.85,  # journal cites a commit the other session made
    "task": 0.8,  # consecutive sessions dispatched for the same task
    "pr": 0.75,  # consecutive sessions dispatched for / delivering on the same PR
    "chain": 0.6,  # trigger=chained: continues the run just before
    "ref": 0.5,  # mentions a task/PR another session worked on
}
HUB_LIMIT = 60  # a key mentioned this often (and never worked) is too generic for "ref"

Edges = dict[tuple[int, int, str], dict]


def norm_pr(p: object) -> str | None:
    """'gptme/gptme#12', 'gptme#12' and PR URLs -> 'gptme#12'."""
    if not p:
        return None
    m = re.search(r"([\w.-]+)#(\d+)$", str(p)) or re.search(
        r"/([\w.-]+)/(?:pull|issues)/(\d+)", str(p)
    )
    return f"{m.group(1).lower()}#{m.group(2)}" if m else None


def node_keys(n: dict) -> tuple[set[str], set[str]]:
    """(primary, mentioned) work keys. Primary = what the session worked on."""
    pk: set[str] = set()
    if n.get("task"):
        pk.add("task:" + n["task"])
    for p in [n.get("pr"), *(n.get("delivered_prs") or [])]:
        if norm_pr(p):
            pk.add("pr:" + norm_pr(p))  # type: ignore[operator]
    pk |= {"task:" + t for t in n.get("delivered_tasks") or []}
    mk = {"task:" + t for t in n.get("tasks_mentioned") or []}
    mk |= {"pr:" + k for p in n.get("prs_mentioned") or [] if (k := norm_pr(p))}
    return pk, mk - pk


class _Index:
    """Lookup of sessions by full id and by short label, per agent and fleet-wide."""

    def __init__(self, nodes: list[dict]):
        self.nodes = nodes
        self.by_id: dict[str, int] = {}
        self.short: dict[tuple[str, str], list[int]] = defaultdict(list)
        self.short_any: dict[str, list[int]] = defaultdict(list)
        for i, n in enumerate(nodes):
            self.by_id[str(n["id"])] = i
            if n.get("journal"):
                self.by_id.setdefault(n["journal"], i)
            labels: set[str] = {str(n["id"])[:8]}
            if n.get("short"):
                labels.add(str(n["short"]))
            if re.fullmatch(r"[0-9a-f]{4,8}", str(n["id"])):
                labels.add(str(n["id"])[:4])
            for lb in labels - {""}:
                self.short[(n["agent"], lb)].append(i)
                self.short_any[lb].append(i)

    def resolve(self, sid: str, agent: str | None, t: float | None = None) -> int | None:
        """Session for an id or label; ambiguity is settled by time proximity."""
        sid = str(sid).strip()
        if sid in self.by_id:
            return self.by_id[sid]
        for key in (sid[:8], sid[:4]) if len(sid) >= 4 else ():
            cands = self.short.get((agent, key)) if agent else None
            cands = cands or (self.short_any.get(key) if len(key) == 8 else None)
            if cands:
                return nearest(self.nodes, cands, t) if t is not None else cands[-1]
        return None


def nearest(nodes: list[dict], cands: list[int], t: float) -> int | None:
    """Prefer the closest candidate that started before t (a reference points backwards)."""
    if not cands:
        return None
    before = [c for c in cands if nodes[c]["start"] <= t]
    return min(before or cands, key=lambda c: abs(nodes[c]["start"] - t))


def attach_commits(nodes: list[dict], commits: Iterable[dict]) -> Counter:
    """Git-Session-Id trailers: add each commit to its session's delivered_shas.

    commits: {"agent", "sha", "ts" (epoch), "sids": [...]}. Plural trailers come from
    squashes and can't say which session wrote what, so they're skipped.
    """
    idx = _Index(nodes)
    st: Counter = Counter()
    for c in commits:
        if len(c["sids"]) != 1:
            st["plural"] += 1
            continue
        i = idx.resolve(c["sids"][0], c["agent"], c.get("ts"))
        if i is None:
            st["unresolved"] += 1
            continue
        shas = nodes[i].setdefault("delivered_shas", [])
        if c["sha"][:10] not in shas:
            shas.append(c["sha"][:10])
            st["attached"] += 1
    return st


def session_at(
    nodes: list[dict],
    by_agent: dict[str, list[int]],
    agent: str,
    t: float,
    subject: str = "",
    slack: float = 600,
) -> int | None:
    """The session of `agent` that most plausibly sent a message at time t.

    Candidates run within `slack` of t. With several (Bob runs many at once), prefer
    sessions launched to handle messages or by the operator, then subject-word overlap
    with the session's text; give up unless one candidate clearly wins.
    """
    cands = [
        i
        for i in by_agent.get(agent, ())
        if nodes[i]["start"] - slack <= t <= nodes[i]["end"] + slack
    ]
    if len(cands) <= 1:
        return cands[0] if cands else None
    words = {w for w in _TOK.findall(subject.lower()) if w not in _STOP and w != "re"}

    def score(i: int) -> float:
        n = nodes[i]
        inside = n["start"] <= t <= n["end"] + 60
        dist = 0 if inside else min(abs(t - n["start"]), abs(t - n["end"])) / slack
        kind = str(n.get("dispatch_kind") or "") + " " + str(n.get("run_type") or "")
        handler = any(k in kind for k in ("agent-msg", "operator", "message"))
        text = " ".join(
            str(n.get(f) or "") for f in ("outcome_line", "title", "task_label", "why")
        ).lower()
        overlap = sum(1 for w in words if w in text)
        return (
            (1.0 if inside else 0.5 - 0.5 * dist) + (1.0 if handler else 0) + min(overlap, 4) * 0.75
        )

    ranked = sorted(cands, key=score, reverse=True)
    return ranked[0] if score(ranked[0]) - score(ranked[1]) >= 0.75 else None


def mine_edges(
    nodes: list[dict],
    messages: Iterable[dict] = (),
    *,
    agent_aliases: Mapping[str, str] | None = None,
    agents_without_session_records: Iterable[str] = (),
) -> tuple[Edges, Counter]:
    """All candidate evidence edges, keyed (earlier, later, kind) -> {"w", "key"}.

    agent_aliases: maps a message sender name to the agent id its sessions use.
    agents_without_session_records: agents whose nodes are not per-session
        records, but which may still have sent a message.
    """
    aliases = dict(agent_aliases or {})
    no_records = frozenset(agents_without_session_records)
    edges: Edges = {}
    st: Counter = Counter()
    idx = _Index(nodes)

    def decay(a: int, b: int) -> float:
        # weak evidence across a long gap is usually context ("HEAD at 90c0abb"), not follow-up
        gap = abs(nodes[b]["start"] - nodes[a]["end"]) / 3600
        return 1.0 if gap < 12 else 0.85 if gap < 48 else 0.7

    def spec(count: int, ok: int) -> float:
        # sessions citing many things at once (sweeps, weekly reviews) shouldn't become hubs
        return 1.0 if count <= ok else max(0.35, (ok / count) ** 0.5)

    def add(a: int, b: int, kind: str, key: str | None = None, sp: float = 1.0) -> None:
        if a == b:
            return
        if (nodes[a]["start"], a) > (nodes[b]["start"], b):
            a, b = b, a
        w = WEIGHT[kind] * sp * (decay(a, b) if kind in ("commit", "ref") else 1.0)
        e = edges.get((a, b, kind))
        if e is None or w > e["w"]:
            edges[(a, b, kind)] = {"w": w, "key": key}
        st[kind] += 1

    for i, n in enumerate(nodes):
        n["_pk"], n["_mk"] = node_keys(n)

    # parent: exact id, else label (own agent first, then fleet-wide: cross-agent parents)
    for i, n in enumerate(nodes):
        if n.get("parent"):
            j = idx.resolve(n["parent"], n["agent"], n["start"])
            if j is not None:
                add(j, i, "parent")
            else:
                st["parent_unresolved"] += 1

    # named sessions and linked journals
    for i, n in enumerate(nodes):
        own = {n.get("short"), str(n["id"])[:4]}
        sp = spec(len(n.get("sessions_mentioned") or []), 6)
        for s in n.get("sessions_mentioned") or []:
            if s in own:
                continue
            j = nearest(nodes, [c for c in idx.short.get((n["agent"], s), []) if c != i], n["end"])
            if j is not None:
                add(j, i, "mention", "session " + s, sp)
        for lk in n.get("journal_links") or []:
            j = idx.by_id.get(lk)
            if j is not None:
                add(j, i, "mention", lk.split("/")[-1])

    # cited commits -> the session that made them
    deliv: dict[str, set[int]] = defaultdict(set)
    for i, n in enumerate(nodes):
        for s in n.get("delivered_shas") or []:
            deliv[s[:7]].add(i)
    for i, n in enumerate(nodes):
        mine = {s[:7] for s in n.get("delivered_shas") or []}
        sp = spec(len(n.get("shas_mentioned") or []), 8)
        for s in n.get("shas_mentioned") or []:
            if s[:7] in mine:
                continue
            ds = [d for d in deliv.get(s[:7], ()) if d != i and nodes[d]["start"] <= n["end"]]
            if 0 < len(ds) <= 2:
                for d in ds:
                    add(d, i, "commit", s[:7], sp)

    # work keys: consecutive workers chain; mentions attach to the nearest prior worker
    primary: dict[str, list[int]] = defaultdict(list)
    mentioned: dict[str, list[int]] = defaultdict(list)
    for i, n in enumerate(nodes):
        for k in n["_pk"]:
            primary[k].append(i)
        for k in n["_mk"]:
            mentioned[k].append(i)
    for k, ix in primary.items():
        for a, b in zip(ix, ix[1:]):
            add(a, b, "task" if k.startswith("task:") else "pr", k)
    for k, ix in mentioned.items():
        prim = primary.get(k, [])
        if not prim and len(ix) > HUB_LIMIT:
            continue
        pstarts = [nodes[p]["start"] for p in prim]
        for i in ix:
            pos = bisect.bisect_right(pstarts, nodes[i]["end"]) - 1
            sp = spec(len(nodes[i]["_mk"]), 4)
            if pos >= 0:
                add(prim[pos], i, "ref", k, sp)
            elif prim:
                add(i, prim[0], "ref", k, sp)
        if not prim:
            for a, b in zip(ix, ix[1:]):
                add(
                    a,
                    b,
                    "ref",
                    k,
                    min(spec(len(nodes[a]["_mk"]), 4), spec(len(nodes[b]["_mk"]), 4)),
                )

    # chained runs
    last: dict[str, int] = {}
    for i, n in enumerate(nodes):
        if n.get("t", "s") != "s":
            continue
        p = last.get(n["agent"])
        if (
            p is not None
            and n.get("trigger") == "chained"
            and n["start"] - nodes[p]["end"] < 30 * 60
        ):
            add(p, i, "chain")
        last[n["agent"]] = i

    # message replies: the replying session follows the session that sent the original
    by_agent: dict[str, list[int]] = defaultdict(list)
    for i, n in enumerate(nodes):
        if n.get("t", "s") == "s" or n["agent"] in no_records:
            by_agent[n["agent"]].append(i)
    msgs: dict[str, dict] = {}
    for m in messages:
        cur = msgs.get(m["id"])
        if cur is None or (m.get("session_id") and not cur.get("session_id")):
            msgs[m["id"]] = m

    def sender_session(m: dict) -> tuple[int | None, bool]:
        agent = aliases.get(m["from"], m["from"])
        if m.get("session_id"):
            j = idx.resolve(m["session_id"], agent, m["ts"])
            if j is not None:
                return j, True
        return session_at(nodes, by_agent, agent, m["ts"], m.get("subj") or ""), False

    for m in msgs.values():
        orig = msgs.get(m.get("in_reply_to") or "")
        if not orig:
            continue
        sa, ea = sender_session(orig)
        sb, eb = sender_session(m)
        if sa is None or sb is None:
            st["message_unresolved"] += 1
            continue
        add(sa, sb, "message", "msg: " + (orig.get("subj") or "")[:60], 1.0 if (ea and eb) else 0.9)
    return edges, st


# ---------------------------------------------------------------- topic similarity

_STOP = set(
    "the and for with that this from into was were are not but have has had its it's than then "
    "also only just more most when what which while after before over under about via per each "
    "all any one two three new now still yet session sessions productive blocked noop task tasks "
    "pr prs fix fixed added add commit commits journal work worked check checked".split()
)
_TOK = re.compile(r"[a-z][a-z0-9_-]{2,}")


def _tokens(n: dict) -> list[str]:
    text = " ".join(
        str(x)
        for x in [
            n.get("outcome_line"),
            n.get("title"),
            n.get("task_label"),
            n.get("name"),
            n.get("why"),
            " ".join(n.get("commits") or []),
        ]
        if x
    ).lower()
    toks = [t for t in _TOK.findall(text) if t not in _STOP]
    # work keys are strong topic markers: count them twice
    keys = [k.split(":", 1)[1] for k in sorted(n.get("_pk", set()) | n.get("_mk", set()))]
    return toks + keys * 2


MIN_TOKENS = 6  # below this a session has too little text to judge its topic


def topic_similarity(
    nodes: list[dict], pairs: Iterable[tuple[int, int]]
) -> dict[tuple[int, int], float]:
    """TF-IDF cosine between the text + work keys of two sessions (0..1).

    -1 means unknown: one side has too little text (e.g. a PM slot with no summary)
    or no informative terms survive the corpus-frequency filter.
    Missing evidence must not read as a topic change.
    """
    toks = [_tokens(n) for n in nodes]
    df: Counter = Counter()
    for t in toks:
        df.update(set(t))
    N = len(nodes)
    vec_cache: dict[int, tuple[dict[str, float], float]] = {}

    def vec(i: int) -> tuple[dict[str, float], float]:
        if i not in vec_cache:
            tf = Counter(toks[i])
            v = {
                t: (1 + math.log(c)) * math.log(N / df[t]) for t, c in tf.items() if df[t] < N * 0.2
            }
            vec_cache[i] = (v, math.sqrt(sum(x * x for x in v.values())) or 1.0)
        return vec_cache[i]

    out = {}
    for a, b in pairs:
        if len(toks[a]) < MIN_TOKENS or len(toks[b]) < MIN_TOKENS:
            out[(a, b)] = -1.0
            continue
        (va, na), (vb, nb) = vec(a), vec(b)
        if not va or not vb:
            out[(a, b)] = -1.0
            continue
        if len(va) > len(vb):
            va, vb = vb, va
        out[(a, b)] = round(sum(x * vb.get(t, 0.0) for t, x in va.items()) / (na * nb), 3)
    return out


# ---------------------------------------------------------------- threading

# on-topic by construction: a shared work key, or a reply in the same message thread
KEYED = {"task", "pr", "ref", "message"}
ON_TOPIC_SIM = 0.08  # at or above this, a link counts as on-topic (no penalty)


def effective_weight(kind: str, w: float, sim: float, coherence: float) -> float:
    """Downweight links between sessions about different things.

    coherence=0 trusts the evidence as-is; coherence=1 halves a parent/mention/commit/
    chain link when the two sessions share no topic, so a spawn, a coordination mention
    ("session 1828 holds the claim") or a commit cited as context doesn't glue unrelated
    work together. Spot checks (2026-11-07): same-work links mostly score >= 0.08.
    Unknown similarity (-1) is not penalised.
    """
    if kind in KEYED or coherence <= 0 or sim < 0:
        return w
    return w * (1 - coherence * 0.5 * (1 - min(1.0, sim / ON_TOPIC_SIM)))


def best_predecessors(
    nodes: list[dict],
    edges: Edges,
    sims: dict[tuple[int, int], float] | None = None,
    minw: float = 0.5,
    coherence: float = 0.0,
    kinds: set[str] | None = None,
) -> list[int]:
    """Each session's single strongest earlier link (ties -> more on-topic, then most recent)."""
    pred = [-1] * len(nodes)
    best = [(0.0, 0.0, 0.0)] * len(nodes)
    for (a, b, kind), e in edges.items():
        if kinds is not None and kind not in kinds:
            continue
        sim = (sims or {}).get((a, b), -1.0)
        w = effective_weight(kind, e["w"], sim, coherence)
        if w < minw:
            continue
        score = (round(w, 6), sim, nodes[a]["start"])
        if score > best[b]:
            best[b], pred[b] = score, a
    return pred


def threads(pred: list[int]) -> list[int]:
    """Thread (tree root) of each session in a best-predecessor forest."""
    root = list(range(len(pred)))
    for i in range(len(pred)):  # preds always point to earlier indices
        if pred[i] >= 0:
            root[i] = root[pred[i]]
    return root
