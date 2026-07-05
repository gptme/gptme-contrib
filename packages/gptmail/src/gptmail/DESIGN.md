# Message System Design Notes

## Email Integration

### SMTP/IMAP Support
- Add smtp_config to __init__
- Use smtplib for actual email delivery in send()
- Use imaplib for receiving external emails
- Add method to sync inbox with IMAP server
- Consider using email.mime for proper MIME handling
- Add GPG signing/encryption support

### Threading Support
- Use Message-ID, In-Reply-To, and References headers
- Build conversation trees from message references
- Handle broken threads (missing messages)
- Support viewing conversations as threads
- Add thread-aware archive/delete operations

## Platform Bridges

### Design Principles
- All communication converted to email format
- Unified threading across platforms
- Consistent interface for all providers
- Platform-specific metadata preserved

### Bridge Architecture
```python

# Base class for platform bridges
class MessageBridge:
    def __init__(self, email_system: AgentEmail):
        self.email = email_system

    def fetch_messages(self):
        """Fetch messages from platform."""
        raise NotImplementedError

    def send_message(self, message_id: str):
        """Send message to platform."""
        raise NotImplementedError

    def convert_to_email(self, platform_message) -> str:
        """Convert platform message to email format."""
        raise NotImplementedError

    def convert_from_email(self, email_message) -> Any:
        """Convert email to platform format."""
        raise NotImplementedError
```

### Twitter Bridge
- Convert DMs to/from email format
- Map Twitter handles to email addresses (@user -> user@twitter.gptme.org)
- Include tweet URLs in message body
- Handle threading via tweet reply chains
- Store Twitter metadata in X-Twitter-* headers
- Support sending DMs and tweets

### Discord Bridge
- Convert messages to/from email format
- Map Discord users/channels to addresses
- Handle Discord threading/replies
- Include message embeds and attachments
- Store Discord metadata in X-Discord-* headers
- Support sending to channels/users

## Message Processing Pipeline

1. **Incoming Messages**
   ```python
   # Process incoming messages from any source
   def process_incoming(message: Any, source: str):
       # 1. Convert to email format
       email_msg = bridges[source].convert_to_email(message)

       # 2. Thread matching
       thread = find_thread(email_msg)
       if thread:
           email_msg.add_reference(thread.root_id)

       # 3. Store in inbox
       msg_id = email.receive(email_msg)

       # 4. Trigger notifications
       notify_new_message(msg_id)
   ```

2. **Outgoing Messages**
   ```python
   # Process outgoing messages to any platform
   def process_outgoing(message_id: str):
       # 1. Get message content
       email_msg = email.read_message(message_id)

       # 2. Determine target platform
       platform = get_platform_from_address(email_msg.to)

       # 3. Convert to platform format
       platform_msg = bridges[platform].convert_from_email(email_msg)

       # 4. Send via platform bridge
       bridges[platform].send_message(platform_msg)

       # 5. Move to sent folder
       email.mark_as_sent(message_id)
   ```

## Implementation Phases

1. **Phase 1: Core Email (✅ Done)**
   - Basic email format
   - Local storage
   - File operations
   - Message composition

2. **Phase 2: Real Email (Next)**
   - SMTP integration
   - IMAP sync
   - GPG support
   - Threading support

3. **Phase 3: Platform Bridges**
   - Bridge architecture
   - Twitter integration
   - Discord integration
   - Thread unification

4. **Phase 4: Advanced Features**
   - Smart notifications
   - Search/filter
   - Analytics
   - Backup/sync
