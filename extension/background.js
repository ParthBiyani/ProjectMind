/*
 * The extension's only network code. It talks to one place, the ProjectMind
 * daemon on this computer (127.0.0.1), and sends nothing anywhere else.
 *
 * Failures never lose data silently: captures that cannot be delivered wait in a
 * small queue and are retried when the daemon is back.
 */
importScripts('lib.js');

const PM = self.PM;
const DEFAULTS = { port: PM.DEFAULT_PORT, token: '', paused: false };

async function settings() {
  const stored = await chrome.storage.local.get(Object.keys(DEFAULTS));
  return { ...DEFAULTS, ...stored };
}

async function setStatus(ok, message) {
  await chrome.storage.local.set({ status: { ok, message, at: Date.now() } });
}

async function post(kind, body) {
  const { port, token } = await settings();
  if (!token) {
    await setStatus(false, 'No token yet: paste it in the extension options.');
    return { ok: false, retry: false };
  }
  let response;
  try {
    response = await fetch(`http://127.0.0.1:${port}/capture/${kind}`, {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
  } catch (_) {
    await setStatus(false, 'The ProjectMind daemon is not running (projectmind serve).');
    return { ok: false, retry: true };
  }
  if (response.status === 401) {
    await setStatus(false, 'The daemon rejected the token. Paste it again in the options.');
    return { ok: false, retry: false };
  }
  if (response.status >= 500) {
    await setStatus(false, 'The daemon had a problem; will retry.');
    return { ok: false, retry: true };
  }
  const result = await response.json().catch(() => ({}));
  await setStatus(true, result.stored ? 'Saved.' : `Not saved: ${result.reason || 'refused'}`);
  return { ok: true, result };
}

async function enqueue(item) {
  const { queue = [] } = await chrome.storage.local.get('queue');
  await chrome.storage.local.set({ queue: PM.enqueue(queue, item, PM.QUEUE_CAP) });
}

async function deliver(kind, body) {
  const outcome = await post(kind, body);
  if (!outcome.ok && outcome.retry) await enqueue({ kind, body });
  return outcome;
}

async function flush() {
  const { queue = [] } = await chrome.storage.local.get('queue');
  if (queue.length === 0) return;
  const { batch, rest } = PM.takeBatch(queue, PM.BATCH);
  let remaining = rest;
  for (let i = 0; i < batch.length; i += 1) {
    const outcome = await post(batch[i].kind, batch[i].body);
    if (!outcome.ok && outcome.retry) {
      remaining = batch.slice(i).concat(rest); // daemon still down: keep the rest in order
      break;
    }
  }
  await chrome.storage.local.set({ queue: remaining });
}

// The never-capture list lives in the daemon (one list for the extension, the
// history reader and the CLI). A copy is kept here for the content scripts.
async function syncConfig() {
  const outcome = await post('config', {});
  if (outcome.ok && outcome.result && Array.isArray(outcome.result.deny)) {
    await chrome.storage.local.set({ deny: outcome.result.deny, enabled: outcome.result.enabled });
  }
  return outcome;
}

async function changeDeny(change) {
  const outcome = await post('deny', change);
  if (outcome.ok && outcome.result && Array.isArray(outcome.result.deny)) {
    await chrome.storage.local.set({ deny: outcome.result.deny });
  }
  return outcome;
}

async function snapshotTabs() {
  const { paused } = await settings();
  if (paused) return;
  const { deny } = await chrome.storage.local.get('deny');
  const tabs = await chrome.tabs.query({});
  const open = tabs
    .filter((tab) => tab.url && PM.cleanUrl(tab.url) && PM.permits(new URL(tab.url).hostname, deny))
    .map((tab) => ({ url: PM.cleanUrl(tab.url), title: tab.title || '' }));
  await deliver('tabs', { tabs: open });
}

async function rememberActivePage() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.id) return { ok: false, message: 'No page to remember.' };
  const [{ result } = {}] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    files: ['lib.js', 'extract.js'],
  }).then(() => chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func: () => globalThis.__pmExtract(),
  }));
  if (!result || !result.text) return { ok: false, message: 'This page has no readable text.' };
  const outcome = await post('page', { ...result, explicit: true });
  const stored = outcome.ok && outcome.result && outcome.result.stored;
  return { ok: stored, message: stored ? 'Remembered.' : 'Not saved (see the status line).' };
}

chrome.runtime.onMessage.addListener((message, _sender, reply) => {
  (async () => {
    if (message.kind === 'page') {
      const { paused } = await settings();
      if (!paused) await deliver('page', message);
      reply({ ok: true });
    } else if (message.kind === 'remember') {
      reply(await rememberActivePage());
    } else if (message.kind === 'test') {
      reply(await syncConfig());
    } else if (message.kind === 'deny') {
      reply(await changeDeny({ add: message.add, remove: message.remove }));
    } else {
      reply({ ok: false });
    }
  })();
  return true; // answer asynchronously
});

// Alarms are not guaranteed to survive a browser restart, so they are created on
// every start as well as on install (creating one that exists just replaces it).
function start() {
  chrome.alarms.create('flush', { periodInMinutes: 1 });
  chrome.alarms.create('tabs', { periodInMinutes: 2 });
  syncConfig().then(flush);
}

chrome.runtime.onInstalled.addListener(start);
chrome.runtime.onStartup.addListener(start);

chrome.alarms.onAlarm.addListener((alarm) => {
  if (alarm.name === 'flush') flush();
  if (alarm.name === 'tabs') syncConfig().then(snapshotTabs);
});
