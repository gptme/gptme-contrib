# Linear Agent Integration

Integrate your gptme-based agent with Linear using the Agent Framework.

## Features

- **@Mentionable**: Users can `@mention` your agent in Linear issues/comments
- **Assignable**: Issues can be delegated to your agent
- **Real-time**: Webhook-based responses in seconds (not polling)
- **Zero billable seats**: Uses OAuth app actor, not human accounts
- **Auto token refresh**: Automatically refreshes OAuth tokens when expired

## Architecture
User @mentions agent in Linear
         |
         v
Linear sends AgentSessionEvent webhook
         |
         v
ngrok tunnel (public HTTPS)
         |
         v
linear-webhook-server.py (Flask server:8081)
         |
         +---> Validates token (auto-refresh if expired)
         +---> Emits acknowledgment activity
         +---> Creates git worktree
         +---> Spawns gptme session
         +---> Merges changes back to main

## Files

| File | Description |
|------|-------------|
| `linear-webhook-server.py` | Flask webhook server - receives Linear events |
| `linear-activity.py` | CLI tool to emit activities back to Linear |
| `.env` | Configuration (secrets - never commit!) |
| `.tokens.json` | OAuth access/refresh tokens (never commit!) |
| `services/` | Systemd service templates |

---

# Setup Guide

Follow these steps to set up Linear integration for your agent.

## Prerequisites

- Linux server with systemd (user services)
- Python 3.10+ with `uv` installed
- `ngrok` installed and authenticated (see below)
- `gptme` installed and accessible in PATH
- Access to Linear workspace settings
- **Agent workspace** with `gptme.toml` configuration

### Installing ngrok

**Have your human operator do the following:**

1. Sign up for a free ngrok account at https://ngrok.com
2. Install ngrok:
   ```bash
   # Linux (snap)
   sudo snap install ngrok

   # Linux (apt)
   curl -sSL https://ngrok-agent.s3.amazonaws.com/ngrok.asc \
     | sudo tee /etc/apt/trusted.gpg.d/ngrok.asc >/dev/null \
     && echo "deb https://ngrok-agent.s3.amazonaws.com buster main" \
     | sudo tee /etc/apt/sources.list.d/ngrok.list \
     && sudo apt update \
     && sudo apt install ngrok

   # macOS
   brew install ngrok
   ```
3. Authenticate ngrok with your authtoken (from ngrok dashboard):
   ```bash
   ngrok config add-authtoken <your-authtoken>
   ```

## Step 1: Get ngrok URL

First, you need a public HTTPS URL for Linear to send webhooks to.

```bash
# Start ngrok temporarily to get your URL
ngrok http 8081

# Note the Forwarding URL, e.g.:
# https://abc123.ngrok-free.app
```

**Note**: Free ngrok tier gives random subdomains. For production, consider ngrok paid plan (static subdomain) or Cloudflare Tunnel.

## Step 2: Create Linear OAuth Application

**Have your human operator do the following in Linear:**

1. Go to **Linear → Settings (gear icon) → Workspace Settings**
2. Click **API** in the left sidebar
3. Click **OAuth Applications**
4. Click the **"+"** button to create new application

5. Fill in the form:

   | Field | Value |
   |-------|-------|
   | **Name** | Your agent name (this becomes the @username) |
   | **Description** | Brief description of your agent |
   | **Webhook URL** | `https://<your-ngrok-domain>/webhook` |
   | **OAuth Callback URL** | `https://<your-ngrok-domain>/oauth/callback` |

6. Enable the **Webhooks** toggle

7. At the bottom, check these webhook event types:
   - ✅ **Agent session events** (REQUIRED - triggers when @mentioned)
   - ✅ **Inbox notifications** (optional)

8. Click **Create**

9. **IMPORTANT**: Copy these values:
   - **Client ID** (`LINEAR_CLIENT_ID`)
   - **Client Secret** (`LINEAR_CLIENT_SECRET`)
   - **Webhook Secret** (`LINEAR_WEBHOOK_SECRET`)

### What Makes the Agent Mentionable?

The OAuth scopes automatically requested include:
- `app:mentionable` - Agent appears in @mention autocomplete
- `app:assignable` - Agent appears in assignee dropdown

The **Name** you enter becomes the agent's @username in Linear.

## Step 3: Set Up Scripts in Your Workspace

### Option A: Automated Setup (Recommended)

Run the interactive setup script which creates symlinks to gptme-contrib (so you get updates automatically via submodule update):

```bash
cd /path/to/gptme-contrib/scripts/linear
./setup.sh
```

