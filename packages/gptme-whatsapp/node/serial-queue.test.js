import { test } from 'node:test';
import assert from 'node:assert/strict';

import { createKeyedQueue } from './serial-queue.js';

/** A task that records start/end around a controllable delay. */
function tracker(events, name, ms) {
    return async () => {
        events.push(`start:${name}`);
        await new Promise((resolve) => setTimeout(resolve, ms));
        events.push(`end:${name}`);
        return name;
    };
}

test('tasks with the same key run one at a time, in order', async () => {
    const queue = createKeyedQueue();
    const events = [];

    const results = await Promise.all([
        queue.run('sender', tracker(events, 'a', 20)),
        queue.run('sender', tracker(events, 'b', 1)),
    ]);

    assert.deepEqual(events, ['start:a', 'end:a', 'start:b', 'end:b']);
    assert.deepEqual(results, ['a', 'b']);
});

test('tasks with different keys run concurrently', async () => {
    const queue = createKeyedQueue();
    const events = [];

    await Promise.all([
        queue.run('one', tracker(events, 'a', 20)),
        queue.run('two', tracker(events, 'b', 20)),
    ]);

    // Both started before either finished: no cross-sender serialization.
    assert.deepEqual(events.slice(0, 2), ['start:a', 'start:b']);
});

test('a rejected task does not block the next task for that key', async () => {
    const queue = createKeyedQueue();

    const failing = queue.run('sender', async () => {
        throw new Error('boom');
    });
    await assert.rejects(failing, /boom/);

    assert.equal(await queue.run('sender', async () => 'ok'), 'ok');
});

test('a key is released once its tasks settle', async () => {
    const queue = createKeyedQueue();
    await queue.run('sender', async () => 'ok');
    // Release happens one microtask after the caller's continuation.
    await new Promise((resolve) => setImmediate(resolve));
    assert.equal(queue.size(), 0);
});
