# Reaching the principal: `principal_notify` + `kuma_push`

Two shared seams for "tell the human something is wrong". They solve different
halves of the same problem:

| Need | Use | Failure it prevents |
|------|-----|---------------------|
| Escalate an incident *now*, from any script | [`scripts/principal_notify.py`](../scripts/principal_notify.py) | An alarm routed to `gh issue create` that, on an agent without `gh`, wrote nothing |
| Prove a monitor is *alive* by pushing heartbeats | [`scripts/kuma_push.py`](../scripts/kuma_push.py) | A silently malformed push URL leaving a dead-man monitor DOWN for weeks |

Both are stdlib-only on purpose: they have to run when the model, the credential,
or the whole loop is dead.

## `principal_notify`: Tier 0 vs Tier 1

- **Tier 0 (always)** writes `state/alerts/<dedup-key>.txt` in the workspace.
  It never fails, and **it is not a notification** — nothing has paged anyone.
- **Tier 1 (pluggable)** delivers out of band: `github`, `pushover`, `telegram`,
  `ntfy`.

`notify_principal()` returns `NotifyResult`; `result.degraded` is `True` until a
Tier-1 backend accepts, and `result.operator_notified` is the explicit yes/no.
The CLI mirrors this in its exit code: **requested Tier-1 + no delivery = exit 1**.
A caller that logs "alert sent" without checking this is the failure this module
exists to remove. `--dry-run` deliberately requests no Tier-1 and exits 0.

### ntfy backend

```bash
# Push URLs and tokens live outside git, mode 600.
install -m 600 /dev/null ~/.config/ntfy/principal.env
cat > ~/.config/ntfy/principal.env <<'EOF'
PRINCIPAL_NOTIFY_NTFY_URL=http://<host>:<port>
PRINCIPAL_NOTIFY_NTFY_TOPIC=<topic>
PRINCIPAL_NOTIFY_NTFY_TOKEN=<token-if-the-topic-is-protected>
EOF

export PRINCIPAL_NOTIFY_BACKENDS=ntfy
export PRINCIPAL_NOTIFY_AGENT_ID=alice
export PRINCIPAL_NOTIFY_NTFY_ENV_FILE=~/.config/ntfy/principal.env

# Tier-0 only: no network, no secret read. Safe to run anywhere.
python3 scripts/principal_notify.py --subject "probe" --body "dry run" --dry-run

# Real delivery. Exit 1 means the server did not accept it.
python3 scripts/principal_notify.py --subject "auth outage" --urgency high \
    --body "no successful model calls for 2h" --dedup-key auth-outage
```

Configuration resolution: process environment first, then `KEY=VALUE` lines in
`PRINCIPAL_NOTIFY_NTFY_ENV_FILE`. An already-set environment variable is never
overridden by the file.

Urgency → ntfy priority (1–5): `low`→2, `normal`→3, `high`→4, `critical`→5,
anything else→3.

**What "delivered" means here.** Success requires HTTP 200 *and* a JSON body
carrying a message `id` — the server accepted the page. ntfy has no read receipt,
so this must never be reported as "the operator saw it". Each accepted page also
appends one receipt to `state/notify-acks.jsonl` (timestamp, backend, agent,
dedup key, subject, urgency, message id). The URL, topic and token are
deliberately absent: on many self-hosted setups the topic is itself a capability.

Failure modes are recorded per backend and never raised, so one dead channel
cannot suppress the local alert or the other backends:

| Failure | `result.failures["ntfy"]` |
|---------|---------------------------|
| `PRINCIPAL_NOTIFY_NTFY_URL`/`_TOPIC` unset | `… not set` |
| topic not `[A-Za-z0-9_-]{1,64}` | `must be 1-64 chars of …` |
| server rejects the publish | `ntfy HTTP 403: <server error>` |
| unreachable / DNS / TLS | `ntfy request failed: <reason class>` |
| 200 but no `id` | `… returned no message id; not acknowledged` |

Error strings never interpolate the request, because a self-hosted ntfy URL can
embed a capability token.

## `kuma_push`: one writer for push monitors