The script will:
- Check prerequisites
- Prompt for configuration values
- Display exact values to enter in Linear OAuth app
- Create symlinks in your workspace
- Set up systemd services
- Offer to run OAuth flow

### Option B: Manual Copy

If you prefer to manage files manually:

```bash
# Copy the linear integration scripts to your workspace
cp -r /path/to/gptme-contrib/scripts/linear ~/repos/<your-workspace>/scripts/
```

**Note**: With manual copy, you'll need to re-copy files to get updates.

## Step 4: Create Configuration

Create the `.env` file with your secrets:

```bash
cat > ~/repos/<your-workspace>/scripts/linear/.env << 'EOF'
LINEAR_WEBHOOK_SECRET=<webhook-secret-from-step-2>
LINEAR_CLIENT_ID=<client-id-from-step-2>
LINEAR_CLIENT_SECRET=<client-secret-from-step-2>
EOF
```

**⚠️ CRITICAL**: The `.env` file contains secrets. It must NEVER be committed to git.

## Step 5: Perform Initial OAuth Flow

The first time, you need to authorize the app to get access tokens.

### Option A: Using linear-activity.py (Recommended)

```bash
cd ~/repos/<your-workspace>/scripts/linear

# Start OAuth flow - opens browser for authorization
uv run linear-activity.py auth
```

This will:
1. Generate the Linear authorization URL
2. Open it in your browser (or print if no display)
3. After you authorize, Linear redirects to your callback URL
4. Extract the code from the redirect and exchange for tokens

### Option B: Manual Token Creation

If the CLI auth flow doesn't work, manually create tokens:

1. Build the authorization URL:
   ```
   https://linear.app/oauth/authorize?client_id=<CLIENT_ID>&redirect_uri=<CALLBACK_URL>&scope=read,write,app:mentionable,app:assignable&response_type=code&state=auth
   ```

2. Visit the URL in browser and authorize

3. After redirect, extract the `code` parameter from the URL

4. Exchange code for tokens:
   ```bash
   curl -X POST https://api.linear.app/oauth/token \
     -H "Content-Type: application/x-www-form-urlencoded" \
     -d "grant_type=authorization_code" \
     -d "client_id=<CLIENT_ID>" \
     -d "client_secret=<CLIENT_SECRET>" \
     -d "redirect_uri=<CALLBACK_URL>" \
     -d "code=<CODE>"
   ```

5. Save the response to `.tokens.json`:
   ```json
   {
     "access_token": "<from response>",
     "refresh_token": "<from response>",
     "expires_at": "<calculate: now + expires_in seconds>"
   }
   ```

### Verify Tokens

```bash
# Check token status
uv run linear-activity.py token-status
```

## Step 6: Install Systemd Services

Copy and customize the service templates:

```bash
# Create systemd user directory if needed
mkdir -p ~/.config/systemd/user

# Copy templates
cp ~/repos/<your-workspace>/scripts/linear/services/*.template ~/.config/systemd/user/

# Rename and edit (replace <AGENT_NAME>, <WORKSPACE>, <HOME>)
cd ~/.config/systemd/user
mv agent-linear-webhook.service.template <agent-name>-linear-webhook.service
mv agent-ngrok.service.template <agent-name>-ngrok.service

# Edit both files to replace placeholders with actual values
```

## Step 7: Enable and Start Services

```bash
# Reload systemd
systemctl --user daemon-reload

# Enable services to start on boot
systemctl --user enable <agent-name>-linear-webhook.service
systemctl --user enable <agent-name>-ngrok.service

# Start services
systemctl --user start <agent-name>-linear-webhook.service
systemctl --user start <agent-name>-ngrok.service

# Verify they're running
systemctl --user status <agent-name>-linear-webhook.service
systemctl --user status <agent-name>-ngrok.service
```

## Step 8: Enable Lingering

**Ask your human operator to run:**

```bash
sudo loginctl enable-linger <username>
```

This ensures services start when the machine boots, not just when you log in.

## Step 9: Verify Integration

1. Check services are running:
   ```bash
   systemctl --user status <agent-name>-linear-webhook
   ```

2. Test by @mentioning your agent in Linear:
   - Go to any Linear issue
   - Type `@<your-agent-name>` in a comment
   - Watch logs: `journalctl --user -u <agent-name>-linear-webhook -f`

---

# Usage

## CLI Tool: linear-activity.py

Emit activities back to Linear from your agent sessions.

