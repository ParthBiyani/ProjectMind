/*
 * Runs only on the allow-listed domains (the manifest decides that, not this
 * file). After a page has been in front of you for a while it sends its readable
 * text to the background worker. Reading a page for a few seconds on the way to
 * somewhere else sends nothing.
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

  function send() {
    const key = pageKey();
    if (sentFor === key) return;
    const page = globalThis.__pmExtract();
    if (!page.text || page.text.length < 80) return;
    sentFor = key;
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
