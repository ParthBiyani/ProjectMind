'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const PM = require('../lib.js');

const ALLOW = ['docs.python.org', 'github.com', '*.readthedocs.io', 'sqlite.org'];

test('hostMatches: exact, subdomain and wildcard forms', () => {
  assert.ok(PM.hostMatches('docs.python.org', 'docs.python.org'));
  assert.ok(PM.hostMatches('gist.github.com', 'github.com'));
  assert.ok(PM.hostMatches('fastapi.readthedocs.io', '*.readthedocs.io'));
  assert.ok(!PM.hostMatches('readthedocs.io', '*.readthedocs.io'));
});

test('hostMatches: lookalike domains do not match', () => {
  assert.ok(!PM.hostMatches('github.com.evil.example', 'github.com'));
  assert.ok(!PM.hostMatches('notgithub.com', 'github.com'));
});

test('permits: allow-list, www prefix and deny-list precedence', () => {
  assert.ok(PM.permits('www.github.com', ALLOW, []));
  assert.ok(!PM.permits('mail.google.com', ALLOW, []));
  assert.ok(!PM.permits('github.com', ALLOW, ['github.com']));
  assert.ok(!PM.permits('bank.example.com', ALLOW, []));
});

test('cleanUrl: strips query and fragment, refuses non-web schemes', () => {
  assert.equal(
    PM.cleanUrl('https://docs.python.org/3/library/re.html?token=abc#section'),
    'https://docs.python.org/3/library/re.html',
  );
  assert.equal(PM.cleanUrl('https://example.com/'), 'https://example.com');
  assert.equal(PM.cleanUrl('file:///C:/secret.txt'), null);
  assert.equal(PM.cleanUrl('chrome://settings'), null);
  assert.equal(PM.cleanUrl('not a url'), null);
});

test('clip: trims and limits', () => {
  assert.equal(PM.clip('  hello  ', 100), 'hello');
  assert.equal(PM.clip('abcdef', 3), 'abc');
  assert.equal(PM.clip(null, 3), '');
});

test('enqueue keeps the newest items when the cap is hit', () => {
  let queue = [];
  for (let i = 0; i < 5; i += 1) queue = PM.enqueue(queue, i, 3);
  assert.deepEqual(queue, [2, 3, 4]);
});

test('takeBatch splits without losing order', () => {
  const { batch, rest } = PM.takeBatch([1, 2, 3, 4, 5], 2);
  assert.deepEqual(batch, [1, 2]);
  assert.deepEqual(rest, [3, 4, 5]);
});

test('bestBlock prefers prose over link lists', () => {
  const article = { text: 'x'.repeat(2000), linkChars: 100 };
  const menu = { text: 'y'.repeat(2500), linkChars: 2400 };
  assert.equal(PM.bestBlock([menu, article]), article);
  assert.equal(PM.bestBlock([{ text: '', linkChars: 0 }]), null);
});

test('matchPattern builds a manifest pattern for a domain', () => {
  assert.equal(PM.matchPattern('docs.python.org'), 'https://*.docs.python.org/*');
  assert.equal(PM.matchPattern('*.readthedocs.io'), 'https://*.readthedocs.io/*');
  assert.equal(PM.matchPattern('www.github.com'), 'https://*.github.com/*');
});

test('the dwell time is long enough that passing through a page sends nothing', () => {
  assert.ok(PM.DWELL_SECONDS >= 15);
  assert.ok(PM.MAX_TEXT <= 50000);
});