```python
from kuma_push import push, push_url

# URL resolved from the environment, then ~/.config/uptime-kuma/push.env
push("KUMA_PUSH_URL_INFERENCE", "up", "healthy: 3 productive sessions")

# URL already in another variable (e.g. Bob's pager)
push_url(os.environ["BOB_AUTH_PAGER_KUMA_PUSH_URL"], "down", "credential dead")
```

Contract:

- `status` and `msg` are **always** URL-encoded. The original incident was a
  literal space in `msg`; curl exits 3 and a `check=False` caller logged success.
- A failed push is **logged** to stderr (non-zero rc, or a body without
  `{"ok": true}`). Returning `False` silently is the bug, not the behaviour.
- Returns `True` only on Kuma's acknowledgement.
- An unset key is *not* a warning: that is the deploy-time state before a monitor
  exists, and warning on every timer tick trains people to ignore stderr.
- Never raises. A heartbeat failure must not take down the run that carries a
  real alert.

Acknowledgement ledger: every acked push stamps `state/kuma-last-ack.json`
(locked, atomic read-merge-replace; a write failure is logged, never raised).
A configured push starts the bootstrap clock **before** the network attempt,
so a sender that never succeeds still becomes overdue. Repeated failures do
not reset that clock. Shell senders' ledger checkers must call
`start_tracking(path=...)` before `load_acks(path)`; it is idempotent and does
not record an acknowledgement. A missing or invalid bootstrap stamp is
reported as uninitialized tracking, never as healthy.
`stale_acks(acks, keys, now, intervals)` reports keys whose last ack is older
than `3 ×` the expected cadence, plus keys never acked — but only once tracking
is older than one tolerance window, so enabling the ledger does not page on the
first tick. Callers supply their own `intervals` (hours); the helper will not
invent a cadence for a monitor it does not own.

```bash
# Shell callers that push themselves record their own ack after {"ok":true}
python3 scripts/kuma_push.py record-ack KUMA_PUSH_URL_PANIC_STOP --ack-file state/kuma-last-ack.json
```

## Migrating existing senders

Each agent currently carries its own sender. These are drop-in replacements; no
behaviour change is intended beyond the failure semantics above.

| Agent | Current sender | Migrate to |
|-------|----------------|------------|
| Alice | Local wrapper that calls `principal_notify` with a hand-built command line | `PRINCIPAL_NOTIFY_BACKENDS=ntfy` + env file. **No code change** — this is the acceptance bar in gptme/gptme-contrib#2171 |
| Gordon | `scripts/_kuma.py` (urlencode + ack ledger) | `from kuma_push import push, record_ack`. The module is a superset: same `record-ack` CLI, same "never silent on failure" rule, `push_url()` added for already-resolved URLs |
| Bob | `scripts/monitoring/kuma-push-inference.sh` (inline curl) | `python3 scripts/kuma_push.py push KUMA_PUSH_URL_INFERENCE --status up --msg <detail>`; the pager's `emit_kuma_heartbeat` uses `push_url()` with `BOB_AUTH_PAGER_KUMA_PUSH_URL` |
| Bob | `state/alerts/*.txt` read by `context.sh` | Unchanged — Tier 0 is still first and still universal |

Order of adoption matters: keep the old sender running until the new one has an
acknowledged push in the ledger, then delete the old one. Two writers to one
monitor is fine for a transition; deleting the working sender before the
replacement is proven is how a monitor goes dark.

## Demonstration without leaking credentials

Never `cat`, `echo`, or `set -x` an env file containing these values. To show the
contract works, print only the non-secret outcome:

```bash
set +x
python3 scripts/principal_notify.py --subject "fleet probe" --body "ntfy contract check" \
    --dedup-key ntfy-contract-check --urgency low > /tmp/notify-result.json
echo "notify exit: $?"
python3 - <<'EOF'
import json
r = json.load(open("/tmp/notify-result.json"))
print({"delivered_via": r["delivered_via"], "degraded": r["degraded"],
       "operator_notified": r["operator_notified"]})
# local_alert_path is present on every run; it proves storage, not delivery.
EOF
tail -1 state/notify-acks.jsonl   # a receipt with a message id, no URL/topic/token
```

Automated tests use a stub HTTP layer and a stub `curl`; they never need a live
channel. A production channel test requires access that already exists on the
agent — do not create a new channel or rotate live credentials for a probe.
