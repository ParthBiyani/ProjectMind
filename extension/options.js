'use strict';

const token = document.getElementById('token');
const port = document.getElementById('port');
const result = document.getElementById('result');
const denyInput = document.getElementById('deny-input');
const denyList = document.getElementById('deny-list');
const denyResult = document.getElementById('deny-result');
const filter = document.getElementById('filter');

function show(element, text, ok) {
  element.textContent = text;
  element.className = ok ? 'ok' : 'bad';
}

async function load() {
  const stored = await chrome.storage.local.get(['token', 'port']);
  token.value = stored.token || '';
  port.value = stored.port || globalThis.PM.DEFAULT_PORT;
}

async function save() {
  await chrome.storage.local.set({ token: token.value.trim(), port: Number(port.value) });
  show(result, 'Saved.', true);
}

async function renderDeny() {
  const { deny } = await chrome.storage.local.get('deny');
  const wanted = filter.value.trim().toLowerCase();
  denyList.replaceChildren();
  if (!Array.isArray(deny)) {
    show(denyResult, 'The list arrives from ProjectMind once the connection works.', false);
    return;
  }
  for (const domain of deny.filter((d) => !wanted || d.includes(wanted))) {
    const item = document.createElement('li');
    const remove = document.createElement('button');
    remove.textContent = '×';
    remove.title = `Read ${domain} again`;
    remove.addEventListener('click', () => change({ remove: domain }));
    item.append(domain, remove);
    denyList.append(item);
  }
  if (!denyResult.textContent) denyResult.textContent = `${deny.length} sites are never read.`;
}

async function change(edit) {
  const reply = await chrome.runtime.sendMessage({ kind: 'deny', ...edit });
  if (!reply || !reply.ok || !reply.result || !reply.result.ok) {
    const reason = reply && reply.result && reply.result.reason;
    show(denyResult, reason ? `Not changed: ${reason}.` : 'Could not reach ProjectMind.', false);
    return;
  }
  const forgotten = reply.result.forgotten ? ` Forgot ${reply.result.forgotten} pages already read.` : '';
  show(denyResult, edit.add ? `Added.${forgotten}` : `${edit.remove} will be read again.`, true);
  denyInput.value = '';
  renderDeny();
}

document.getElementById('save').addEventListener('click', save);

document.getElementById('test').addEventListener('click', async () => {
  await save();
  show(result, 'Testing…', true);
  const reply = await chrome.runtime.sendMessage({ kind: 'test' });
  if (!reply || !reply.ok) {
    const { status } = await chrome.storage.local.get('status');
    show(result, status ? status.message : 'Could not reach the daemon.', false);
    return;
  }
  const config = reply.result;
  show(result, config.enabled ? 'Connected. Capture is on.' : 'Connected, but capture is off (projectmind browser enable).', config.enabled);
  renderDeny();
});

document.getElementById('deny-add').addEventListener('click', () => {
  const domain = globalThis.PM.cleanDomain(denyInput.value);
  if (!domain) {
    show(denyResult, 'That is not a site name.', false);
    return;
  }
  change({ add: domain });
});
denyInput.addEventListener('keydown', (event) => {
  if (event.key === 'Enter') document.getElementById('deny-add').click();
});
filter.addEventListener('input', renderDeny);

load();
chrome.runtime.sendMessage({ kind: 'test' }).finally(renderDeny);
