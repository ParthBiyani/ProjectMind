'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const PM = require('../lib.js');

const DENY = ['kite.com', 'fyers.in', 'mail.google.com'];

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

test('permits: every site except the never-capture list', () => {
  assert.ok(PM.permits('www.github.com', DENY));
  assert.ok(PM.permits('some-blog.example.org', DENY));
  assert.ok(!PM.permits('kite.com', DENY));
  assert.ok(!PM.permits('www.kite.com', DENY));
  assert.ok(!PM.permits('trade.fyers.in', DENY));
  assert.ok(!PM.permits('mail.google.com', DENY));
  assert.ok(PM.permits('notkite.com', DENY));
});

test('permits: nothing is read before the list has arrived', () => {
  assert.ok(!PM.permits('github.com', undefined));
  assert.ok(PM.permits('github.com', []));
});

test('permits: this machine and the local network are never read', () => {
  for (const host of ['localhost', '127.0.0.1', '192.168.1.5', '10.0.0.2', '172.20.1.1',
    '169.254.0.1', 'router', 'nas.local', 'app.localhost', '[::1]']) {
    assert.ok(!PM.permits(host, []), host);
  }
  assert.ok(PM.permits('8.8.8.8', []));
  assert.ok(PM.permits('172.32.0.1', []));
});

test('cleanDomain: what a person types becomes a site name', () => {
  assert.equal(PM.cleanDomain('https://www.Kite.com/login?x=1'), 'kite.com');
  assert.equal(PM.cleanDomain(' fyers.in '), 'fyers.in');
  assert.equal(PM.cleanDomain('*.example.com'), 'example.com');
  assert.equal(PM.cleanDomain('not a domain'), null);
  assert.equal(PM.cleanDomain('localhost'), null);
});

test('looksSensitive: password and card fields', () => {
  const doc = (selector) => ({ querySelector: (q) => (q.includes(selector) ? {} : null) });
  assert.ok(PM.looksSensitive(doc('type="password"')));
  assert.ok(PM.looksSensitive(doc('cc-')));
  assert.ok(!PM.looksSensitive({ querySelector: () => null }));
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

test('the dwell time is long enough that passing through a page sends nothing', () => {
  assert.ok(PM.DWELL_SECONDS >= 15);
  assert.ok(PM.MAX_TEXT <= 50000);
});
