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

  const LOCAL_SUFFIXES = ['.local', '.localhost', '.internal', '.lan', '.home.arpa'];

  // This machine or the local network: never read, whatever the list says.
  function isLocal(host) {
    const h = String(host || '').toLowerCase().replace(/^\[|\]$/g, '').replace(/\.$/, '');
    if (!h || h === 'localhost') return true;
    if (h.indexOf('.') === -1 && h.indexOf(':') === -1) return true;
    if (LOCAL_SUFFIXES.some((s) => h.endsWith(s))) return true;
    const v4 = h.match(/^(\d+)\.(\d+)\.(\d+)\.(\d+)$/);
    if (v4) {
      const [a, b] = [Number(v4[1]), Number(v4[2])];
      return a === 10 || a === 127 || (a === 172 && b >= 16 && b <= 31) ||
        (a === 192 && b === 168) || (a === 169 && b === 254) || a === 0;
    }
    if (h.indexOf(':') !== -1) return h === '::1' || /^f[cd]/.test(h) || /^fe80/.test(h);
    return false;
  }

  // A host that exists to sign you in (login.*, accounts.*, *.auth.*, id.*): never read, on
  // any site. Mirrors is_signin_host in browser.py.
  const SIGNIN_LABELS = ['login', 'signin', 'sign-in', 'sso', 'auth', 'authn', 'oauth', 'account',
    'accounts', 'identity', 'secure'];
  const SIGNIN_PREFIXES = ['login-', 'signin-', 'static-login', 'authentication', 'authn-', 'sso-',
    'secure-'];

  function isSigninHost(host) {
    const labels = String(host || '').toLowerCase().replace(/^\.+|\.+$/g, '').split('.');
    if (labels[0] === 'id' && labels.length > 2) return true;
    const front = labels.length > 2 ? labels.slice(0, -2) : labels.slice(0, 1);
    return front.some((l) => SIGNIN_LABELS.indexOf(l) !== -1 ||
      SIGNIN_PREFIXES.some((p) => l.startsWith(p)));
  }

  // Everything except the never-capture list. `deny` undefined means the list has
  // not arrived from the daemon yet, and then nothing is read (fail closed).
  function permits(host, deny) {
    if (!Array.isArray(deny)) return false;
    const h = String(host).toLowerCase().replace(/^www\./, '');
    if (isLocal(h) || isSigninHost(h)) return false;
    return !deny.some((d) => hostMatches(h, d));
  }

  // AI chat sites are read only on their conversation pages: never a login, settings,
  // upgrade or payment page of the same site. Mirrors CHAT_SITES in browser.py.
  const CHAT_ID = '[\\w%.:~-]+';
  const CHAT_SITES = {
    'chatgpt.com': [`^/c/${CHAT_ID}$`, `^/g/${CHAT_ID}/c/${CHAT_ID}$`, `^/share/${CHAT_ID}$`],
    'chat.openai.com': [`^/c/${CHAT_ID}$`, `^/share/${CHAT_ID}$`],
    'claude.ai': [`^/chat/${CHAT_ID}$`, `^/share/${CHAT_ID}$`],
    'gemini.google.com': [`^/app/${CHAT_ID}$`, `^/share/${CHAT_ID}$`],
  };

  function chatPathAllowed(host, path) {
    const clean = String(path || '').replace(/\/+$/, '') || '/';
    for (const site of Object.keys(CHAT_SITES)) {
      if (hostMatches(host, site)) return CHAT_SITES[site].some((p) => new RegExp(p).test(clean));
    }
    return true;
  }

  // A whole address: the site is allowed and, on a chat site, it is a conversation.
  function permitsPage(rawUrl, deny) {
    let url;
    try {
      url = new URL(rawUrl);
    } catch (_) {
      return false;
    }
    const host = url.hostname.toLowerCase().replace(/^www\./, '');
    return permits(host, deny) && chatPathAllowed(host, url.pathname);
  }

  // A typed address such as www.Kite.com/login -> kite.com; null if it is not a domain.
  function cleanDomain(raw) {
    let text = String(raw || '').trim().toLowerCase();
    if (text.indexOf('://') !== -1) {
      try { text = new URL(text).hostname; } catch (_) { return null; }
    }
    text = text.split('/')[0].replace(/^\*\./, '').replace(/^www\./, '').replace(/^\.+|\.+$/g, '');
    if (!text || /\s/.test(text) || text.indexOf('.') === -1) return null;
    return text;
  }

  // A page asking for a password or a card number is not read, whatever the site.
  function looksSensitive(doc) {
    return Boolean(doc.querySelector(
      'input[type="password"], input[autocomplete^="cc-"], input[name*="card" i][name*="number" i]'));
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

  const api = {
    DEFAULT_PORT,
    DWELL_SECONDS,
    MAX_TEXT,
    QUEUE_CAP,
    BATCH,
    hostMatches,
    isLocal,
    isSigninHost,
    permits,
    permitsPage,
    chatPathAllowed,
    cleanDomain,
    looksSensitive,
    cleanUrl,
    clip,
    enqueue,
    takeBatch,
    bestBlock,
  };

  if (typeof module !== 'undefined' && module.exports) {
    module.exports = api;
  } else {
    root.PM = api;
  }
})(typeof self !== 'undefined' ? self : globalThis);
