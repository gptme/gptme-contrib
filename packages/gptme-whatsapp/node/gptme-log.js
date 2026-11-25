/**
 * Read gptme's reply from its conversation log instead of scraping stdout.
 *
 * Stdout carries terminal output too: the echoed prompt, tool calls, `@complete`
 * and "Goodbye! (resume with …)". The log at
 * `<logs dir>/<name>/conversation.jsonl` has the assistant message by itself.
 */

import { existsSync, readFileSync } from 'fs';
import { homedir } from 'os';
import { join } from 'path';

// Structured calls carry a call id; bare @handles are user-facing text.
// gptme also emits the parameterless completion sentinel as `@complete`.
const TOOL_CALL_LINE = /^(?:@[a-z_]+\([^)]*\):.*|@complete(?::\s*\{\})?)$/;

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
export function gptmeLogsDir(env = process.env) {
    if (env.GPTME_LOGS_HOME) return env.GPTME_LOGS_HOME;
    const data = env.XDG_DATA_HOME || join(homedir(), '.local', 'share');
    return join(data, 'gptme', 'logs');
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
        const text = msg.content
            .split('\n')
            .filter((line) => !TOOL_CALL_LINE.test(line.trim()))
            .join('\n')
            .trim();
        if (text) return text;
    }
    return null;
}
