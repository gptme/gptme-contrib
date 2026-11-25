import { test } from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { readFileSync, mkdtempSync, mkdirSync, appendFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import vm from 'node:vm';
import { runWithReply } from './gptme-log.js';

// Exercise the actual bridge functions without installing Chromium or contacting
// WhatsApp. Only imports are replaced; callGptme/spawnAndCapture run unchanged.
function bridge() {
    const logs = mkdtempSync(join(tmpdir(), 'whatsapp-bridge-'));
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
        process: { env: { HOME: logs, GPTME_AGENT: 'test' }, on() {} },
        existsSync: () => true,
        runWithReply: (name, generate) => runWithReply(name, generate, logs),
        setTimeout: (callback) => { timers.add(callback); return callback; },
        clearTimeout: (timer) => timers.delete(timer),
        spawn: (cmd, args) => {
            const child = new EventEmitter();
            child.stdin = { end() {} };
            child.stdout = new EventEmitter();
            child.stderr = new EventEmitter();
            child.kill = (signal) => { child.signal = signal; };
            const name = args[args.indexOf('--name') + 1];
            mkdirSync(join(logs, name), { recursive: true });
            child.answer = (content) => appendFileSync(join(logs, name, 'conversation.jsonl'),
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
