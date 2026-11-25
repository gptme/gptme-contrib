/**
 * Read gptme's reply from its conversation log instead of scraping stdout.
 *
 * Stdout carries terminal output too: the echoed prompt, tool calls, `@complete`
 * and "Goodbye! (resume with …)". The log at
 * `<logs dir>/<name>/conversation.jsonl` has the assistant message by itself.
 */

import { existsSync, readFileSync } from 'fs';
import { homedir } from 'os';
import { join, resolve } from 'path';

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
            if (!fence) fence = match[1];
            else if (match[1][0] === fence[0] && match[1].length >= fence.length) fence = null;
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

const pendingRuns = new Map();

/** Serialize offset capture, generation and extraction for each conversation. */
export async function runWithReply(convName, generate, logsDir = gptmeLogsDir()) {
    const key = logPath(convName, logsDir);
    const previous = pendingRuns.get(key) || Promise.resolve();
    const current = previous.then(async () => {
        const since = logLineCount(convName, logsDir);
        // false means the subprocess failed: never extract a partial reply.
        if (await generate() === false) return null;
        return replySince(convName, since, logsDir);
    });
    // A failed run must not poison later messages in this conversation.
    const settled = current.catch(() => {});
    pendingRuns.set(key, settled);
    try {
        return await current;
    } finally {
        if (pendingRuns.get(key) === settled) pendingRuns.delete(key);
    }
}

/** gptme's conversation logs dir (mirrors gptme.dirs.get_logs_dir). */
export function gptmeLogsDir(env = process.env, cwd = process.cwd()) {
    if (env.GPTME_LOGS_HOME) return resolve(cwd, env.GPTME_LOGS_HOME);
    const data = env.XDG_DATA_HOME || join(homedir(), '.local', 'share');
    return resolve(cwd, data, 'gptme', 'logs');
}

function logPath(convName, logsDir) {
    return join(logsDir, convName, 'conversation.jsonl');
}

function readLines(path) {
    if (!existsSync(path)) return [];
    return readFileSync(path, 'utf8').split('\n').filter((l) => l.trim());
}

/** Number of rows already in the log, so a run's own rows can be told apart. */
export function logLineCount(convName, logsDir = gptmeLogsDir()) {
    return readLines(logPath(convName, logsDir)).length;
}

/**
 * The last assistant message written after row `since`, without tool-call lines.
 *
 * Returns null when the run wrote no assistant text, so a failed run never
 * resends the previous reply.
 */
export function replySince(convName, since, logsDir = gptmeLogsDir()) {
    const rows = readLines(logPath(convName, logsDir)).slice(since);
    for (let i = rows.length - 1; i >= 0; i--) {
        let msg;
        try {
            msg = JSON.parse(rows[i]);
        } catch {
            continue;
        }
        if (!msg || msg.role !== 'assistant' || typeof msg.content !== 'string') continue;
        const text = withoutToolCalls(msg.content);
        if (text) return text;
    }
    return null;
}
