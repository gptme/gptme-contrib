# Twitter Automation System

A flexible system for Twitter interaction, built on gptme. This system enables AI agents to:
- Monitor Twitter timeline and lists
- Generate contextually appropriate responses
- Maintain high-quality engagement through LLM-assisted review
- Schedule and manage tweet posting

## Components

### 1. Twitter Client (`twitter.py`)
Basic Twitter operations:
- Post tweets and threads
- Read timeline and lists
- Monitor mentions and replies
- Check engagement metrics

### 2. Workflow Manager (`workflow.py`)
Manages the tweet lifecycle:
- Timeline monitoring
- Draft creation and storage
- Review process
- Scheduled posting

### 3. LLM Integration (`llm.py`)
Handles AI-powered operations:
- Tweet evaluation
- Response generation
- Draft review and verification
- Quality control

### 4. Configuration (`config/config.yml`)
Defines interaction parameters:
- Topics of interest
- Evaluation criteria
- Response templates
- Rate limits and blacklists

### 5. Command Line Interface (`cli.py`)
Unified interface for all Twitter operations.

## Directory Structure

```txt
tweets/
├── new/       - New tweet drafts
├── review/    - Tweets pending review
├── approved/  - Approved tweets ready to post
├── posted/    - Archive of posted tweets
└── rejected/  - Rejected tweet drafts
```

## Setup

### Initial Configuration

1. Configure Twitter credentials in `.env`:
```env
# OAuth 2.0 (Recommended)
TWITTER_CLIENT_ID=your_client_id
TWITTER_CLIENT_SECRET=your_client_secret
TWITTER_BEARER_TOKEN=your_bearer_token

# OAuth 1.0a (Legacy)
TWITTER_API_KEY=your_api_key
TWITTER_API_SECRET=your_api_secret
TWITTER_ACCESS_TOKEN=your_access_token
TWITTER_ACCESS_SECRET=your_access_secret
```

2. Configure interaction parameters in `config/config.yml`

## Usage

### Command Line Usage

```bash
# Post a tweet
./twitter.py post "Hello world!"

# Read timeline
./twitter.py timeline

# Monitor for content to respond to
./workflow.py monitor

# Review drafts
./workflow.py review

# Post approved tweets
./workflow.py post
```

### Common Workflows

1. **Monitor and respond to tweets**:
   ```bash
   ./workflow.py monitor --interval 900
   ```

2. **Review and approve drafts**:
   ```bash
   ./workflow.py review
   ```

3. **Post approved tweets**:
   ```bash
   ./workflow.py post
   ```

4. **Auto workflow (monitor, review, post)**:
   ```bash
   ./workflow.py auto --auto-approve --post-approved
   ```

## Automation Options

Instead of built-in cron setup, you can use various methods to automate the Twitter workflow:

### 1. System Cron Job

Add entries to your crontab:

```bash
# Monitor timeline every 15 minutes
*/15 * * * * cd /path/to/project && ./workflow.py monitor --dry-run

# Review notification at 9am, 1pm, and 5pm
0 9,13,17 * * * notify-send "Twitter Review" "Time to review tweet drafts!"

# Post approved tweets every 30 minutes
*/30 * * * * cd /path/to/project && ./workflow.py post
```

Use `crontab -e` to edit your crontab.

### 2. Systemd Timers (Linux)

Create a service file `/etc/systemd/system/twitter-monitor.service`:
```ini
[Unit]
Description=Twitter Monitoring Service
After=network.target

[Service]
Type=oneshot
ExecStart=/path/to/./workflow.py monitor
WorkingDirectory=/path/to/project
User=yourusername

[Install]
WantedBy=multi-user.target
```

Create a timer file `/etc/systemd/system/twitter-monitor.timer`:
```ini
[Unit]
Description=Run Twitter Monitor every 15 minutes

[Timer]
OnBootSec=5min
OnUnitActiveSec=15min

[Install]
WantedBy=timers.target
```

Enable and start the timer:
```bash
sudo systemctl enable twitter-monitor.timer
sudo systemctl start twitter-monitor.timer
```

### 3. Task Scheduler (Windows)

Use Windows Task Scheduler to create scheduled tasks for:
- Monitoring (`workflow monitor`)
- Reviewing (`workflow review`)
- Posting (`workflow post`)

### 4. Docker with Scheduling

Run in a Docker container with scheduled execution:
```dockerfile
FROM python:3.10-slim

WORKDIR /app
COPY . .
RUN pip install -e .

# Run workflow monitor every 15 minutes
CMD ["bash", "-c", "while true; do ./workflow.py monitor; sleep 900; done"]
```

## Best Practices

### Tweet Creation
- Keep tweets concise and focused
- Use 1-2 emojis maximum
- Put links in follow-up tweets
- Maintain professional tone
- Stay within configured topics

### Review Process
1. LLM performs initial review
2. Human review for final approval
3. Consider suggested improvements
4. Check context when relevant
5. Verify thread coherence

### Monitoring
- Check review notifications
- Monitor engagement metrics
- Adjust configuration as needed
- Review rejected tweets for patterns

## Troubleshooting

### Common Issues

1. Authentication Errors
   - Verify credentials in `.env`
   - Check token expiration
   - Ensure proper OAuth flow

2. Rate Limiting
   - Check configured limits
   - Monitor Twitter API usage
   - Adjust intervals if needed

3. Draft Processing
   - Verify LLM availability
   - Check file permissions
   - Monitor disk space

### Logging

Logs are stored in:
- `twitter.log` - Basic operations
- `twitter_error.log` - Error details
- `twitter_llm.log` - LLM interactions