```bash
# Show thinking/progress (session stays active)
uv run linear-activity.py thought <session-id> "Analyzing the codebase..."

# Send final response (CLOSES the session)
uv run linear-activity.py response <session-id> "Done! See PR #42."

# Report error
uv run linear-activity.py error <session-id> "Failed to access repository"

# Check token status
uv run linear-activity.py token-status

# Manually refresh token
uv run linear-activity.py refresh
```

## Activity Types

| Type | Purpose | Effect |
|------|---------|--------|
| `thought` | Show reasoning/progress | Session stays active |
| `response` | Final answer | **Closes the session** |
| `error` | Error occurred | Marks session as errored |

## Mentioning Users

**Important**: When mentioning users in Linear comments or agent responses via the API, you must use the full Linear profile URL format. Using `@username` syntax does **not** work through the API.

### Correct Format

Use the full profile link in your comment body:
```markdown
[User Name](https://linear.app/<workspace>/settings/account/<user-id>)
```

### Example

Instead of writing:
```markdown
@ErikBjare can you review this?
```

Write:
```markdown
[Erik Bjäreholt](https://linear.app/superuserlabs/settings/account/ace04b67-c8dc-432f-a00d-85953cc14e13) can you review this?
```

### Finding User Profile Links

To find a user's profile link:
1. Go to Linear
2. Click on any user's avatar/name to open their profile
3. Copy the URL from the browser address bar

The URL format is: `https://linear.app/<workspace>/settings/account/<user-id>`

Where:
- `<workspace>` is your Linear workspace slug (e.g., `superuserlabs`)
- `<user-id>` is the user's UUID (e.g., `ace04b67-c8dc-432f-a00d-85953cc14e13`)


## Service Management

```bash
# Check status
systemctl --user status <agent-name>-linear-webhook <agent-name>-ngrok

# View logs (follow)
journalctl --user -u <agent-name>-linear-webhook -f

# Restart services
systemctl --user restart <agent-name>-linear-webhook <agent-name>-ngrok

# Stop services
systemctl --user stop <agent-name>-linear-webhook <agent-name>-ngrok
```

---

# Troubleshooting

## Webhook Not Receiving Events

1. Check ngrok is running: `systemctl --user status <agent-name>-ngrok`
2. Check webhook URL in Linear matches ngrok URL
3. Check logs: `journalctl --user -u <agent-name>-linear-webhook -f`

## Authentication Errors (401)

1. Check `.tokens.json` exists and has valid tokens
2. Check `.env` has CLIENT_ID and CLIENT_SECRET for token refresh
3. Try manual refresh: `uv run linear-activity.py refresh`

## Token Refresh Failing

Ensure `.env` contains all three values:
- `LINEAR_WEBHOOK_SECRET`
- `LINEAR_CLIENT_ID`
- `LINEAR_CLIENT_SECRET`

## Services Not Starting After Reboot

Check if lingering is enabled:
```bash
loginctl show-user <username> | grep Linger
```

If `Linger=no`, ask human to run: `sudo loginctl enable-linger <username>`

## ngrok URL Changed

If using free ngrok, the URL changes on restart. Update the Webhook URL in Linear OAuth application settings.

---

# Security Notes

1. **Never commit secrets**: `.env` and `.tokens.json` must be in `.gitignore`
2. **Webhook signatures**: All webhooks are verified using HMAC-SHA256
3. **OAuth tokens**: Auto-refreshed when expired (requires CLIENT_ID/SECRET)
4. **ngrok authtoken**: Keep private

---

# Reference

## Environment Variables

| Variable | Purpose | Required |
|----------|---------|----------|
| `LINEAR_WEBHOOK_SECRET` | Verify webhook signatures | Yes |
| `LINEAR_CLIENT_ID` | OAuth token refresh | Yes |
| `LINEAR_CLIENT_SECRET` | OAuth token refresh | Yes |
| `AGENT_NAME` | Agent name for paths (default: "agent") | No |
| `AGENT_WORKSPACE` | Path to agent workspace (default: `~/repos/$AGENT_NAME`) | No |
| `WORKTREE_BASE` | Path for session worktrees (default: `~/repos/$AGENT_NAME-worktrees`) | No |
| `PORT` | Webhook server port (default: 8081) | No |

## Important Paths

| Path | Purpose |
|------|---------|
| `scripts/linear/` | Webhook server and CLI |
| `logs/linear-sessions/` | Session execution logs |
| `/tmp/<agent>-linear-notifications/` | Raw webhook payloads |
| `~/.config/systemd/user/` | Systemd service files |
