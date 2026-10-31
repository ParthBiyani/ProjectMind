# ProjectMind Capture (browser extension)

Sends the readable text of the pages you read, except sites on your never-capture
list, to ProjectMind running **on your own computer**, so that later
"what did I read about X?" has an answer with a source. It works in Microsoft Edge
and Google Chrome (the same extension, Manifest V3).

## What it can and cannot see

- It reads **every site except the never-capture list**. The list starts with
  banking, brokers (Kite/Zerodha, Fyers and others), payments, mail, chat, social media,
  sign-in pages, password managers, government identity sites, health, personal
  documents, cloud consoles and AI chats (`DEFAULT_DENY` in
  [`src/projectmind/sources/browser.py`](../src/projectmind/sources/browser.py)).
  Edit it in the extension's **Options** page, or press **Never capture this site** in
  the toolbar popup. Adding a site also forgets what was already read from it.
- It never reads this computer or your local network (localhost, private addresses,
  `.local` names), never runs in private windows, and skips any page that asks for a
  password or a card number.
- Until it has received the list from the daemon, it reads nothing.
- It sends data to `127.0.0.1` (this computer) and nowhere else. There is no
  analytics and no remote code.
- A page is captured only after you have had it in front of you for 20 seconds, so
  clicking through a page sends nothing.
- URLs are stored without query string or fragment. Text that looks like a
  credential is dropped by the ProjectMind daemon before it is stored.

Everything the extension sends is checked again by the daemon
(`src/projectmind/capture.py`): the extension is a convenience, not the only
safeguard.

## Install (Edge)

1. Start ProjectMind's daemon: `projectmind serve` (or let it start at logon with
   `projectmind schedule install`).
2. Turn capture on: `projectmind browser enable`.
3. Open `edge://extensions`, switch on **Developer mode**, choose **Load
   unpacked** and select this `extension` folder.
4. Get the token: `projectmind daemon token`.
5. Open the extension's **Options**, paste the token, press **Test connection**.
   It should say *Connected. Capture is on.*

## Install (Chrome)

Same, with `chrome://extensions`.

## Using it

- Read as normal. Pages on allowed sites are saved after 20 seconds.
- **Remember this page** (toolbar icon) saves the current page whatever the site,
  except sites you have denied with `projectmind browser deny <domain>`.
- **Pause capture** stops everything until you resume.
- Add a site: `projectmind browser allow docs.example.com`. For the extension to
  read it automatically, also add the site to `host_permissions` and
  `content_scripts.matches` in `manifest.json` and reload the extension; without
  that, use **Remember this page** on it.

## If the daemon is not running

Captures wait in a small queue (200 items) and are delivered when it comes back.
The toolbar popup shows the last result.

## Development

`node --test extension/test/lib.test.js` runs the unit tests for the logic that
does not need a browser. `tests/test_extension.py` checks the manifest as a
security boundary and runs the Node tests.
