/**
 * gptme-whatsapp bridge
 *
 * Connects WhatsApp (via whatsapp-web.js) to a gptme agent.
 * Supports both gptme and Claude Code as backends.
 *
 * Setup:
 *   npm install
 *   GPTME_AGENT=sven node index.js
 *
 * On first run, scan the QR code with the phone that Sven "owns"
 * (e.g. a dedicated SIM, or link as a secondary device).
 * Auth persists in .wwebjs_auth/ — no rescanning needed after that.
 *
 * Environment variables:
 *   GPTME_AGENT         - Agent name (default: sven)
 *   BACKEND             - 'gptme' or 'claude-code' (default: gptme)
 *   ALLOWED_CONTACTS    - Comma-separated phone numbers
 *   GPTME_CMD           - Path to gptme binary (default: gptme)
 *   CLAUDE_CMD          - Path to claude binary (default: claude)
 *   AGENT_WORKSPACE     - Path to agent workspace
 *   SYSTEM_PROMPT_FILE  - Path to system prompt file for Claude Code
 */

import pkg from 'whatsapp-web.js';
const { Client, LocalAuth } = pkg;
import qrcode from 'qrcode-terminal';
import { gptmeLogsDir, runWithReply } from './gptme-log.js';
import { spawn } from 'child_process';
import { existsSync } from 'fs';
import { sessionArgs, sessionName } from './claude-session.js';
import { createKeyedQueue } from './serial-queue.js';

// Config from env
const AGENT_NAME = process.env.GPTME_AGENT || 'sven';
const BACKEND = process.env.BACKEND || 'gptme'; // 'gptme' or 'claude-code'
const ALLOWED_CONTACTS = (process.env.ALLOWED_CONTACTS || '').split(',').filter(Boolean);
const GPTME_CMD = process.env.GPTME_CMD || 'gptme';
const CLAUDE_CMD = process.env.CLAUDE_CMD || 'claude';
const WORKSPACE = process.env.AGENT_WORKSPACE || process.env.HOME + '/' + AGENT_NAME;
const SYSTEM_PROMPT_FILE = process.env.SYSTEM_PROMPT_FILE || WORKSPACE + '/state/system-prompt.txt';

console.log(`Starting gptme-whatsapp bridge for agent: ${AGENT_NAME} (backend: ${BACKEND})`);
if (ALLOWED_CONTACTS.length > 0) {
    console.log(`Allowed contacts: ${ALLOWED_CONTACTS.join(', ')}`);
} else {
    console.log('Warning: ALLOWED_CONTACTS not set — accepting all contacts');
}
if (BACKEND === 'claude-code' && !existsSync(SYSTEM_PROMPT_FILE)) {
    console.log(`Warning: SYSTEM_PROMPT_FILE not found at ${SYSTEM_PROMPT_FILE}`);
    console.log('Run: cd ${WORKSPACE} && ./scripts/build-system-prompt.sh > ${SYSTEM_PROMPT_FILE}');
}

// Serializes messages per sender so two rapid messages cannot race for the
// same agent session.
const senderQueue = createKeyedQueue();

const RUN_TIMEOUT_MS = 120_000;
// How long a timed-out run gets to stop cleanly before it is force-killed.
const KILL_TERM_MS = 2_000;
// How long a killed run gets to release its pipes before we stop waiting.
const KILL_GRACE_MS = 5_000;
// Agent subprocesses still running.
const running = new Set();

const client = new Client({
    authStrategy: new LocalAuth({ dataPath: '.wwebjs_auth' }),
    puppeteer: {
        headless: true,
        args: ['--no-sandbox', '--disable-setuid-sandbox'],
    },
});

client.on('qr', (qr) => {
    console.log('\nScan this QR code with the WhatsApp phone:\n');
    qrcode.generate(qr, { small: true });
});

client.on('ready', () => {
    console.log(`\nWhatsApp bridge ready — ${AGENT_NAME} is listening for messages`);
});

client.on('auth_failure', (msg) => {
    console.error('Authentication failed:', msg);
    process.exit(1);
});

client.on('message', async (msg) => {
    // Skip group messages, status updates, etc.
    if (msg.from.endsWith('@g.us') || msg.from === 'status@broadcast') {
        return;
    }

    // Skip non-text messages (images, videos, stickers, etc.)
    if (!msg.body) {
        return;
    }

    const sender = msg.from.replace('@c.us', '');
    const body = msg.body.trim();

    // Check allowlist if configured
    if (ALLOWED_CONTACTS.length > 0 && !ALLOWED_CONTACTS.includes(sender)) {
        console.log(`Ignoring message from non-allowed contact: ${sender}`);
        return;
    }

    console.log(`[${new Date().toISOString()}] Message from ${sender}: ${body.slice(0, 80)}...`);

    // One conversation per sender, so a sender's messages must not overlap:
    // they would race for the same session (see serial-queue.js).
    await senderQueue.run(sender, async () => {
        const response = await callAgent(sender, body);
        if (response) {
            await msg.reply(response);
            console.log(`[${new Date().toISOString()}] Replied to ${sender}`);
        }
    });
});

/**
 * Call the agent backend with the message and return the response.
 * Supports both gptme and Claude Code backends.
 */
async function callAgent(sender, message) {
    if (BACKEND === 'claude-code') {
        return callClaudeCode(sender, message);
    }
    return callGptme(sender, message);
}

/**
 * Call gptme with the message and return the response.
 * Uses conversation naming to maintain history per sender.
 */
