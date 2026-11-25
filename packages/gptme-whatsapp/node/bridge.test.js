import { test } from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { readFileSync, mkdtempSync, mkdirSync, appendFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import vm from 'node:vm';
import { gptmeLogsDir, runWithReply } from './gptme-log.js';

// Exercise the actual bridge functions without installing Chromium or contacting
// WhatsApp. Only imports are replaced; callGptme/spawnAndCapture run unchanged.
function bridge(env = {}) {
    const logs = mkdtempSync(join(tmpdir(), 'whatsapp-bridge-'));
    const environment = { HOME: logs, GPTME_AGENT: 'test', GPTME_LOGS_HOME: logs, ...env };
    const children = [];
    const timers = new Set();
    const source = readFileSync(new URL('./index.js', import.meta.url), 'utf8')
        .replace(/^import .*;\n/gm, '');
    class Client {
        on() {}
        initialize() {}
    }
    const context = vm.createContext({
        pkg: { Client, LocalAuth: class {} },
        qrcode: {},
        console: { log() {}, error() {} },
        process: { env: environment, on() {} },
        existsSync: () => true,
        gptmeLogsDir: (env, cwd) => gptmeLogsDir(env, cwd),
        runWithReply: (name, generate, dir = gptmeLogsDir(environment)) => runWithReply(name, generate, dir),
        setTimeout: (callback) => { timers.add(callback); return callback; },
        clearTimeout: (timer) => timers.delete(timer),
        spawn: (cmd, args, options) => {
            const child = new EventEmitter();
            child.stdin = { end() {} };
            child.stdout = new EventEmitter();
            child.stderr = new EventEmitter();
            child.kill = (signal) => { child.signal = signal; };
            const name = args[args.indexOf('--name') + 1];
            // Model the child's cwd, independently of the extraction helper.
            const dir = resolve(options.cwd, environment.GPTME_LOGS_HOME
                || join(environment.XDG_DATA_HOME, 'gptme', 'logs'));
            mkdirSync(join(dir, name), { recursive: true });
            child.answer = (content) => appendFileSync(join(dir, name, 'conversation.jsonl'),
                JSON.stringify({ role: 'assistant', content }) + '\n');
            children.push(child);
            return child;
        },
    });
    vm.runInContext(source, context);
    return { context, children, timers };
}

const tick = () => new Promise((resolve) => setImmediate(resolve));

test('callGptme serializes subprocesses and extracts the matching replies', async () => {
    const { context, children, timers } = bridge();
    const first = context.callGptme('123', 'first');
    const second = context.callGptme('123', 'second');
    await tick();
    assert.equal(children.length, 1);
    children[0].answer('first answer');
    children[0].emit('close', 0);
    assert.equal(await first, 'first answer');
    await tick();
    assert.equal(children.length, 2);
    children[1].answer('second answer');
    children[1].emit('close', 0);
    assert.equal(await second, 'second answer');
    assert.equal(timers.size, 0);
});

test('a timed-out child keeps the queue locked until close and sends no partial reply', async () => {
    const { context, children, timers } = bridge();
    const first = context.callGptme('123', 'first');
    const second = context.callGptme('123', 'second');
    await tick();
    children[0].answer('partial answer');
    [...timers][0]();
    assert.equal(children[0].signal, 'SIGKILL');
    await tick();
    assert.equal(children.length, 1);
    children[0].emit('close', null);
    assert.equal(await first, null);
    await tick();
    assert.equal(children.length, 2);
    children[1].answer('next answer');
    children[1].emit('close', 0);
    assert.equal(await second, 'next answer');
    assert.equal(timers.size, 0);
});

test('relative logs home is read from the subprocess workspace', async () => {
    const workspace = mkdtempSync(join(tmpdir(), 'whatsapp-agent-'));
    const { context, children } = bridge({ AGENT_WORKSPACE: workspace, GPTME_LOGS_HOME: 'logs' });
    const reply = context.callGptme('123', 'hi');
    await tick();
    children[0].answer('workspace reply');
    children[0].emit('close', 0);
    assert.equal(await reply, 'workspace reply');
});

test('spawn failure clears the timer and releases the conversation', async () => {
    const { context, children, timers } = bridge();
    const first = context.callGptme('123', 'first');
    await tick();
    children[0].emit('error', new Error('ENOENT'));
    assert.equal(await first, null);
    assert.equal(timers.size, 0);
    const second = context.callGptme('123', 'second');
    await tick();
    children[1].answer('recovered');
    children[1].emit('close', 0);
    assert.equal(await second, 'recovered');
});
