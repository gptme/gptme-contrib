import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, appendFileSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';

import { gptmeLogsDir, logLineCount, replySince, runWithReply } from './gptme-log.js';

const row = (role, content) => JSON.stringify({ role, content, timestamp: '2026-11-24T13:00:00' }) + '\n';

function setup() {
    const logs = mkdtempSync(join(tmpdir(), 'gptme-logs-'));
    const conv = 'whatsapp-sven-46700000000-c-us';
    mkdirSync(join(logs, conv));
    return { logs, conv, file: join(logs, conv, 'conversation.jsonl') };
}

test('logs dir follows GPTME_LOGS_HOME, then XDG_DATA_HOME', () => {
    assert.equal(gptmeLogsDir({ GPTME_LOGS_HOME: '/x/logs' }), '/x/logs');
    assert.equal(gptmeLogsDir({ XDG_DATA_HOME: '/d' }), '/d/gptme/logs');
});

test('reply is the last assistant text of this run, without tool calls', () => {
    const { logs, conv, file } = setup();
    writeFileSync(file, row('system', 'prompt') + row('user', 'old') + row('assistant', 'old reply'));
    const since = logLineCount(conv, logs);
    assert.equal(since, 3);
    appendFileSync(file,
        row('user', 'what is 2+2?')
        + row('assistant', 'Let me check.\n@shell(call_1): {"command": "echo 4"}')
        + row('system', 'Ran command: echo 4\n4')
        + row('assistant', 'It is 4.\n@complete'));
    assert.equal(replySince(conv, since, logs), 'It is 4.');
});

test('a run with no assistant text does not resend the previous reply', () => {
    const { logs, conv, file } = setup();
    writeFileSync(file, row('user', 'hi') + row('assistant', 'old reply'));
    const since = logLineCount(conv, logs);
    appendFileSync(file, row('user', 'hello again') + row('assistant', '@complete') + 'not json\n');
    assert.equal(replySince(conv, since, logs), null);
});

test('ordinary contact handles survive reply extraction', () => {
    const { logs, conv, file } = setup();
    writeFileSync(file, row('assistant', 'Contact support:\n@helpdesk'));
    assert.equal(replySince(conv, 0, logs), 'Contact support:\n@helpdesk');
    appendFileSync(file, row('assistant', '@helpdesk'));
    assert.equal(replySince(conv, 1, logs), '@helpdesk');
});

test('overlapping runs for one conversation read only their own reply', async () => {
    const { logs, conv, file } = setup();
    let finishFirst;
    let firstStarted;
    const started = new Promise((resolve) => { firstStarted = resolve; });
    const held = new Promise((resolve) => { finishFirst = resolve; });
    const first = runWithReply(conv, async () => {
        appendFileSync(file, row('assistant', 'first answer'));
        firstStarted();
        await held;
    }, logs);
    await started;
    let secondStarted = false;
    const second = runWithReply(conv, async () => {
        secondStarted = true;
        appendFileSync(file, row('assistant', 'second answer'));
    }, logs);
    await new Promise((resolve) => setImmediate(resolve));
    const overlapped = secondStarted;
    finishFirst();
    assert.deepEqual(await Promise.all([first, second]), ['first answer', 'second answer']);
    assert.equal(overlapped, false);
});

test('other conversations run independently and a failed run does not block the queue', async () => {
    const { logs, conv, file } = setup();
    let release;
    const held = new Promise((resolve) => { release = resolve; });
    const failed = runWithReply(conv, async () => {
        await held;
        throw new Error('generation failed');
    }, logs);
    const rejection = assert.rejects(failed, /generation failed/);
    const next = runWithReply(conv, async () => {
        appendFileSync(file, row('assistant', 'recovered'));
    }, logs);
    const other = await runWithReply('another-contact', async () => 'done', logs);
    assert.equal(other, null);
    release();
    await rejection;
    assert.equal(await next, 'recovered');
    // A subprocess reporting failure must not send its partially written text.
    assert.equal(await runWithReply(conv, async () => {
        appendFileSync(file, row('assistant', 'partial reply'));
        return false;
    }, logs), null);
    assert.equal(await runWithReply(conv, async () => {}, logs), null);
});

test('a missing log (first message, wrong dir) yields no reply', () => {
    const logs = mkdtempSync(join(tmpdir(), 'gptme-logs-'));
    assert.equal(logLineCount('nope', logs), 0);
    assert.equal(replySince('nope', 0, logs), null);
});
