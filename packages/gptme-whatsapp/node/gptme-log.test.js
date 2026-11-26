import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync, appendFileSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';

import {
    gptmeLogsDir, logLineCount, replySince, runWithReply, settleAbandonedRun, waitForLogQuiet,
} from './gptme-log.js';

// Runs only accept rows timestamped at or after they started, so rows written
// for a run must be current unless a test deliberately backdates them.
const row = (role, content, timestamp = new Date().toISOString()) => JSON.stringify({ role, content, timestamp }) + '\n';

function setup() {
    const logs = mkdtempSync(join(tmpdir(), 'gptme-logs-'));
    const conv = 'whatsapp-sven-46700000000-c-us';
    mkdirSync(join(logs, conv));
    return { logs, conv, file: join(logs, conv, 'conversation.jsonl') };
}

test('logs dir follows GPTME_LOGS_HOME, then XDG_DATA_HOME', () => {
    assert.equal(gptmeLogsDir({ GPTME_LOGS_HOME: '/x/logs' }), '/x/logs');
    assert.equal(gptmeLogsDir({ XDG_DATA_HOME: '/d' }), '/d/gptme/logs');
    assert.equal(gptmeLogsDir({ GPTME_LOGS_HOME: 'logs' }, '/srv/agent'), '/srv/agent/logs');
    assert.equal(gptmeLogsDir({ XDG_DATA_HOME: 'data' }, '/srv/agent'), '/srv/agent/data/gptme/logs');
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

test('multiline tool arguments never become a reply', () => {
    const { logs, conv, file } = setup();
    const call = '@shell(call_1): ' + JSON.stringify({
        command: 'echo "}"', nested: { values: [1, 2], escaped: '"\\' },
    }, null, 2);
    writeFileSync(file, row('assistant', 'The answer is 4.') + row('assistant', call));
    assert.equal(replySince(conv, 0, logs), 'The answer is 4.');
    assert.equal(replySince(conv, 1, logs), null);
    appendFileSync(file, row('assistant', `Before.\n${call}\nAfter.\n@complete`));
    assert.equal(replySince(conv, 2, logs), 'Before.\n\nAfter.');
});

test('multiple calls, MCP names and incomplete arguments do not leak', () => {
    const { logs, conv, file } = setup();
    writeFileSync(file, row('assistant', 'Answer.\n@shell(call_1): {"command":"true"}\n'
        + '@server.tool/name(call-2): {\n "nested": {"key": "value"}\n}\n@complete: {}'));
    assert.equal(replySince(conv, 0, logs), 'Answer.');
    appendFileSync(file, row('assistant', '@shell(call_3): {\n "command": "unfinished'));
    assert.equal(replySince(conv, 1, logs), null);
});

test('tool-call examples in fenced code are preserved', () => {
    const { logs, conv, file } = setup();
    const example = 'Example:\n```txt\n@shell(call_1): {\n  "command": "echo 4"\n}\n```';
    writeFileSync(file, row('assistant', example));
    assert.equal(replySince(conv, 0, logs), example);
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

test('a run returns only its own reply, and nothing when it failed', async () => {
    const { logs, conv, file } = setup();
    writeFileSync(file, row('assistant', 'old reply'));
    assert.equal(await runWithReply(conv, async () => {
        appendFileSync(file, row('assistant', 'new reply'));
    }, logs), 'new reply');
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

test('a reply from an earlier run appended late is not this run\'s reply', async () => {
    const { logs, conv, file } = setup();
    writeFileSync(file, row('user', 'hi') + row('assistant', 'old reply'));
    const stale = new Date(Date.now() - 60_000).toISOString();
    assert.equal(await runWithReply(conv, async () => {
        // An abandoned run's survivor can append its final text after the next
        // run took its row offset. It was generated before this run started.
        appendFileSync(file, row('assistant', 'stale answer', stale));
        appendFileSync(file, row('assistant', 'fresh answer'));
    }, logs), 'fresh answer');
    // Only stale rows: send nothing rather than the previous run's answer.
    assert.equal(await runWithReply(conv, async () => {
        appendFileSync(file, row('assistant', 'another stale answer', stale));
    }, logs), null);
});

test('an abandoned run settles once its log stops changing', async () => {
    const { logs, conv, file } = setup();
    writeFileSync(file, row('assistant', 'partial answer'));
    assert.equal(await waitForLogQuiet(file, 20, 500), true);
    assert.equal(await settleAbandonedRun(conv, logs, 20, 500), true);
});

test('a log that never stops changing is reported unsettled', async () => {
    const { logs, conv, file } = setup();
    writeFileSync(file, row('assistant', 'partial answer'));
    let writing = true;
    const writer = (async () => {
        while (writing) {
            appendFileSync(file, row('assistant', 'orphan row'));
            await new Promise((done) => setTimeout(done, 20));
        }
    })();
    try {
        assert.equal(await settleAbandonedRun(conv, logs, 20, 200), false);
    } finally {
        writing = false;
        await writer;
    }
});
