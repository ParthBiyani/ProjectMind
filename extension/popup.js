'use strict';

const statusBox = document.getElementById('status');
const message = document.getElementById('message');
const pauseButton = document.getElementById('pause');

async function render() {
  const { status, paused } = await chrome.storage.local.get(['status', 'paused']);
  pauseButton.textContent = paused ? 'Resume capture' : 'Pause capture';
  if (paused) {
    statusBox.textContent = 'Paused. Nothing is being captured.';
    statusBox.className = '';
  } else if (!status) {
    statusBox.textContent = 'No captures yet.';
    statusBox.className = '';
  } else {
    const when = new Date(status.at).toLocaleTimeString();
    statusBox.textContent = `${status.message} (${when})`;
    statusBox.className = status.ok ? 'ok' : 'bad';
  }
}

document.getElementById('remember').addEventListener('click', async () => {
  message.textContent = 'Saving…';
  const reply = await chrome.runtime.sendMessage({ kind: 'remember' });
  message.textContent = reply && reply.message ? reply.message : 'Done.';
  render();
});

pauseButton.addEventListener('click', async () => {
  const { paused } = await chrome.storage.local.get('paused');
  await chrome.storage.local.set({ paused: !paused });
  render();
});

document.getElementById('never').addEventListener('click', async () => {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const domain = tab && tab.url ? globalThis.PM.cleanDomain(tab.url) : null;
  if (!domain) {
    message.textContent = 'This page has no site to block.';
    return;
  }
  const reply = await chrome.runtime.sendMessage({ kind: 'deny', add: domain });
  const done = reply && reply.ok && reply.result && reply.result.ok;
  message.textContent = done ? `${domain} will never be read.` : 'Could not reach ProjectMind.';
});

document.getElementById('options').addEventListener('click', (event) => {
  event.preventDefault();
  chrome.runtime.openOptionsPage();
});

render();
