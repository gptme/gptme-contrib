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
import { spawn } from 'child_process';
import { existsSync } from 'fs';

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

    // Call agent backend non-interactively
    const response = await callAgent(sender, body);
    if (response) {
        await msg.reply(response);
        console.log(`[${new Date().toISOString()}] Replied to ${sender}`);
    }
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

    const args = [
        '-p', message,
        '--name', convName,
        '--non-interactive',
        '-y',
        '--workspace', WORKSPACE,
    ];

    return spawnAndCapture(GPTME_CMD, args, extractResponse);
}

/**
 * Call Claude Code with the message and return the response.
 * Uses --append-system-prompt-file for agent identity and --resume for history.
 */
async function callClaudeCode(sender, message) {
    const args = ['-p', message, '--output-format', 'text'];

    // Inject agent identity via system prompt file
    if (existsSync(SYSTEM_PROMPT_FILE)) {
        args.push('--append-system-prompt-file', SYSTEM_PROMPT_FILE);
    }

    // Resume previous conversation for this sender (maintains history)
    const convId = `whatsapp-${AGENT_NAME}-${sender.replace(/[^a-zA-Z0-9]/g, '-')}`;
    args.push('--resume', convId);

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
        });

        // Close stdin immediately (prevents SIGSTOP in non-interactive contexts)
        proc.stdin.end();

        let stdout = '';
        let stderr = '';

        proc.stdout.on('data', (data) => { stdout += data.toString(); });
        proc.stderr.on('data', (data) => { stderr += data.toString(); });

        proc.on('close', (code) => {
            if (code !== 0) {
                console.error(`${cmd} exited with code ${code}`);
                console.error('stderr:', stderr.slice(-500));
                resolve(null);
                return;
            }

            const response = responseExtractor(stdout);
            resolve(response);
        });

        proc.on('error', (err) => {
            console.error(`Failed to spawn ${cmd}:`, err);
            resolve(null);
        });

        // Timeout: 120 seconds
        setTimeout(() => {
            proc.kill('SIGTERM');
            console.error(`${cmd} timed out`);
            resolve(null);
        }, 120_000);
    });
}

/**
 * Extract the last assistant response from gptme stdout.
 * gptme --non-interactive prints tool outputs and responses.
 * We want just the final text response.
 */
function extractResponse(output) {
    const lines = output.split('\n');
    const responseLines = [];
    let inResponse = false;

    for (const line of lines) {
        // gptme formats: "assistant: <text>" or just the response text
        if (line.startsWith('assistant:') || line.startsWith('assistant ')) {
            inResponse = true;
            responseLines.push(line.replace(/^assistant:\s*/, '').replace(/^assistant\s+\w+:\s*/, ''));
        } else if (inResponse && !line.startsWith('user:') && !line.startsWith('system:')) {
            responseLines.push(line);
        } else if (line.startsWith('user:') || line.startsWith('system:')) {
            inResponse = false;
        }
    }

    const response = responseLines.join('\n').trim();

    // Fallback: return last non-empty lines if parsing fails
    if (!response) {
        const nonEmpty = lines.filter(l => l.trim()).slice(-10).join('\n');
        return nonEmpty || null;
    }

    // WhatsApp has a 4096 char limit per message
    if (response.length > 4000) {
        return response.slice(0, 3990) + '\n... (truncated)';
    }

    return response;
}

// Graceful shutdown
process.on('SIGTERM', () => {
    console.log('Shutting down WhatsApp bridge...');
    client.destroy().then(() => process.exit(0));
});
process.on('SIGINT', () => {
    console.log('Shutting down WhatsApp bridge...');
    client.destroy().then(() => process.exit(0));
});

client.initialize();
