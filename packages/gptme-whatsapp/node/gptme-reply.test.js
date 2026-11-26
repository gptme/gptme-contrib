import { test } from 'node:test';
import assert from 'node:assert/strict';

import { extractReply } from './gptme-reply.js';

const msg = (role, content) => JSON.stringify({ type: 'message', role, content }) + '\n';

test('the reply is the last assistant message of the run', () => {
    assert.equal(extractReply(
        msg('user', 'what is 2+2?')
        + msg('assistant', 'Let me check.\n@shell(call_1): {"command": "echo 4"}')
        + msg('system', 'Ran command: echo 4\n4')
        + msg('assistant', 'It is 4.\n@complete'),
    ), 'It is 4.');
});

test('a tool-only final message falls back to the preceding answer', () => {
    assert.equal(extractReply(
        msg('assistant', 'The answer is 4.') + msg('assistant', '@complete(chatcmpl-tool-1): {}'),
    ), 'The answer is 4.');
    assert.equal(extractReply(msg('assistant', '@complete(chatcmpl-tool-1): {}')), null);
});

test('multiline tool arguments never become a reply', () => {
    const call = '@shell(call_1): ' + JSON.stringify({
        command: 'echo "}"', nested: { values: [1, 2], escaped: '"\\' },
    }, null, 2);
    assert.equal(extractReply(msg('assistant', 'The answer is 4.') + msg('assistant', call)),
        'The answer is 4.');
    assert.equal(extractReply(msg('assistant', `Before.\n${call}\nAfter.\n@complete`)),
        'Before.\n\nAfter.');
});

test('multiple calls, MCP names and incomplete arguments do not leak', () => {
    assert.equal(extractReply(msg('assistant', 'Answer.\n@shell(call_1): {"command":"true"}\n'
        + '@server.tool/name(call-2): {\n "nested": {"key": "value"}\n}\n@complete: {}')), 'Answer.');
    assert.equal(extractReply(msg('assistant', '@shell(call_3): {\n "command": "unfinished')), null);
});

test('tool-call examples in fenced code are preserved', () => {
    const example = 'Example:\n```txt\n@shell(call_1): {\n  "command": "echo 4"\n}\n```';
    assert.equal(extractReply(msg('assistant', example)), example);
});

test('ordinary contact handles survive extraction', () => {
    assert.equal(extractReply(msg('assistant', 'Contact support:\n@helpdesk')),
        'Contact support:\n@helpdesk');
    assert.equal(extractReply(msg('assistant', '@helpdesk')), '@helpdesk');
});

test('no assistant text yields no reply', () => {
    assert.equal(extractReply(''), null);
    assert.equal(extractReply(msg('user', 'hello again')), null);
    assert.equal(extractReply('not json\n' + msg('system', 'Goodbye! (resume with: gptme --name x)')), null);
    assert.equal(extractReply('{"type": "message", "role": "assistant"}\n'), null);
});

test('non-message JSON and malformed lines are ignored', () => {
    assert.equal(extractReply(
        '{"type": "usage", "input_tokens": 3}\nnot json\n' + msg('assistant', 'Fine.'),
    ), 'Fine.');
});
