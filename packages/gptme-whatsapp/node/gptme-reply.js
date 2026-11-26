/**
 * Read gptme's reply from its structured stdout.
 *
 * Runs use `--output-format json`, which emits one JSON object per message on
 * stdout. That stream belongs to the run's own process: unlike the shared
 * conversation log, a timed-out run's leftover process cannot append to it, so
 * one run's text can never be returned as another run's reply.
 *
 * Plain text output carries the echoed prompt, tool calls, `@complete` and
 * "Goodbye! (resume with …)", which is why the reply is not scraped from it.
 */

/** Find the end of a tool's JSON object, including nested/quoted braces. */
function jsonEnd(content, start) {
    let depth = 0;
    let quoted = false;
    let escaped = false;
    for (let i = start; i < content.length; i++) {
        const ch = content[i];
        if (escaped) {
            escaped = false;
        } else if (quoted && ch === '\\') {
            escaped = true;
        } else if (ch === '"') {
            quoted = !quoted;
        } else if (!quoted) {
            if (ch === '{') depth++;
            if (ch === '}' && --depth === 0) return i + 1;
        }
    }
    return null;
}

/** Strip complete structured calls, not their argument lines or code examples. */
function withoutToolCalls(content) {
    // Match the upstream call-id syntax, including MCP tool names. Bare @handles
    // are text, apart from the explicit parameterless completion sentinel.
    const markers = /^[ \t]*(?:([`~]{3,})[^\n]*|@[^\r\n]+?\([\w:.-]+\):\s*(?=\{)|@complete(?::\s*\{\})?[ \t]*(?=\r?$))/gm;
    let fence = null;
    let cursor = 0;
    let result = '';
    let match;
    while ((match = markers.exec(content))) {
        if (match[1]) {
            // Only a bare delimiter line closes a fence; ```txt does not.
            const line = content.slice(match.index).split('\n')[0];
            const bare = line.trim().replace(/^[`~]+/, '') === '';
            if (!fence) fence = match[1];
            else if (bare && match[1][0] === fence[0] && match[1].length >= fence.length) fence = null;
            continue;
        }
        if (fence) continue;
        const start = markers.lastIndex;
        const end = content[start] === '{' ? jsonEnd(content, start) : start;
        // Incomplete structured arguments must not leak either.
        result += content.slice(cursor, match.index);
        cursor = end ?? content.length;
        markers.lastIndex = cursor;
    }
    return (result + content.slice(cursor)).trim();
}

/**
 * The last assistant message in a run's JSONL stdout, without tool calls.
 *
 * Returns null when the run produced no assistant text, so a failed or
 * tool-only run never resends an earlier reply.
 */
export function extractReply(stdout) {
    const lines = String(stdout ?? '').split('\n');
    for (let i = lines.length - 1; i >= 0; i--) {
        const line = lines[i].trim();
        if (!line.startsWith('{')) continue;
        let event;
        try {
            event = JSON.parse(line);
        } catch {
            continue;
        }
        if (!event || event.type !== 'message' || event.role !== 'assistant') continue;
        if (typeof event.content !== 'string') continue;
        const text = withoutToolCalls(event.content);
        if (text) return text;
    }
    return null;
}
