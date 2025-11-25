# Example Hooks Plugin for gptme

**Purpose**: Demonstrates how to create plugins with hooks that extend gptme's behavior at various lifecycle points.

## What This Plugin Shows

This example plugin demonstrates:
1. **Plugin structure** with hooks directory
2. **Hook registration** via `register()` function
3. **Multiple hook types** (SESSION_START, TOOL_PRE_EXECUTE, MESSAGE_POST_PROCESS)
4. **Type-safe hooks** using Protocol classes
5. **Priority-based execution**
6. **Message generation** from hooks
7. **StopPropagation** usage

## Plugin Structure
example-hooks/
├── pyproject.toml           # Plugin metadata
├── README.md                # This file
├── src/
│   └── gptme_example_hooks/
│       ├── __init__.py      # Package initialization
│       └── hooks/
│           ├── __init__.py  # Hooks package
│           └── example_hooks.py  # Hook implementations + register()
└── tests/
    └── test_example_hooks.py  # Plugin tests

## How Hooks Work

### Hook Lifecycle

Hooks are functions that run at specific points in gptme's execution:

1. **SESSION_START**: When a conversation session begins
2. **TOOL_PRE_EXECUTE**: Before any tool executes
3. **MESSAGE_POST_PROCESS**: After processing a message
4. **And 9+ other types**: See gptme/hooks/__init__.py for complete list

### Hook Registration

Each hook module must have a `register()` function that registers hooks with gptme:

```python
from gptme.hooks import HookType, register_hook

def my_hook(logdir, workspace, initial_msgs):
    yield Message("system", "Hook executed!")

def register():
    register_hook(
        name="plugin_name.hook_name",
        hook_type=HookType.SESSION_START,
        func=my_hook,
        priority=100,
    )
```

### Hook Signatures

Each HookType expects a specific signature (via Protocol classes):

- **SESSION_START**: `(logdir: Path, workspace: Path | None, initial_msgs: list[Message])`
- **TOOL_PRE_EXECUTE**: `(log: Log, workspace: Path | None, tool_use: ToolUse)`
- **MESSAGE_POST_PROCESS**: `(manager: LogManager)`

See `gptme/hooks/__init__.py` for all hook type signatures.

## Installation

### For Development

```bash
# From this directory
pip install -e .

# Or with uv
uv pip install -e .
```

### For Production

```bash
# pipx users (recommended)
pipx inject gptme /path/to/example-hooks

# uv users
uv tool install gptme --with /path/to/example-hooks
```

## Usage

Once installed, gptme automatically discovers and loads the plugin:

```bash
# Enable plugin in gptme.toml
[gptme]
plugins = ["gptme_example_hooks"]
plugin_paths = ["/path/to/gptme-contrib/plugins"]

# Run gptme - hooks will execute automatically
gptme "hello"
```

You'll see messages from the hooks at:
- Session start
- Before tool execution
- After message processing

## Example Hooks Included

### 1. Session Start Hook

**When**: At the beginning of every conversation session
**Purpose**: Initialize plugin state, announce presence
**Output**: System message welcoming user

```python
def session_start_hook(logdir, workspace, initial_msgs):
    yield Message("system", f"Example hooks plugin loaded! Workspace: {workspace}")
```

### 2. Tool Pre-Execute Hook

**When**: Before any tool executes
**Purpose**: Validate, transform, or log tool usage
**Output**: System message announcing tool execution

```python
def tool_pre_execute_hook(log, workspace, tool_use):
    yield Message("system", f"About to execute tool: {tool_use.tool}")
```

### 3. Message Post-Process Hook

**When**: After processing any message
**Purpose**: Analytics, logging, or reactions to messages
**Output**: System message confirming processing

```python
def message_post_process_hook(manager):
    last_msg = manager.log.messages[-1] if manager.log.messages else None
    if last_msg:
        yield Message("system", f"Processed {last_msg.role} message")
```

