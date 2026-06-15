"""Tests for ``AgentTransport`` — filesystem inter-agent messaging.

Step 2 of folding agent-msg into gptmail. These exercise the transport seam over
a temp ``messages/`` dir with no network and no git: the ``deliver`` hook is
either absent (outbox-only) or a local-copy stub. Behavior is checked against the
``agent-msg.py`` shape it replaces (filename IDs, frontmatter, read-stamping,
delivery-failure stamping).

See task: fold-agent-msg-into-gptmail-single-comms-tool.
"""

from pathlib import Path

import pytest
import yaml

from gptmail.transport import Transport
from gptmail.transport.agent import AgentTransport, meta_of, split_frontmatter

# A message-id (inbox filename) whose sanitized-subject tail leaves a run of
# dashes plus a ``.md`` suffix — i.e. it literally contains ``---``. Carried
# verbatim into ``in_reply_to``, this used to truncate the frontmatter via a
# naive ``content.split("---", 2)``. Erik bug report 2026-06-15.
DASHRUN_ID = "20260615-123532-377975-erik-ty-vs-mypy-eval--blog-candidate----gptma.md"


def _transport(tmp_path: Path, deliver=None) -> AgentTransport:
    return AgentTransport(tmp_path / "messages", "alice", deliver=deliver)


def _frontmatter(path: Path) -> dict:
    meta = meta_of(path)
    assert meta is not None
    return meta


def test_satisfies_protocol(tmp_path: Path) -> None:
    assert isinstance(_transport(tmp_path), Transport)


def test_channel_is_agent(tmp_path: Path) -> None:
    assert _transport(tmp_path).channel == "agent"


def test_send_writes_outbox_returns_filename_id(tmp_path: Path) -> None:
    t = _transport(tmp_path)
    mid = t.send("bob", "Hello", "body text")
    assert mid.endswith(".md")
    out = t.outbox / mid
    assert out.exists()
    fm = _frontmatter(out)
    assert fm["from"] == "alice"
    assert fm["to"] == "bob"
    assert fm["subject"] == "Hello"
    assert fm["read"] is False


def test_send_records_reply_to(tmp_path: Path) -> None:
    t = _transport(tmp_path)
    mid = t.send("bob", "Re: x", "reply body", reply_to="parent.md")
    assert _frontmatter(t.outbox / mid)["in_reply_to"] == "parent.md"


def test_send_with_deliver_hook_places_in_recipient_inbox(tmp_path: Path) -> None:
    bob_inbox = tmp_path / "bob" / "messages" / "inbox"
    t = _transport(tmp_path, deliver=AgentTransport.local_deliver(bob_inbox))
    mid = t.send("bob", "Hi", "hello")
    assert (bob_inbox / mid).exists()
    # Successful delivery leaves no delivered:false stamp.
    assert "delivered" not in _frontmatter(t.outbox / mid)


def test_send_stamps_delivery_failure(tmp_path: Path) -> None:
    t = _transport(tmp_path, deliver=lambda path, recipient: False)
    mid = t.send("bob", "task delivered: complete", "hello")
    meta = _frontmatter(t.outbox / mid)
    assert meta["delivered"] is False
    assert meta["subject"] == "task delivered: complete"


def test_list_inbox_returns_id_subject_timestamp(tmp_path: Path) -> None:
    t = _transport(tmp_path)
    # Deliver a message into alice's own inbox to list it.
    deliver = AgentTransport.local_deliver(t.inbox)
    sender = AgentTransport(tmp_path / "messages", "bob", deliver=deliver)
    mid = sender.send("alice", "Question", "?")
    listed = t.list_inbox()
    assert len(listed) == 1
    msg_id, subject, ts = listed[0]
    assert msg_id == mid
    assert subject == "Question"
    assert ts.year >= 2026


def test_list_inbox_empty_when_no_folder(tmp_path: Path) -> None:
    assert _transport(tmp_path).list_inbox("archive") == []


