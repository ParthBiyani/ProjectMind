/*
 * Pure logic shared by the extension's scripts, kept free of browser APIs so it
 * can be tested under Node (`node --test extension/test`).
 *
 * The rules here mirror src/projectmind/sources/browser.py and
 * src/projectmind/capture.py. The daemon applies them again on every request, so
 * this file is a convenience and a courtesy, never the only line of defence.
 */
(function (root) {
  'use strict';

  const DEFAULT_PORT = 47615;
  const DWELL_SECONDS = 20;
  const MAX_TEXT = 50000;
  const QUEUE_CAP = 200;
  const BATCH = 20;

  function hostMatches(host, pattern) {
    const h = String(host).toLowerCase();
    const p = String(pattern).toLowerCase().replace(/^www\./, '');
    if (p.startsWith('*.')) {
      const suffix = p.slice(1);
      return h.endsWith(suffix) && h !== suffix.slice(1);
    }
    return h === p || h.endsWith('.' + p);
  }

  function permits(host, allow, deny) {
    const h = String(host).toLowerCase().replace(/^www\./, '');
    if ((deny || []).some((d) => hostMatches(h, d))) return false;
    return (allow || []).some((a) => hostMatches(h, a));
  }

  // Page identity: no query string, no fragment (no tokens, no session ids).
  function cleanUrl(raw) {
    let url;
    try {
      url = new URL(raw);
    } catch (_) {
      return null;
    }
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return null;
    const cleaned = url.protocol + '//' + url.hostname + url.pathname;
    return cleaned.replace(/\/+$/, '') || null;
  }

  function clip(text, max) {
    const s = String(text || '').trim();
    return s.length > max ? s.slice(0, max) : s;
  }

  // Offline queue: newest wins when the cap is hit.
  function enqueue(queue, item, cap) {
    const next = queue.concat([item]);
    const limit = cap || QUEUE_CAP;
    return next.length > limit ? next.slice(next.length - limit) : next;
  }

  function takeBatch(queue, size) {
    const n = size || BATCH;
    return { batch: queue.slice(0, n), rest: queue.slice(n) };
  }

  // Of several candidate blocks, the one most likely to be the article: lots of
  // text, few links. Navigation, footers and link lists lose.
  function bestBlock(blocks) {
    let best = null;
    let bestScore = 0;
    for (const block of blocks) {
      const length = (block.text || '').length;
      if (length === 0) continue;
      const density = Math.min(1, (block.linkChars || 0) / length);
      const score = length * (1 - density);
      if (score > bestScore) {
        best = block;
        bestScore = score;
      }
    }
    return best;
  }

  // A pattern the manifest can use to request access to an allowed domain.
  function matchPattern(domain) {
    const bare = String(domain).replace(/^\*\./, '').replace(/^www\./, '');
    return 'https://*.' + bare + '/*';
  }

  const api = {
    DEFAULT_PORT,
    DWELL_SECONDS,
    MAX_TEXT,
    QUEUE_CAP,
    BATCH,
    hostMatches,
    permits,
    cleanUrl,
    clip,
    enqueue,
    takeBatch,
    bestBlock,
    matchPattern,
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  } else {
    root.PM = api;
  }
})(typeof self !== 'undefined' ? self : globalThis);