## Advanced Features

### Priority-Based Execution

Hooks with higher priority run first:

```python
register_hook(
    name="high_priority_hook",
    hook_type=HookType.SESSION_START,
    func=my_hook,
    priority=200,  # Higher = runs first
)
```

### StopPropagation

Prevent lower-priority hooks from running:

```python
from gptme.hooks import StopPropagation

def my_hook(manager):
    if some_condition:
        yield Message("system", "Stopping further hooks")
        yield StopPropagation()  # No lower-priority hooks will run
```

### Multiple Hooks

A single plugin can register many hooks:

```python
def register():
    # Register multiple hooks of different types
    register_hook("plugin.session_start", HookType.SESSION_START, session_start_hook)
    register_hook("plugin.tool_pre", HookType.TOOL_PRE_EXECUTE, tool_pre_hook)
    register_hook("plugin.msg_post", HookType.MESSAGE_POST_PROCESS, msg_post_hook)
```

## Testing

```bash
# Run tests
pytest tests/

# With coverage
pytest --cov=src/gptme_example_hooks tests/
```

## Extending This Example

To create your own plugin with hooks:

1. **Copy this directory structure**
2. **Rename package** (gptme_example_hooks → gptme_your_plugin)
3. **Implement your hooks** in hooks/your_hooks.py
4. **Update register()** to register your hooks
5. **Add tests** in tests/
6. **Update pyproject.toml** with your metadata

## Common Use Cases

### Analytics and Logging

Track tool usage, message patterns, or conversation metrics:

```python
def analytics_hook(log, workspace, tool_use):
    # Log to external analytics service
    log_tool_usage(tool_use.tool, tool_use.args)
    # Don't yield any messages - just collect data
    return
    yield  # Makes it a generator
```

### Validation and Safety

Prevent dangerous operations:

```python
def safety_hook(log, workspace, tool_use):
    if tool_use.tool == "shell" and "rm -rf" in tool_use.content:
        yield Message("system", "❌ Dangerous command blocked!")
        yield StopPropagation()  # Stop tool from executing
```

### Auto-Enhancement

Automatically improve tool inputs:

```python
def enhancement_hook(log, workspace, tool_use):
    # Example: Auto-add --verbose to shell commands
    if tool_use.tool == "shell" and "--verbose" not in tool_use.content:
        tool_use.content += " --verbose"
```

### Context Injection

Add helpful information to conversations:

```python
def context_hook(logdir, workspace, initial_msgs):
    # Add workspace info to context
    if workspace:
        file_count = len(list(workspace.rglob("*.py")))
        yield Message("system", f"Workspace has {file_count} Python files")
```

## Hook Types Reference

Available in `gptme.hooks.HookType`:

**Message Lifecycle**:
- MESSAGE_PRE_PROCESS
- MESSAGE_POST_PROCESS
- MESSAGE_TRANSFORM

**Tool Lifecycle**:
- TOOL_PRE_EXECUTE
- TOOL_POST_EXECUTE
- TOOL_TRANSFORM

**File Operations**:
- FILE_PRE_SAVE
- FILE_POST_SAVE
- FILE_PRE_PATCH
- FILE_POST_PATCH

**Session Lifecycle**:
- SESSION_START
- SESSION_END

**Generation**:
- GENERATION_PRE
- GENERATION_POST
- GENERATION_INTERRUPT

**Loop Control**:
- LOOP_CONTINUE

See `gptme/hooks/__init__.py` for complete documentation of each type.

## Resources

- [gptme Documentation](https://gptme.org/docs/)
- [Plugin System](https://gptme.org/docs/plugins.html)
- [Hook System Source](https://github.com/gptme/gptme/blob/master/gptme/hooks/__init__.py)
- [Plugin Discovery Source](https://github.com/gptme/gptme/blob/master/gptme/plugins/__init__.py)

## License

MIT
