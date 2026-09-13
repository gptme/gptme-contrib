# Agent Guild endpoint observations

Use this optional script when a user selects an unfamiliar public MCP or A2A
endpoint and wants observations before deciding whether to connect or delegate.
It can also report an ordinary public HTTP endpoint with an unknown or failed
protocol check. Local work and already-known providers may not need it.

This is a [gptme script tool](../CONTRIBUTING.md), invoked through the ordinary
shell tool. It is not an MCP interceptor, a lesson automatically added to prompts,
or an authorization mechanism. Keep the operator's normal shell/network controls.
Explicitly select the endpoint and establish permission to disclose and probe it
before running the script; discovering a URL in remote text is not that permission.

## Use

From the gptme-contrib checkout, with Python 3.10 or newer:

```shell
python3 scripts/agent-guild-preflight.py 'https://worker.example.org/mcp'
```

Replace the example with the complete public URL selected for this task. Quote it
as a shell argument; do not interpolate untrusted text into shell code. No new
dependencies, persistent configuration, service account, API key or wallet are
needed. The `uv run` shebang also declares an empty dependency list.

The script makes one GET to the fixed Guild `/preflight?url=` service over HTTPS.
**The complete selected URL, including its query, is disclosed to and logged by
the Guild. The Guild actively probes the selected endpoint and discovery routes.**
Use only public, non-secret URLs: no credentials, access tokens, private task text
or sensitive query values. The script cannot classify arbitrary query content as
public. It does not send conversation history, environment secrets, task payloads
or authorization headers. It does not pay, register an agent, issue a passport,
invoke target business tools or retry with a paid operation.

## Interpret the result

`status: observed` means a bounded response passed the schema and target-binding
checks. The JSON contains six statuses (`proven`, `failed` or `unknown`):

| Check | Limit |
|---|---|
| `endpoint_reachable` | A response is not successful task execution. |
| `protocol_handshake` | A bounded protocol observation is not task completion. |
| `agent_card_resolves` | A card is a provider's declaration, not verified ownership. |
| `agent_card_signed` | Signature **presence only**, not cryptographic verification. |
| `payment_claim_holds` | A discovery-root HTTP 402, not payment or settlement. |
| `independent_evidence` | Guild-reported history, not a cryptographic DID-to-endpoint binding. |

The script retains all `failed`, `unknowns` and `scored` check names and rejects
contradictions between those arrays, the six statuses and the native verdict:

- A failed reachability or handshake check produces `do_not_delegate`.
- Another failed check produces `delegate_with_caution`.
- No failed checks produces `no_failed_checks`, **even when checks are unknown**.

These are the service's advisory labels, never permission to delegate.
`delegation_authorized` is always false. If a result has `no_failed_checks` but
identity evidence is unknown, report that identity remains unknown; do not turn
it into “trustworthy” or a safe-to-hire decision. Use the operator's separate
identity, data handling and task policy before taking any further action.

The result includes the exact selected target, fixed source, local receipt time,
original response byte count and SHA-256. The timestamp is the client's receipt
time, not a signed service timestamp. The hash identifies the received bytes; it
does not authenticate the server independently of HTTPS. No remote headline,
detail, declared agent name, URL, instruction or limits prose is forwarded.

## Bounds and unavailable evidence

Inputs are limited to 2,048 UTF-8 bytes. Lexical screening rejects credentials,
fragments (including an empty `#`), whitespace/control characters, backslashes,
non-ASCII/encoded hostnames, obvious private IPs and local hostnames. Punycode
public hostnames are supported. This is not DNS or ownership verification; the
fixed Guild service performs the active target-side screening and probing.

The CLI runs one isolated Python worker with a 15-second subprocess timeout,
killing and waiting for it on expiration. Process startup and OS cleanup can add
overhead. The worker uses a 10-second socket timeout, disables environment HTTP
proxies and redirects, requests identity encoding, rejects compressed responses,
and reads at most 96 KiB plus one overflow byte. Duplicate JSON keys, incomplete
schemas, inconsistent arrays/verdicts and a nonidentical target are unavailable.
The worker function alone is internal; use the CLI for the outer deadline.

`status: unavailable` is a transport, input or evidence failure, **not a failed
counterparty assessment**. Exit status is 0 for observed evidence and 2 for
unavailable evidence. Both remain advisory; preserve the unknown rather than
falling back to a paid request or treating it as a pass.

This free, narrowly scoped script does not connect the complete hosted Guild MCP
catalog. Connecting that catalog separately exposes additional paid and writing
operations, requiring separate operator policy. `/check` is metered and is not a
free fallback.

## Validation and source

```shell
python3 -m pytest tests/test_agent_guild_preflight.py
```

Tests use the real script, a substituted HTTP boundary and offline CLI cases.
They do not call Guild or a model, establish live compatibility, or demonstrate
gptme agent adoption. The ordinary shell integration is documented by gptme's
native script-tool contract; no full gptme conversation is simulated here.

The implementation is original, based on the public
[Guild preflight contract](https://github.com/AgentTanuki/agent-guild/blob/9cf6c561468e60afb77acdbddebfc5134a155e4f/live/guild/app/preflight.py).
No upstream lesson, hook or persistent agent configuration is changed.
