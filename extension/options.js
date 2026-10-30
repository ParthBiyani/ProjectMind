'use strict';

const token = document.getElementById('token');
const port = document.getElementById('port');
const result = document.getElementById('result');
const rules = document.getElementById('rules');

function show(text, ok) {
  result.textContent = text;
  result.className = ok ? 'ok' : 'bad';
}

async function load() {
  const stored = await chrome.storage.local.get(['token', 'port']);
  token.value = stored.token || '';
  port.value = stored.port || globalThis.PM.DEFAULT_PORT;
}

async function save() {
  await chrome.storage.local.set({ token: token.value.trim(), port: Number(port.value) });
  show('Saved.', true);
}

document.getElementById('save').addEventListener('click', save);

document.getElementById('test').addEventListener('click', async () => {
  await save();
  show('Testing…', true);
  const reply = await chrome.runtime.sendMessage({ kind: 'test' });
  if (!reply || !reply.ok) {
    const { status } = await chrome.storage.local.get('status');
    show(status ? status.message : 'Could not reach the daemon.', false);
    return;
  }
  const config = reply.result;
  show(config.enabled ? 'Connected. Capture is on.' : 'Connected, but capture is off.', config.enabled);
  rules.textContent = config.enabled
    ? `${config.allow.length} allowed sites. Change them with: projectmind browser allow <domain>`
    : 'Turn it on with: projectmind browser enable';
});

load();
