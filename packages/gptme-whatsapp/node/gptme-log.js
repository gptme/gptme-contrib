/**
 * Read gptme's reply from its conversation log instead of scraping stdout.
 *
 * Stdout carries terminal output too: the echoed prompt, tool calls, `@complete`
 * and "Goodbye! (resume with …)". The log at
 * `<logs dir>/<name>/conversation.jsonl` has the assistant message by itself.
 */

import { existsSync, readFileSync, statSync } from 'fs';
import { homedir } from 'os';
import { join, resolve } from 'path';

// How long a log must stay unchanged before an abandoned run is considered done.
const LOG_QUIET_MS = 400;
// Upper bound on waiting for that, so an abandoned run cannot block its sender.
const LOG_QUIET_CAP_MS = 4_000;

// Conversations whose last run was abandoned while a process may still have
// been writing its log. Their rows cannot be attributed until it goes quiet.
const unsettled = new Set();

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

/**
 * Run `generate` and return the reply it wrote.
 *
 * Runs of one conversation must not overlap, or the row offset cannot tell
 * their replies apart; the bridge's per-sender queue guarantees that.
 */
export async function runWithReply(convName, generate, logsDir = gptmeLogsDir()) {
    // A run abandoned mid-write may still append rows. Do not take an offset
    // for this run until that log is quiet, and never fall back to an earlier
    // reply if it is still changing.
    if (unsettled.has(logPath(convName, logsDir))
        && !(await settleAbandonedRun(convName, logsDir))) return null;
    const startedAt = Date.now();
    const since = logLineCount(convName, logsDir);
    // false means the subprocess failed: never extract a partial reply.
    if (await generate() === false) return null;
    return replySince(convName, since, logsDir, startedAt);
}

/** gptme's conversation logs dir (mirrors gptme.dirs.get_logs_dir). */
export function gptmeLogsDir(env = process.env, cwd = process.cwd()) {
    if (env.GPTME_LOGS_HOME) return resolve(cwd, env.GPTME_LOGS_HOME);
    const data = env.XDG_DATA_HOME || join(homedir(), '.local', 'share');
    return resolve(cwd, data, 'gptme', 'logs');
}

export function logPath(convName, logsDir = gptmeLogsDir()) {
    return join(logsDir, convName, 'conversation.jsonl');
}

function logSignature(path) {
    try {
        const st = statSync(path);
        return `${st.size}:${st.mtimeMs}`;
    } catch {
        return 'missing';
    }
}

/**
 * Wait until the log stops changing.
 *
 * A process that left the killed group can hold the pipes and keep appending
 * after the run was abandoned. Its rows must not be attributed to the next run,
 * so that run waits for the writer to go quiet first. Returns false when the log
 * is still changing at the cap: the writer is alive and attribution is unsafe.
 */
export async function waitForLogQuiet(path, quietMs = LOG_QUIET_MS, capMs = LOG_QUIET_CAP_MS) {
    const deadline = Date.now() + capMs;
    let previous = logSignature(path);
    let stableSince = Date.now();
    while (Date.now() < deadline) {
        await new Promise((done) => setTimeout(done, Math.min(quietMs, 100)));
        const current = logSignature(path);
        if (current !== previous) {
            previous = current;
            stableSince = Date.now();
        } else if (Date.now() - stableSince >= quietMs) {
            return true;
        }
    }
    return false;
}

/**
 * Record that a run was abandoned while a process may still hold its log open.
 *
 * Waits for the log to settle, and remembers the conversation if it does not so
 * its next run refuses to attribute rows to itself.
 */
export async function settleAbandonedRun(
    convName, logsDir = gptmeLogsDir(), quietMs = LOG_QUIET_MS, capMs = LOG_QUIET_CAP_MS,
) {
    const path = logPath(convName, logsDir);
    if (await waitForLogQuiet(path, quietMs, capMs)) {
        unsettled.delete(path);
        return true;
    }
    unsettled.add(path);
    return false;
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
 * resends the previous reply. Rows generated before `notBefore` (the run's
 * start) are ignored: an abandoned run's survivor can append its final text
 * after our offset was taken, and that text is not this run's reply.
 */
export function replySince(convName, since, logsDir = gptmeLogsDir(), notBefore = null) {
    const rows = readLines(logPath(convName, logsDir)).slice(since);
    for (let i = rows.length - 1; i >= 0; i--) {
        let msg;
        try {
            msg = JSON.parse(rows[i]);
        } catch {
            continue;
        }
        if (!msg || msg.role !== 'assistant' || typeof msg.content !== 'string') continue;
        if (notBefore !== null) {
            const at = Date.parse(msg.timestamp);
            if (Number.isFinite(at) && at < notBefore) continue;
        }
        const text = withoutToolCalls(msg.content);
        if (text) return text;
    }
    return null;
}
