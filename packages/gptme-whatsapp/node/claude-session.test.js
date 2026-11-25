import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, mkdirSync, writeFileSync } from 'fs';
import { tmpdir } from 'os';
import { join } from 'path';

import { uuidv5, sessionIdFor, sessionName, sessionExists, sessionArgs } from './claude-session.js';

test('uuidv5 matches the RFC 4122 reference vector', () => {
    // Python: uuid.uuid5(uuid.NAMESPACE_URL, "https://example.com")
    assert.equal(uuidv5('https://example.com'), '4fd35a71-71ef-5a55-a9d9-aa75c889a6d0');
});

test('session id is a stable v5 UUID per conversation name', () => {
    const id = sessionIdFor('whatsapp-sven-46700000000-c-us');
    assert.equal(id, sessionIdFor('whatsapp-sven-46700000000-c-us'));
    assert.notEqual(id, sessionIdFor('whatsapp-sven-46700000001-c-us'));
    assert.match(id, /^[0-9a-f]{8}-[0-9a-f]{4}-5[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
});

test('the agent workspace is part of the conversation name', () => {
    const sender = '46700000000@c.us';
    const name = sessionName('sven', '/home/sven/sven', sender);
    assert.equal(name, sessionName('sven', '/home/sven/sven', sender));
    assert.equal(name, 'whatsapp-sven--home-sven-sven-46700000000-c-us');
    // sessionExists scans every project dir, so a moved workspace must not
    // resume the conversation stored under the old one.
    assert.notEqual(
        sessionIdFor(name),
        sessionIdFor(sessionName('sven', '/home/sven/other', sender)),
    );
});

test('first message creates the session, later messages resume it', () => {
    const config = mkdtempSync(join(tmpdir(), 'claude-config-'));
    const conv = 'whatsapp-sven-46700000000-c-us';
    const id = sessionIdFor(conv);

    // No projects dir yet: a fresh install must not crash.
    assert.equal(sessionExists(id, config), false);
    assert.deepEqual(sessionArgs(conv, config), ['--session-id', id]);

    const project = join(config, 'projects', '-home-sven-sven');
    mkdirSync(project, { recursive: true });
    writeFileSync(join(project, `${id}.jsonl`), '{}\n');
    assert.equal(sessionExists(id, config), true);
    assert.deepEqual(sessionArgs(conv, config), ['--resume', id]);
});
