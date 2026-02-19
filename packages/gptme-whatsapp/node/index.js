/**
 * gptme-whatsapp bridge
 *
 * Connects WhatsApp (via whatsapp-web.js) to a gptme agent.
 *
 * Setup:
 *   npm install
 *   GPTME_AGENT=sven node index.js
 *
 * On first run, scan the QR code with the phone that Sven "owns"
 * (e.g. a dedicated SIM, or link as a secondary device).
 * Auth persists in .wwebjs_auth/ — no rescanning needed after that.
 */

import { Client, LocalAuth } from 'whatsapp-web.js';
import qrcode from 'qrcode-terminal';
import { spawn } from 'child_process';

// Config from env
const AGENT_NAME = process.env.GPTME_AGENT || 'sven';
const ALLOWED_CONTACTS = (process.env.ALLOWED_CONTACTS || '').split(',').filter(Boolean);
const GPTME_CMD = process.env.GPTME_CMD || 'gptme';
const WORKSPACE = process.env.AGENT_WORKSPACE || process.env.HOME + '/' + AGENT_NAME;

console.log(`Starting gptme-whatsapp bridge for agent: ${AGENT_NAME}`);
if (ALLOWED_CONTACTS.length > 0) {
    console.log(`Allowed contacts: ${ALLOWED_CONTACTS.join(', ')}`);
} else {
    console.log('Warning: ALLOWED_CONTACTS not set — accepting all contacts');
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

    const sender = msg.from.replace('@c.us', '');

    // Skip media messages or messages without text body
    if (!msg.body) {
        console.log(`Ignoring non-text message from ${sender} (type: ${msg.type})`);
        return;
    }

    const body = msg.body.trim();

    // Check allowlist if configured
    if (ALLOWED_CONTACTS.length > 0 && !ALLOWED_CONTACTS.includes(sender)) {
        console.log(`Ignoring message from non-allowed contact: ${sender}`);
        return;
    }

    console.log(`[${new Date().toISOString()}] Message from ${sender}: ${body.slice(0, 80)}...`);

    // Call gptme non-interactively
    const response = await callGptme(sender, body);
    if (response) {
        await msg.reply(response);
        console.log(`[${new Date().toISOString()}] Replied to ${sender}`);
    }
});

/**
 * Call gptme with the message and return the response.
 * Uses conversation naming to maintain history per sender.
 */
async function callGptme(sender, message) {
    return new Promise((resolve) => {
        // Sanitize sender for use as conversation name
        const convName = `whatsapp-${AGENT_NAME}-${sender.replace(/[^a-zA-Z0-9]/g, '-')}`;

        const args = [
            '-p', message,
            '--name', convName,
            '--non-interactive',
            '-y',
            '--workspace', WORKSPACE,
        ];

        const proc = spawn(GPTME_CMD, args, {
            env: {
                ...process.env,
                // Prevent nested Claude Code sessions if running under CC
                CLAUDECODE: undefined,
                CLAUDE_CODE_ENTRYPOINT: undefined,
            },
            stdio: ['ignore', 'pipe', 'pipe'],
        });

        let stdout = '';
        let stderr = '';

        proc.stdout.on('data', (data) => { stdout += data.toString(); });
        proc.stderr.on('data', (data) => { stderr += data.toString(); });

        proc.on('close', (code) => {
            if (code !== 0) {
                console.error(`gptme exited with code ${code}`);
                console.error('stderr:', stderr.slice(-500));
                resolve(null);
                return;
            }

            // Extract the assistant's last response from stdout
            const response = extractResponse(stdout);
            resolve(response);
        });

        proc.on('error', (err) => {
            console.error('Failed to spawn gptme:', err);
            resolve(null);
        });

        // Timeout: 120 seconds
        setTimeout(() => {
            proc.kill('SIGTERM');
            console.error('gptme timed out');
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