async function callGptme(sender, message) {
    const convName = `whatsapp-${AGENT_NAME}-${sender.replace(/[^a-zA-Z0-9]/g, '-')}`;

    // gptme takes the prompt as a positional argument (there is no -p flag).
    // '--' stops a message starting with '-' from being parsed as an option.
    const args = [
        '--name', convName,
        '--non-interactive',
        '-y',
        '--workspace', WORKSPACE,
        '--',
        message,
    ];

    // Read the reply from the conversation log, not stdout (which also carries
    // the echoed prompt, tool calls and exit chrome). Only rows this run wrote.
    return truncate(await runWithReply(convName,
        () => spawnAndCapture(GPTME_CMD, args, () => true),
        gptmeLogsDir(process.env, WORKSPACE)));
}

/**
 * Call Claude Code with the message and return the response.
 * Uses --append-system-prompt-file for agent identity and a per-sender session
 * (--session-id on the first message, --resume after) for history.
 */
async function callClaudeCode(sender, message) {
    const args = ['-p', message, '--output-format', 'text'];

    // Inject agent identity via system prompt file
    if (existsSync(SYSTEM_PROMPT_FILE)) {
        args.push('--append-system-prompt-file', SYSTEM_PROMPT_FILE);
    }

    // Per-sender session (maintains history). --resume needs an existing
    // session, so the first message creates it under a deterministic UUID.
    const convId = sessionName(AGENT_NAME, WORKSPACE, sender);
    args.push(...sessionArgs(convId));

    return spawnAndCapture(CLAUDE_CMD, args, (output) => {
        // Claude Code --output-format text returns just the response
        const trimmed = output.trim();
        if (!trimmed) return null;
        // Truncate for WhatsApp 4096 char limit
        if (trimmed.length > 4000) {
            return trimmed.slice(0, 3990) + '\n... (truncated)';
        }
        return trimmed;
    });
}

/**
 * Signal a subprocess and everything it started (its process group), so a
 * GPTME_CMD / CLAUDE_CMD wrapper does not leave the real agent running.
 */
function signalTree(proc, signal) {
    try {
        process.kill(-proc.pid, signal);
    } catch {
        // No such group: already gone, or a platform without process groups.
        proc.kill(signal);
    }
}

/**
 * Spawn a subprocess and capture its output.
 */
function spawnAndCapture(cmd, args, responseExtractor) {
    return new Promise((resolve) => {
        const proc = spawn(cmd, args, {
            cwd: WORKSPACE,
            env: {
                ...process.env,
                // Prevent nested Claude Code sessions if running under CC
                CLAUDECODE: undefined,
                CLAUDE_CODE_ENTRYPOINT: undefined,
            },
            stdio: ['pipe', 'pipe', 'pipe'],
            // Lead a new process group, which signalTree() can target.
            detached: process.platform !== 'win32',
        });
        running.add(proc);

        // Close stdin immediately (prevents SIGSTOP in non-interactive contexts)
        proc.stdin.end();

        let stdout = '';
        let stderr = '';

        proc.stdout.on('data', (data) => { stdout += data.toString(); });
        proc.stderr.on('data', (data) => { stderr += data.toString(); });

        let timedOut = false;
        let killTimer;
        let graceTimer;
        const finish = (result) => {
            clearTimeout(timer);
            clearTimeout(killTimer);
            clearTimeout(graceTimer);
            running.delete(proc);
            resolve(result);
        };

        proc.on('close', (code) => {
            if (code !== 0 || timedOut) {
                console.error(`${cmd} exited with code ${code}`);
                console.error('stderr:', stderr.slice(-500));
                finish(false);
                return;
            }

            // A successful run can still emit diagnostics on stderr.
            if (stderr.trim()) console.error(`${cmd} stderr:`, stderr.slice(-500));
            finish(responseExtractor(stdout));
        });

        proc.on('error', (err) => {
            console.error(`Failed to spawn ${cmd}:`, err);
            finish(false);
        });

        // Do not resolve yet: that would let the sender's next run overlap a
        // still-writing process.
        const timer = setTimeout(() => {
            timedOut = true;
            console.error(`${cmd} timed out`);
            // Ask the whole group to stop cleanly, then force it if the agent
            // does not exit in time.
            signalTree(proc, 'SIGTERM');
            killTimer = setTimeout(() => signalTree(proc, 'SIGKILL'), KILL_TERM_MS);
            // A process that left the group can hold the pipes open, so `close`
            // may never fire. Stop waiting, or this sender stays blocked.
            graceTimer = setTimeout(() => {
                proc.stdout.destroy();
                proc.stderr.destroy();
                finish(false);
            }, KILL_GRACE_MS);
        }, RUN_TIMEOUT_MS);
    });
}

/** Fit a reply in one WhatsApp message (4096 char limit). */
function truncate(response) {
    if (!response) return null;
    if (response.length > 4000) {
        return response.slice(0, 3990) + '\n... (truncated)';
    }
    return response;
}

// Graceful shutdown. Agent runs lead their own process groups, so a terminal's
// Ctrl-C or hangup no longer reaches them: pass the signal on.
for (const signal of ['SIGTERM', 'SIGINT', 'SIGHUP']) {
    process.on(signal, () => {
        console.log('Shutting down WhatsApp bridge...');
        for (const proc of running) signalTree(proc, signal);
        client.destroy().then(() => process.exit(0));
    });
}

client.initialize();