def test_list_inbox_rejects_path_traversal(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("---\nsubject: nope\ntimestamp: 2026-01-01T00:00:00Z\n---\n")
    with pytest.raises(ValueError):
        _transport(tmp_path).list_inbox("../../outside")


def test_read_marks_message_read(tmp_path: Path) -> None:
    t = _transport(tmp_path)
    bob = AgentTransport(
        tmp_path / "messages", "bob", deliver=AgentTransport.local_deliver(t.inbox)
    )
    mid = bob.send("alice", "Yo", "content here")
    assert _frontmatter(t.inbox / mid)["read"] is False
    content = t.read(mid)
    assert "content here" in content
    assert _frontmatter(t.inbox / mid)["read"] is True


def test_read_only_updates_read_field(tmp_path: Path) -> None:
    t = _transport(tmp_path)
    bob = AgentTransport(
        tmp_path / "messages", "bob", deliver=AgentTransport.local_deliver(t.inbox)
    )
    mid = bob.send("alice", "read: false alarm", "content here")
    t.read(mid)
    meta = _frontmatter(t.inbox / mid)
    assert meta["read"] is True
    assert meta["subject"] == "read: false alarm"


def test_read_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        _transport(tmp_path).read("nope.md")


def test_read_rejects_path_traversal(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        _transport(tmp_path).read("../../etc/passwd")


def test_read_include_thread_prepends_ancestor(tmp_path: Path) -> None:
    # Realistic thread: bob sends an original then a reply, both delivered into
    # alice's inbox. Delivery preserves filenames, so the reply's in_reply_to
    # (bob's outbox filename = parent id) resolves in alice's inbox.
    alice = _transport(tmp_path)
    bob = AgentTransport(
        tmp_path / "bob-messages", "bob", deliver=AgentTransport.local_deliver(alice.inbox)
    )
    parent_id = bob.send("alice", "Original", "the original question")
    reply_id = bob.send("alice", "Re: Original", "the answer", reply_to=parent_id)

    threaded = alice.read(reply_id, include_thread=True)
    assert "the original question" in threaded
    assert "the answer" in threaded
    # ancestor comes first
    assert threaded.index("the original question") < threaded.index("the answer")


def test_read_include_thread_rejects_parent_path_traversal(tmp_path: Path) -> None:
    alice = _transport(tmp_path)
    bob = AgentTransport(
        tmp_path / "bob-messages", "bob", deliver=AgentTransport.local_deliver(alice.inbox)
    )
    (tmp_path / "secret.md").write_text("TOP SECRET\n")
    reply_id = bob.send("alice", "Re: Original", "the answer", reply_to="../../secret.md")

    threaded = alice.read(reply_id, include_thread=True)
    assert "the answer" in threaded
    assert "TOP SECRET" not in threaded


def test_conversation_id_is_order_free_pair(tmp_path: Path) -> None:
    t = _transport(tmp_path)
    mid = t.send("bob", "Hi", "body")  # from alice to bob, in outbox
    assert t.conversation_id_for(mid) == "agent:alice|bob"


def test_conversation_id_falls_back_when_unknown(tmp_path: Path) -> None:
    assert _transport(tmp_path).conversation_id_for("ghost.md") == "agent:ghost.md"


# -- frontmatter ``---``-in-value corruption (Erik bug report 2026-06-15) -----


def test_split_frontmatter_value_containing_fence_is_lossless() -> None:
    # A ``---`` inside the in_reply_to value must NOT end the block, and the
    # split must rejoin losslessly so rewrite paths don't corrupt the file.
    doc = f"---\nfrom: bob\nto: erik\nin_reply_to: {DASHRUN_ID}\nread: false\n---\n\nthe body\n"
    parts = split_frontmatter(doc)
    assert parts is not None
    assert "---".join(parts) == doc
    meta = yaml.safe_load(parts[1])
    assert meta["in_reply_to"] == DASHRUN_ID


def test_split_frontmatter_none_without_block() -> None:
    assert split_frontmatter("no frontmatter here\n") is None
    assert split_frontmatter("---\nunterminated: true\n") is None


def test_reply_to_with_dashrun_roundtrips(tmp_path: Path) -> None:
    t = _transport(tmp_path)
    mid = t.send("bob", "Re: x", "reply body", reply_to=DASHRUN_ID)
    assert _frontmatter(t.outbox / mid)["in_reply_to"] == DASHRUN_ID


def test_read_preserves_dashrun_in_reply_to(tmp_path: Path) -> None:
    # Marking ``read: false`` -> ``read: true`` must not mangle an in_reply_to
    # that contains ``---`` (the rewrite path used a naive split).
    alice = _transport(tmp_path)
    bob = AgentTransport(
        tmp_path / "bob-messages", "bob", deliver=AgentTransport.local_deliver(alice.inbox)
    )
    mid = bob.send("alice", "Re: thread", "answer", reply_to=DASHRUN_ID)
    alice.read(mid)
    meta = _frontmatter(alice.inbox / mid)
    assert meta["read"] is True
    assert meta["in_reply_to"] == DASHRUN_ID


def test_delivery_failure_preserves_dashrun_in_reply_to(tmp_path: Path) -> None:
    # The delivered:false stamp rewrites frontmatter too — must stay intact.
    t = _transport(tmp_path, deliver=lambda path, recipient: False)
    mid = t.send("bob", "Re: x", "body", reply_to=DASHRUN_ID)
    meta = _frontmatter(t.outbox / mid)
    assert meta["delivered"] is False
    assert meta["in_reply_to"] == DASHRUN_ID


def test_make_filename_never_contains_fence(tmp_path: Path) -> None:
    # New message-ids must never carry a ``---`` run (protects external pollers).
    t = _transport(tmp_path)
    mid = t.send("bob", "ty vs mypy eval -- blog candidate -- gptma", "body")
    assert "---" not in mid
