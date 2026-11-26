import { test } from 'node:test';
import assert from 'node:assert/strict';
import { EventEmitter } from 'node:events';
import { spawn } from 'node:child_process';
import {
    readFileSync, mkdtempSync, writeFileSync, existsSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import vm from 'node:vm';
import { extractReply } from './gptme-reply.js';
import { createKeyedQueue } from './serial-queue.js';

// Exercise the actual bridge functions without installing Chromium or contacting
// WhatsApp. Only imports and `process` are replaced; the message handler,
// callGptme and spawnAndCapture run unchanged.
function bridge(env = {}, overrides = {}) {
    const logs = mkdtempSync(join(tmpdir(), 'whatsapp-bridge-'));
    const environment = { HOME: logs, GPTME_AGENT: 'test', GPTME_LOGS_HOME: logs, ...env };
    const children = [];
    const timers = new Set();
    const handlers = {};
    const signals = {};
    const sent = [];
    const source = readFileSync(new URL('./index.js', import.meta.url), 'utf8')
        .replace(/^import .*;\n/gm, '');
    class Client {
        on(event, handler) { handlers[event] = handler; }
        initialize() {}
        async destroy() {}
    }
    const context = vm.createContext({
        pkg: { Client, LocalAuth: class {} },
        qrcode: {},
        console: { log() {}, error() {} },
        process: {
            env: environment,
            platform: 'linux',
            on(signal, handler) { signals[signal] = handler; },
            exit() {},
            kill(pid, signal) { children.find((c) => c.pid === -pid).signal = signal; },
        },
        existsSync: () => true,
        createKeyedQueue,
        extractReply,
        setTimeout: (callback) => { timers.add(callback); return callback; },
        clearTimeout: (timer) => timers.delete(timer),
        spawn: (cmd, args, options) => {
            const child = new EventEmitter();
            child.pid = 1000 + children.length;
            child.detached = options.detached;
            child.stdin = { end() {} };
            child.stdout = new EventEmitter();
            child.stderr = new EventEmitter();
            child.stdout.destroy = () => { child.stdout.destroyed = true; };
            child.stderr.destroy = () => { child.stderr.destroyed = true; };
            child.args = args;
            // The reply arrives on this run's own stdout, as JSONL.
            child.answer = (content) => child.stdout.emit('data',
                JSON.stringify({ type: 'message', role: 'assistant', content }) + '\n');
            children.push(child);
            return child;
        },
        ...overrides,
    });
    vm.runInContext(source, context);
    const receive = (body) => handlers.message({
        from: '123@c.us', body, reply: async (text) => { sent.push(text); },
    });
    return { context, children, timers, signals, sent, receive };
}

const tick = () => new Promise((resolve) => setImmediate(resolve));

test('messages from one sender are serialized and get their own replies', async () => {
    const { children, timers, sent, receive } = bridge();
    const first = receive('first');
    const second = receive('second');
    await tick();
    assert.equal(children.length, 1);
    children[0].answer('first answer');
    children[0].emit('close', 0);
    await first;
    await tick();
    assert.equal(children.length, 2);
    children[1].answer('second answer');
    children[1].emit('close', 0);
    await second;
    assert.deepEqual(sent, ['first answer', 'second answer']);
    assert.equal(timers.size, 0);
});

test('a timed-out run holds the queue until close and sends no partial reply', async () => {
    const { children, timers, sent, receive } = bridge();
    const first = receive('first');
    const second = receive('second');
    await tick();
    children[0].answer('partial answer');
    const [timeout] = timers;
    timeout();
    assert.equal(children[0].detached, true);
    // The group is asked to stop cleanly first, then force-killed if it does not.
    assert.equal(children[0].signal, 'SIGTERM');
    const escalation = [...timers][1];
    escalation();
    assert.equal(children[0].signal, 'SIGKILL');
    await tick();
    assert.equal(children.length, 1);
    children[0].emit('close', null);
    await first;
    await tick();
    assert.equal(children.length, 2);
    children[1].answer('next answer');
    children[1].emit('close', 0);
    await second;
    assert.deepEqual(sent, ['next answer']);
    assert.equal(timers.size, 0);
});

test('a timed-out run that never closes is abandoned after the grace period', async () => {
    const { children, timers, sent, receive } = bridge();
    const first = receive('first');
    const second = receive('second');
    await tick();
    children[0].answer('partial answer');
    const [timeout] = timers;
    timeout();
    // Fire the escalation and then the grace fallback.
    const [, escalation, grace] = [...timers];
    escalation();
    grace();
    await first;
    assert.equal(children[0].stdout.destroyed, true);
    assert.equal(children[0].stderr.destroyed, true);
    await tick();
    assert.equal(children.length, 2);
    children[1].answer('next answer');
    children[1].emit('close', 0);
    await second;
    assert.deepEqual(sent, ['next answer']);
    assert.equal(timers.size, 0);
});

test('a timeout also kills what a wrapper command started', {
    skip: process.platform === 'win32', timeout: 10_000,
}, async (t) => {
    const dir = mkdtempSync(join(tmpdir(), 'whatsapp-wrapper-'));
    const wrapper = join(dir, 'gptme-wrapper');
    const pidFile = join(dir, 'agent.pid');
    // The background sleep inherits stdout/stderr, like gptme under a wrapper.
    writeFileSync(wrapper, '#!/bin/sh\nsleep 30 &\necho $! > agent.pid\nwait\n', { mode: 0o755 });
    const { context, timers } = bridge(
        { AGENT_WORKSPACE: dir, GPTME_CMD: wrapper, PATH: process.env.PATH },
        { spawn, process: {
            env: { HOME: dir, AGENT_WORKSPACE: dir, GPTME_CMD: wrapper, PATH: process.env.PATH },
            platform: process.platform,
            on() {},
            kill: (pid, signal) => process.kill(pid, signal),
        } },
    );
    const agentPid = () => (existsSync(pidFile) ? Number(readFileSync(pidFile, 'utf8')) : 0);
    t.after(() => {
        if (!agentPid()) return;
        try {
            process.kill(agentPid(), 'SIGKILL');
        } catch { /* already gone */ }
    });
    const reply = context.callGptme('123', 'hi');
    while (!agentPid()) await new Promise((resolve) => setTimeout(resolve, 10));
    // Fire only the run timeout: `close` must arrive without the grace fallback.
    [...timers][0]();
    assert.equal(await reply, null);
    assert.equal(timers.size, 0);
});

test('shutdown signals are forwarded to running agent process groups', async () => {
    const { children, signals, receive } = bridge();
    const run = receive('hi');
    await tick();
    for (const signal of ['SIGINT', 'SIGTERM', 'SIGHUP']) {
        signals[signal]();
        assert.equal(children[0].signal, signal);
    }
    children[0].emit('close', null);
    await run;
});

test('gptme runs with structured JSON stdout', async () => {
    const workspace = mkdtempSync(join(tmpdir(), 'whatsapp-agent-'));
    const { context, children } = bridge({ AGENT_WORKSPACE: workspace });
    const reply = context.callGptme('123', 'hi');
    await tick();
    const args = children[0].args;
    assert.equal(args[args.indexOf('--output-format') + 1], 'json');
    children[0].answer('workspace reply');
    children[0].emit('close', 0);
    assert.equal(await reply, 'workspace reply');
});

test('spawn failure clears the timer and releases the conversation', async () => {
    const { children, timers, sent, receive } = bridge();
    const first = receive('first');
    await tick();
    children[0].emit('error', new Error('ENOENT'));
    await first;
    assert.equal(timers.size, 0);
    const second = receive('second');
    await tick();
    children[1].answer('recovered');
    children[1].emit('close', 0);
    await second;
    assert.deepEqual(sent, ['recovered']);
});

test('an abandoned run cannot leak text into the next run\'s reply', async () => {
    const { children, timers, sent, receive } = bridge();
    const first = receive('first');
    const second = receive('second');
    await tick();
    children[0].answer('partial answer');
    const [timeout] = timers;
    timeout();
    const [, escalation, grace] = [...timers];
    escalation();
    grace();
    await first;
    await tick();
    assert.equal(children.length, 2);
    // The abandoned run's stdout is its own: a survivor writing there cannot
    // reach the next run, whose reply comes from its own pipe.
    children[0].answer('stale orphan answer');
    children[1].answer('next answer');
    children[1].emit('close', 0);
    await second;
    assert.deepEqual(sent, ['next answer']);
    assert.equal(timers.size, 0);
});
