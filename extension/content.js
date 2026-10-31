/*
 * Runs on every web page except this machine (the manifest excludes localhost).
 * Before reading anything it checks the never-capture list, which the background
 * worker keeps in sync with the daemon; until that list has arrived it reads
 * nothing. After a page has been in front of you for a while it sends its readable
 * text. Reading a page for a few seconds on the way somewhere else sends nothing,
 * and a page that asks for a password or a card number is never read.
 */
(function () {
  'use strict';

  const PM = globalThis.PM;
  let seconds = 0;
  let sentFor = null;
  let lastKey = pageKey();

  function pageKey() {
    return location.origin + location.pathname;
  }

  function visible() {
    return document.visibilityState === 'visible' && document.hasFocus();
  }

  async function allowedHere() {
    const { deny, paused } = await chrome.storage.local.get(['deny', 'paused']);
    return !paused && PM.permits(location.hostname, deny);
  }

  async function send() {
    const key = pageKey();
    if (sentFor === key) return;
    sentFor = key;
    if (!(await allowedHere()) || PM.looksSensitive(document)) return;
    const page = globalThis.__pmExtract();
    if (!page.text || page.text.length < 80) return;
    chrome.runtime.sendMessage({
      kind: 'page',
      url: page.url,
      title: page.title,
      text: page.text,
      dwell_seconds: seconds,
    }).catch(() => {});
  }

  setInterval(() => {
    const key = pageKey();
    if (key !== lastKey) {
      // Single-page apps change the address without reloading: start again.
      lastKey = key;
      seconds = 0;
      sentFor = null;
    }
    if (!visible()) return;
    seconds += 1;
    if (seconds === PM.DWELL_SECONDS) send();
  }, 1000);
})();
