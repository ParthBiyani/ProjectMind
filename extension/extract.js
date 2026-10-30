/*
 * Reads the readable text of the current page. Injected as a content script on
 * allowed domains, and on demand by "Remember this page".
 *
 * It uses what the browser already renders: `innerText` respects visibility, so
 * hidden menus and collapsed sections are not read. Navigation, headers, footers,
 * forms and scripts are excluded, and the densest block of text with the fewest
 * links wins.
 */
(function () {
  'use strict';

  const NOISE = 'script,style,noscript,template,nav,header,footer,aside,form,iframe,svg,' +
    '[role="navigation"],[role="banner"],[role="contentinfo"],[role="complementary"],' +
    '[aria-hidden="true"]';

  function textWithoutNoise(element) {
    const clone = element.cloneNode(true);
    clone.querySelectorAll(NOISE).forEach((node) => node.remove());
    // cloneNode drops layout, so innerText degrades to textContent; collapse space.
    return (clone.innerText || clone.textContent || '').replace(/[ \t]+\n/g, '\n').replace(/\n{3,}/g, '\n\n').trim();
  }

  function linkChars(element) {
    let total = 0;
    element.querySelectorAll('a').forEach((a) => {
      total += (a.textContent || '').length;
    });
    return total;
  }

  globalThis.__pmExtract = function () {
    const roots = Array.from(document.querySelectorAll('main,article,[role="main"]'));
    if (roots.length === 0 && document.body) roots.push(document.body);
    const blocks = roots.map((element) => ({
      element,
      text: textWithoutNoise(element),
      linkChars: linkChars(element),
    }));
    const pick = globalThis.PM.bestBlock(blocks);
    return {
      title: (document.title || '').trim(),
      url: location.href,
      text: globalThis.PM.clip(pick ? pick.text : '', globalThis.PM.MAX_TEXT),
    };
  };
})();
