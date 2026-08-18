# gptme-bob-status

Bob-specific `StatusProvider` for `gptme-util status`.

Registers a provider under the `gptme.status_providers` entry-point group that contributes Bob's operational data (active tasks, PR queue, services, blockers, journal entries) to `gptme-util status --json` and `gptme-util status`.

The provider detects Bob's workspace by checking for `tasks/` and `gptme.toml`. Outside Bob's workspace it returns empty data, so the package is safe to install globally.

## Installation

```bash
pip install gptme-bob-status
```

After installation, `gptme-util status` automatically picks up the Bob provider via entry points — no configuration needed.
