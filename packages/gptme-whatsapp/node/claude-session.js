/**
 * Per-sender Claude Code session handling.
 *
 * `claude -p --resume` needs an existing session's UUID or title, so resuming a
 * made-up name fails on every message. Each sender instead gets a deterministic
 * UUIDv5: the first message creates the session with `--session-id`, and later
 * messages resume it with `--resume`.
 */

import { createHash } from 'crypto';
import { existsSync, readdirSync } from 'fs';
import { homedir } from 'os';
import { join } from 'path';

// RFC 4122 URL namespace; the name is prefixed so the IDs stay bridge-specific.
const NAMESPACE_URL = '6ba7b811-9dad-11d1-80b4-00c04fd430c8';

/** RFC 4122 version-5 (SHA-1) UUID for `name` in `namespace`. */
export function uuidv5(name, namespace = NAMESPACE_URL) {
    const ns = Buffer.from(namespace.replace(/-/g, ''), 'hex');
    const hash = createHash('sha1')
        .update(Buffer.concat([ns, Buffer.from(name, 'utf8')]))
        .digest();
    hash[6] = (hash[6] & 0x0f) | 0x50;
    hash[8] = (hash[8] & 0x3f) | 0x80;
    const hex = hash.subarray(0, 16).toString('hex');
    return [
        hex.slice(0, 8),
        hex.slice(8, 12),
        hex.slice(12, 16),
        hex.slice(16, 20),
        hex.slice(20),
    ].join('-');
}

/** Deterministic Claude Code session ID for a conversation name. */
export function sessionIdFor(convName) {
    return uuidv5(`gptme-whatsapp:${convName}`);
}

/**
 * Whether Claude Code already stored session `sessionId`.
 *
 * Sessions live at `<config>/projects/<escaped cwd>/<id>.jsonl`. Scanning every
 * project directory avoids depending on how the cwd is escaped.
 */
export function sessionExists(sessionId, configDir = defaultConfigDir()) {
    const projects = join(configDir, 'projects');
    let entries;
    try {
        entries = readdirSync(projects, { withFileTypes: true });
    } catch {
        return false;
    }
    return entries.some(
        (e) => e.isDirectory() && existsSync(join(projects, e.name, `${sessionId}.jsonl`)),
    );
}

/** Args that select the sender's session: create it once, resume it after. */
export function sessionArgs(convName, configDir = defaultConfigDir()) {
    const sessionId = sessionIdFor(convName);
    return sessionExists(sessionId, configDir)
        ? ['--resume', sessionId]
        : ['--session-id', sessionId];
}

function defaultConfigDir() {
    return process.env.CLAUDE_CONFIG_DIR || join(homedir(), '.claude');
}
