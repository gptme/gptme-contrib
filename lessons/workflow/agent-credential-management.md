---
match:
  keywords:
    - "need credentials for"
    - "store password securely"
    - "manage API keys"
    - "credential storage"
    - "secrets management"
    - "secure credential"
    - "OAuth blocked at password"
    - "need to authenticate"
    - "store API key securely"
    - "secret management for agent"
---

# Agent Credential Management

## Rule
When needing to store or retrieve credentials, use a GPG-encrypted credential system in the agent's `secrets/` directory.

## Context
When autonomous agents need to:
- Store passwords, API keys, or tokens securely
- Retrieve credentials for external service authentication
- Set up new service integrations requiring secrets
- Handle OAuth flows that require password entry

## Detection
Observable signals indicating credential management is needed:
- OAuth flow blocked at password entry step
- Need to authenticate with external service (GitHub, email, etc.)
- Setting up new integration requiring API keys
- Prompted for credentials during autonomous operation
- Creating accounts for services that require passwords

## Pattern
Use GPG-encrypted credential storage:

```shell
# Directory structure (in agent workspace)
secrets/
├── agent-public.gpg          # Agent's public key
├── credentials/              # Encrypted credentials
│   ├── github.gpg
│   ├── email.gpg
│   └── ...
└── README.md                 # Usage documentation

# Check if credential exists
ls secrets/credentials/<service>.gpg 2>/dev/null

# Read credential (requires GPG decryption)
gpg --decrypt secrets/credentials/<service>.gpg 2>/dev/null

# Store new credential (encrypt with agent's key)
echo '{"service": "github", "username": "...", "token": "..."}' | \
  gpg --encrypt --recipient-file secrets/agent-public.gpg > secrets/credentials/github.gpg
```

**Credential format (JSON)**:
```json
{
  "service": "github",
  "username": "agent-username",
  "password": "...",
  "token": "...",
  "notes": "Created 2026-01-27 for autonomous operations"
}
```

## Setup Requirements
1. Agent needs a GPG keypair (public key in `secrets/`)
2. `secrets/credentials/` directory must exist
3. Human assistance required for initial credential provisioning

## Escalation Path
If credential management is not set up:
1. Check for existing `secrets/` directory and GPG key
2. Document the credential need in workspace issue tracker
3. Escalate to human operator for credential provisioning
4. Once provisioned, credential can be used autonomously

## Outcome
Following this pattern results in:
- **Secure storage**: Credentials encrypted at rest with GPG
- **Autonomous capability**: Agents can retrieve secrets without human intervention
- **Audit trail**: Credential usage tracked via git commits
- **Minimal exposure**: No plaintext credentials in workspace

## Related
- GPG documentation for key management
- Agent workspace `secrets/README.md` for specific setup instructions
