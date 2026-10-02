# gptme-voice

Real-time voice interface for gptme agents: talk to your agent by microphone,
browser or phone (Twilio) through the OpenAI Realtime or xAI Grok voice APIs.
The agent keeps its own personality and can hand work to a gptme subagent that
reads files, checks tasks and runs commands in its workspace.

**Status:** experimental but in daily use. It is a server with many optional
integrations (Twilio phone calls, callbacks, cross-agent handoff, physical
"body" control). The core path is a local mic or phone conversation.

## How it fits

- Runs next to an agent workspace (a repo with `gptme.toml`, for example one
  created from [gptme-agent-template](https://github.com/gptme/gptme-agent-template)).
  It loads the personality files listed there.
- Delegates real work to `gptme` subprocesses, so tools, lessons and context
  work as in a normal gptme session.
- Optional companions:
  - [gptme-voice-node](../gptme-voice-node/README.md) is a headless client for
    embedded devices (Raspberry Pi + mic array).
  - [gptme-vision-node](../gptme-vision-node/README.md) adds a camera with an
    on-demand `look` tool.
  - [gptme-rag](../gptme-rag/README.md) powers the opt-in `workspace_search` tool.
  - [gptme-body-protocol](../gptme-body-protocol/) is the remote body-node
    protocol.

## Features

- **Low-latency voice conversations** via OpenAI Realtime (default) or xAI Grok
- **Agent personality** loaded from the workspace's `gptme.toml` prompt files
  (`ABOUT.md` first)
- **Subagent tools**: `subagent`, `subagent_status`, `subagent_cancel`
  dispatch tasks to gptme in the background while the conversation continues
- **`workspace_search`** (opt-in): fast gptme-rag lookup over recent journals
  for "what have you been doing?" questions, without a subagent
- **Phone calls via Twilio**: inbound (`/incoming` webhook + `/twilio` media
  stream) and outbound (`gptme-voice-call`), with a caller allowlist
- **Call continuity**: quick reconnects resume the previous transcript, and
  trusted callbacks after a missed outbound call get that call's prepared
  context
- **Cross-agent handoff** (`handoff_to_agent`): transfer a caller to a peer
  agent through a signed, file-based protocol
- **Camera `look` tool** for `/local` clients that advertise a camera
- **Body tools** (`body_status`, `body_move`, `body_turn`, `body_stop`, …):
  capability-gated goal-level commands to an in-process MAVSDK vehicle or an
  authenticated remote body node, handled directly in the tool bridge and never
  through a subagent
- **Browser transport** (opt-in): `/voice` WebSocket and a `/browser` test page
- **Latency tracing**: per-utterance ASR / first-audio / round-trip JSONL

## Install

`gptme-voice` depends on workspace-only packages (`gptme-body-protocol`), so
install it from a gptme-contrib checkout with uv:

```bash
git clone https://github.com/gptme/gptme-contrib
cd gptme-contrib
uv sync --package gptme-voice                  # server + phone
uv sync --package gptme-voice --extra local    # + PyAudio for gptme-voice-client
uv sync --package gptme-voice --extra body     # + MAVSDK for drone/rover bodies
```

PyAudio needs PortAudio headers (`portaudio19-dev` on Debian/Ubuntu).

### API keys

Keys and most settings below are read from the environment **or** gptme config
(`~/.config/gptme/config.toml` / `config.local.toml`), so you don't need to
export them if gptme already has them:

- `OPENAI_API_KEY` for the default `openai` provider
- `XAI_API_KEY` for `--provider grok`

## Quickstart: talk to your agent locally

```bash
# Terminal 1: start the server (auto-detects the agent workspace, see below)
gptme-voice-server --workspace /path/to/agent-repo

# Terminal 2: local mic/speaker client (needs the `local` extra)
gptme-voice-client
```

Use headphones. The local client mutes the mic while audio plays to avoid a
speaker-to-mic feedback loop, so without headphones you can't interrupt the
agent mid-sentence.

Without `--workspace`, the server checks whether it is running from a
`gptme-contrib/` checkout nested inside an agent repo (i.e. the parent of
`gptme-contrib/` contains a `gptme.toml`) and uses that repo. If not, it uses
generic instructions.

### Server options

```text
gptme-voice-server [--host 0.0.0.0] [--port 8080] [--workspace PATH]
                   [--provider openai|grok] [--model MODEL]
                   [--reasoning-effort minimal|low|medium|high|xhigh]
                   [--voice VOICE] [--output-speed 0.25-1.5]
                   [--enable-browser-transport] [--debug]
```

Endpoints: `GET /` (health), `WS /local` (local and embedded clients),
`POST /incoming` + `WS /twilio` (phone), and with `--enable-browser-transport`
also `WS /voice` + `GET /browser`.

## Phone calls via Twilio

### Inbound

1. Expose the server publicly (for example `ngrok http 8080`).
2. In the Twilio console, set the number's **Voice webhook** to
   `https://<public-url>/incoming` (HTTP POST).
3. Set `TWILIO_AUTH_TOKEN` (used to verify webhook signatures) and
   `TWILIO_CALLER_ALLOWLIST` (comma-separated numbers). Body tools and
   `workspace_search` are exposed only to allowlisted callers (and loopback
   clients). When the allowlist is set, inbound calls from any other number
   are rejected (HTTP 403); without `TWILIO_AUTH_TOKEN` the caller number is
   unauthenticated and can be spoofed. Callers are recognised from the workspace `people/` directory.
   Internal context such as the activity digest and ops status goes only to
   people marked `- Call role: operator`. Everyone else is treated as an
   external guest.

### Outbound

```bash
export TWILIO_ACCOUNT_SID=... TWILIO_AUTH_TOKEN=... TWILIO_PHONE_NUMBER=...
export GPTME_VOICE_PUBLIC_BASE_URL=https://<public-url>   # or TWILIO_PUBLIC_BASE_URL

gptme-voice-call +12025550123
gptme-voice-call +12025550123 --dry-run      # print the TwiML, don't dial
```

Options: `--from-number`, `--public-base-url`, `--workspace`, `--context-file`,
`--call-type` (default `general`), `--dry-run`.

### Missed-call callbacks

If an outbound call goes unanswered and the operator calls back, the callback
session gets the context prepared for the original call, as if the call had
happened with the operator silent.

```bash
gptme-voice-call +12025550123 \
  --workspace /path/to/agent-repo \
  --context-file state/standup-brief.json \
  --call-type standup
```

This appends a note to `state/voice-calls/callback-history.jsonl` and writes
`state/voice-calls/missed-call-context.json` in the workspace. On a callback,
the server reads the referenced `context_file` (workspace-relative, path
traversal rejected) and injects it, including the path. Standup-brief-shaped
JSON (`generated_at` + `text`) gets freshness checks. Other JSON and text files
load as-is. An inlined `context` snapshot in the note is the fallback when the
file is missing, stale or unreadable. Every inbound session also receives a
compact index of the last few calls (pointers, not payloads).

Restore conditions, all required:

- the callback arrives within **4 hours** of the original call and on the same
  UTC calendar day (a call at 23:50 UTC returned at 00:10 UTC does not restore);
- a signed `/incoming` webhook, an exact `TWILIO_CALLER_ALLOWLIST` match, and
  `Call role: operator` in the caller's people file;
- the media WebSocket presents the webhook's grant bound to both number and
  CallSid;
- a bounded Twilio lookup confirms the outbound call went to this caller and
  ended `no-answer`, `busy`, `failed` or `canceled`.

Answered, unresolved, stale or malformed evidence leaves normal inbound
behaviour intact, and API errors fail closed. For older deployments, the server
falls back to `state/voice-calls/last-standup-call-sid.txt` +
`state/standup-brief.json` when no newer note exists.

## Other integrations

### Workspace search (gptme-rag)

Set `GPTME_VOICE_RAG=1` to advertise `workspace_search`. It is disabled
automatically if gptme-rag is not installed. Recap questions search recent
`journal/` files (lexical first) and return in a few seconds. Tunables:
`GPTME_VOICE_RAG_TIMEOUT_SECONDS` (default 8) and
`GPTME_VOICE_RAG_RECENCY_HOURS` (default 24).

### Cross-agent handoff

Set `GPTME_VOICE_HANDOFF_DIR` (a state directory shared with peer agents),
`GPTME_VOICE_HANDOFF_SECRET` (HMAC key; handoff stays disabled without it), and
`GPTME_VOICE_AGENTS` (comma-separated roster of participating agents). The
server's own identity comes from `GPTME_VOICE_AGENT_NAME`, `AGENT_NAME`, or
`[agent] name` in `gptme.toml`. `GPTME_VOICE_HANDOFF_AGENTS` limits which peers
this server may transfer to (default: the roster minus itself).

### Remote body node

```bash
GPTME_VOICE_BODY_URL=tcp://127.0.0.1:7777
GPTME_VOICE_BODY_TOKEN=<body-node-token>
GPTME_VOICE_BODY_CONTROLLER_ID=gptme-voice-local   # optional
```

`GPTME_VOICE_BODY_URL` also accepts `null` (status only, for a tabletop device)
and `mavsdk://<system_address>` (for example `mavsdk://udpin://0.0.0.0:14540`
for PX4 SITL; needs the `body` extra). Plaintext `tcp://` is loopback-only
(`127.0.0.1` / `::1`; hostnames, including `localhost`, are refused), so the
bearer token and physical commands never cross a network in the clear. The
remote node negotiates its capabilities in the handshake, and only matching
tools are registered. Safety (leases, command TTLs, deadman, collision) stays
the body node's job. Limits: `GPTME_VOICE_BODY_MAX_ALT_M` (30),
`GPTME_VOICE_BODY_MAX_MOVE_M` (50), `GPTME_VOICE_BODY_CALL_TIMEOUT_S` (12).

### Latency tracing

Set `GPTME_VOICE_LATENCY_SINK` to a file path or `-` (stdout). Each utterance
emits one JSONL `utterance_trace` with `asr_ms` (speech stopped → transcript),
`tts_first_audio_ms` (`response.created` → first audio chunk) and
`round_trip_ms` (speech stopped → first audio chunk).

## Configuration reference

| Variable | Default | Purpose |
|---|---|---|
| `GPTME_VOICE_SUBAGENT_MODEL_FAST` / `_SMART` | `openrouter/anthropic/claude-haiku-4.5` / gptme default | Subagent models (`GPTME_VOICE_SUBAGENT_MODEL` sets both) |
| `GPTME_VOICE_SUBAGENT_TIMEOUT_FAST_SECONDS` / `_SMART_SECONDS` | `60` / `120` | Subagent timeouts |
| `GPTME_VOICE_SUBAGENT_PATH` | `gptme` on `PATH` | gptme binary used for subagents |
| `GPTME_VOICE_RESUME_WINDOW_SECONDS` | `300` | Reconnects within this window resume the previous transcript |
| `GPTME_VOICE_STATE_DIR` | `/tmp/gptme-voice-call-state` | Per-call state and transcript records |
| `GPTME_VOICE_POST_CALL_COMMAND` | unset | Command run after a call, given the call record paths as arguments and `GPTME_VOICE_POST_CALL_JSON(S)` / `GPTME_VOICE_CALLER_ID` in its environment |
| `GPTME_VOICE_POST_CALL_DELAY_SECONDS` | resume window | Intended post-call delay. The server runs the command right away and passes this value in the command's environment; the command is responsible for waiting (e.g. by scheduling a timer) |
| `GPTME_VOICE_GPTME_SERVER_URL` / `_KEY` | unset | Also post call transcripts to a gptme server conversation |

## Architecture

| Module | Role |
|---|---|
| `realtime/server.py` | Starlette server: endpoints, Twilio auth, call state, callbacks |
| `realtime/openai_client.py` / `xai_client.py` | Realtime API clients (VAD, audio streaming, tool schemas) |
| `realtime/tool_bridge.py` | Async subagent dispatcher plus body/vision/RAG/handoff routing |
| `realtime/missed_call_context.py` | Callback-history notes and callback context loading |
| `realtime/twilio_integration.py`, `call.py` | Twilio TwiML, signing, outbound calls |
| `realtime/client.py` | Local mic/speaker test client |
| `realtime/audio.py`, `latency.py` | PCM ↔ μ-law conversion, latency tracing |
| `rag.py`, `vision.py`, `handoff.py`, `body/` | Workspace search, camera bridge, handoff protocol, body adapters |

## Limitations

- **No barge-in without headphones**: the local client mutes the mic during
  playback. Acoustic echo cancellation (e.g. speexdsp or WebRTC AEC) would fix
  this.
- **Subagent latency**: each subagent call starts a full gptme process, which
  takes a few seconds. The conversation continues and the result is injected
  when ready.
