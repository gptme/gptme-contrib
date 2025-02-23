# Twitter Automation System

A complete system for automated Twitter interaction, built on gptme. This system enables AI agents to:
- Monitor Twitter timeline and lists
- Generate contextually appropriate responses
- Maintain high-quality engagement through LLM-assisted review
- Schedule and manage tweet posting

## Components

### 1. Twitter CLI (`twitter.py`)
Basic Twitter operations:
- Post tweets and threads
- Read timeline and lists
- Monitor mentions and replies
- Check engagement metrics

### 2. Workflow Manager (`twitter_workflow.py`)
Manages the tweet lifecycle:
- Timeline monitoring
- Draft creation and storage
- Review process
- Scheduled posting

### 3. LLM Integration (`twitter_llm.py`)
Handles AI-powered operations:
- Tweet evaluation
- Response generation
- Draft review and verification
- Quality control

### 4. Configuration (`twitter_config.yml`)
Defines interaction parameters:
- Topics of interest
- Evaluation criteria
- Response templates
- Rate limits and blacklists

### 5. Cron Setup (`setup_twitter_cron.py`)
Automates workflow:
- Timeline monitoring schedule
- Review notifications
- Post scheduling

## Directory Structure

```txt
tweets/
├── new/       - New tweet drafts
├── review/    - Tweets pending review
├── approved/  - Approved tweets ready to post
├── posted/    - Archive of posted tweets
└── rejected/  - Rejected tweet drafts
```

## Usage

### Initial Setup

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

2. Configure interaction parameters in `twitter_config.yml`

3. Set up cron jobs:
```bash
./setup_twitter_cron.py install
```

### Manual Operations

1. Monitor timeline:
```bash
./twitter_workflow.py monitor
```

2. Create tweet draft:
```bash
./twitter_workflow.py draft "Tweet text"
```

3. Review drafts:
```bash
./twitter_workflow.py review
```

4. Post approved tweets:
```bash
./twitter_workflow.py post
```

### Automated Operation

The system runs automatically once cron jobs are installed:
1. Monitors timeline at configured intervals
2. Notifies for review at scheduled times
3. Posts approved tweets according to schedule

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

## Contributing

1. Follow the code style
2. Add tests for new features
3. Update documentation
4. Use conventional commits

## Related

- [Twitter Best Practices](../../lessons/tools/twitter-best-practices.md)
- [Twitter Banner Design](../../tasks/twitter-banner.md)
- [Establish Twitter Workflow](../../tasks/establish-twitter-workflow.md)
