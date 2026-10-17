// SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
// SPDX-License-Identifier: GPL-3.0-only

// Previews of a Discord ping and of an in-game MOTD, shared by the start page and the Send Ping page.
//   FleetOpsPreview.discord(container, text, {channel, mentions, time})   the ping as Discord would show it
//   FleetOpsPreview.motd(container, html)                                the MOTD as the game would show it
//   FleetOpsPreview.motdColour(value)                                    a game colour (#AARRGGBB) as CSS, or null
//   FleetOpsPreview.skeleton(container, lines)                           grey placeholder lines before an answer
// The renderers build DOM nodes only and never put text into the page as HTML: everything the FC typed (and
// everything a template made of it) arrives as text nodes, and a link is made only for http(s) addresses.
// The look is close to Discord and to the game, not identical (fonts differ).
(() => {
  const LINK = /^https?:\/\//i;

  function clear(container) {
    while (container.firstChild) {
      container.removeChild(container.firstChild);
    }
  }

  function node(tag, className, text) {
    const element = document.createElement(tag);
    if (className) {
      element.className = className;
    }
    if (text !== undefined) {
      element.textContent = text;
    }
    return element;
  }

  function language() {
    return document.documentElement.lang || navigator.language || 'en';
  }

  // --- Discord ----------------------------------------------------------------------------------------

  // <t:UNIX:STYLE> in the page language and the browser's time zone, as Discord shows it to each reader.
  function discordTime(seconds, style) {
    const date = new Date(Number(seconds) * 1000);
    if (Number.isNaN(date.getTime())) {
      return null;
    }
    const lang = language();
    try {
      switch (style) {
        case 't': return new Intl.DateTimeFormat(lang, { hour: '2-digit', minute: '2-digit' }).format(date);
        case 'T': return new Intl.DateTimeFormat(lang, { timeStyle: 'medium' }).format(date);
        case 'd': return new Intl.DateTimeFormat(lang, { dateStyle: 'short' }).format(date);
        case 'D': return new Intl.DateTimeFormat(lang, { dateStyle: 'long' }).format(date);
        case 'F': return new Intl.DateTimeFormat(lang, { dateStyle: 'full', timeStyle: 'short' }).format(date);
        case 'R': return relative(date, lang);
        default: return new Intl.DateTimeFormat(lang, { dateStyle: 'long', timeStyle: 'short' }).format(date);
      }
    } catch (error) {
      return date.toISOString();
    }
  }

  function relative(date, lang) {
    const formatter = new Intl.RelativeTimeFormat(lang, { numeric: 'auto' });
    const seconds = Math.round((date.getTime() - Date.now()) / 1000);
    const steps = [['day', 86400], ['hour', 3600], ['minute', 60]];
    for (const [unit, size] of steps) {
      if (Math.abs(seconds) >= size) {
        return formatter.format(Math.round(seconds / size), unit);
      }
    }
    return formatter.format(seconds, 'second');
  }

  // One alternative per kind of inline text, the earliest in the line wins. Code first, so its content stays plain.
  const INLINE = new RegExp([
    '(`+)([^`]|[^`][\\s\\S]*?[^`])\\1(?!`)',            // 1 backticks, 2 code
    '\\*\\*(\\S|\\S[\\s\\S]*?\\S)\\*\\*',                // 3 bold
    '__(\\S|\\S[\\s\\S]*?\\S)__',                        // 4 underline
    '~~(\\S|\\S[\\s\\S]*?\\S)~~',                        // 5 strike
    '\\*(\\S|\\S[^*]*?\\S)\\*(?!\\*)',                   // 6 italic
    '\\b_(\\S|\\S[^_]*?\\S)_\\b',                        // 7 italic
    '\\[([^\\]\\n]+)\\]\\(([^\\s)]+)\\)',                // 8 label, 9 address
    '<t:(-?\\d+)(?::([tTdDfFR]))?>',                     // 10 seconds, 11 style
    '(@everyone|@here)',                                 // 12 mass mention
  ].join('|'), 'g');

  function mentionPattern(mentions) {
    const names = (mentions || []).filter(Boolean).sort((a, b) => b.length - a.length);
    if (!names.length) {
      return null;
    }
    return new RegExp(names.map((name) => name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|'), 'g');
  }

  // Plain text with the labels of the ticked ping targets marked as mentions.
  function plain(parent, text, mentions) {
    if (!text) {
      return;
    }
    const pattern = mentionPattern(mentions);
    if (!pattern) {
      parent.appendChild(document.createTextNode(text));
      return;
    }
    let last = 0;
    for (const found of text.matchAll(pattern)) {
      if (found.index > last) {
        parent.appendChild(document.createTextNode(text.slice(last, found.index)));
      }
      parent.appendChild(node('span', 'fo-discord-mention', found[0]));
      last = found.index + found[0].length;
    }
    if (last < text.length) {
      parent.appendChild(document.createTextNode(text.slice(last)));
    }
  }

  function inline(parent, text, mentions, depth) {
    let last = 0;
    INLINE.lastIndex = 0;
    for (const found of text.matchAll(INLINE)) {
      if (found.index > last) {
        plain(parent, text.slice(last, found.index), mentions);
      }
      last = found.index + found[0].length;
      const [, , code, bold, underline, strike, italicStar, italicUnderscore, label, address, seconds, style, mass] = found;
      if (code !== undefined) {
        parent.appendChild(node('code', '', code));
      } else if (depth < 4 && (bold !== undefined || underline !== undefined || strike !== undefined || italicStar !== undefined || italicUnderscore !== undefined)) {
        const tag = bold !== undefined ? 'strong' : underline !== undefined ? 'u' : strike !== undefined ? 's' : 'em';
        const inner = bold ?? underline ?? strike ?? italicStar ?? italicUnderscore;
        const element = node(tag);
        inline(element, inner, mentions, depth + 1);
        parent.appendChild(element);
      } else if (label !== undefined) {
        if (LINK.test(address)) {
          const link = node('a');
          link.href = address;
          link.rel = 'noopener noreferrer';
          link.target = '_blank';
          link.textContent = label;
          parent.appendChild(link);
        } else {
          parent.appendChild(document.createTextNode(found[0]));
        }
      } else if (seconds !== undefined) {
        const shown = discordTime(seconds, style || 'f');
        parent.appendChild(shown === null ? document.createTextNode(found[0]) : node('span', 'fo-discord-time', shown));
      } else if (mass !== undefined) {
        parent.appendChild(node('span', 'fo-discord-mention', mass));
      } else {
        parent.appendChild(document.createTextNode(found[0]));
      }
    }
    if (last < text.length) {
      plain(parent, text.slice(last), mentions);
    }
  }

  // One line of a message: a heading, a quote, small text or a paragraph.
  function line(text, mentions) {
    let found = /^(#{1,3}) (.*)$/.exec(text);
    if (found) {
      const heading = node(`h${found[1].length}`);
      inline(heading, found[2], mentions, 0);
      return heading;
    }
    found = /^-# (.*)$/.exec(text);
    if (found) {
      const small = node('div', 'fo-discord-sub');
      inline(small, found[1], mentions, 0);
      return small;
    }
    found = /^> ?(.*)$/.exec(text);
    if (found) {
      const quote = node('div', 'fo-discord-quote');
      inline(quote, found[1], mentions, 0);
      return quote;
    }
    const paragraph = node('div');
    if (text === '') {
      paragraph.appendChild(document.createElement('br'));
    } else {
      inline(paragraph, text, mentions, 0);
    }
    return paragraph;
  }

  function discord(container, text, options) {
    const opts = options || {};
    clear(container);
    const avatar = node('div', 'fo-discord-avatar');
    avatar.setAttribute('aria-hidden', 'true');
    const icon = node('i', 'fa-solid fa-robot');
    avatar.appendChild(icon);
    const body = node('div');
    const head = node('div', 'fo-discord-head');
    head.appendChild(node('strong', '', 'FleetOps'));
    if (opts.channel) {
      head.appendChild(node('span', 'fo-discord-sub', `#${String(opts.channel).replace(/^#/, '')}`));
    }
    head.appendChild(node('span', 'fo-discord-sub', opts.time || new Intl.DateTimeFormat(language(), { hour: '2-digit', minute: '2-digit' }).format(new Date())));
    body.appendChild(head);
    String(text || '').split(/\r\n|\r|\n/).forEach((raw) => body.appendChild(line(raw, opts.mentions)));
    container.appendChild(avatar);
    container.appendChild(body);
  }

  // --- The game's MOTD --------------------------------------------------------------------------------

  // A game colour is #AARRGGBB (alpha first) or #RRGGBB; anything else is ignored.
  function motdColour(value) {
    const found = /^#([0-9a-f]{2})?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})$/i.exec(String(value || '').trim());
    if (!found) {
      return null;
    }
    const alpha = found[1] === undefined ? 1 : Math.round((parseInt(found[1], 16) / 255) * 100) / 100;
    const [red, green, blue] = [found[2], found[3], found[4]].map((part) => parseInt(part, 16));
    return `rgba(${red}, ${green}, ${blue}, ${alpha})`;
  }

  const MOTD_TAGS = { B: 'b', STRONG: 'b', I: 'i', EM: 'i', U: 'u' };
  const MOTD_LINKS = new Set(['A', 'LOC', 'URL']);

  function motdChildren(source, target, depth) {
    source.childNodes.forEach((child) => {
      if (child.nodeType === Node.TEXT_NODE) {
        target.appendChild(document.createTextNode(child.textContent));
        return;
      }
      if (child.nodeType !== Node.ELEMENT_NODE || depth > 20) {
        return;
      }
      const name = child.tagName.toUpperCase();
      if (name === 'BR') {
        target.appendChild(document.createElement('br'));
      } else if (MOTD_TAGS[name]) {
        const element = node(MOTD_TAGS[name]);
        motdChildren(child, element, depth + 1);
        target.appendChild(element);
      } else if (name === 'FONT') {
        const element = node('span');
        const colour = motdColour(child.getAttribute('color'));
        if (colour) {
          element.style.color = colour;
        }
        const size = parseInt(child.getAttribute('size'), 10);
        if (Number.isFinite(size)) {
          element.style.fontSize = `${Math.min(22, Math.max(10, size))}px`;
        }
        motdChildren(child, element, depth + 1);
        target.appendChild(element);
      } else if (MOTD_LINKS.has(name) || name.startsWith('URL=')) {
        // A link to a character, a system or a channel: underlined, but it opens nothing here.
        const element = node('span', 'fo-ingame-link');
        motdChildren(child, element, depth + 1);
        target.appendChild(element);
      } else {
        motdChildren(child, target, depth + 1);
      }
    });
  }

  function motd(container, html) {
    clear(container);
    // An inert document: nothing in it runs, loads or renders; only its text and a few tags are copied.
    const parsed = new DOMParser().parseFromString(String(html || ''), 'text/html');
    motdChildren(parsed.body, container, 0);
  }

  // --- Before the first answer ------------------------------------------------------------------------

  function skeleton(container, lines) {
    clear(container);
    const widths = ['', 'w-75', 'w-50'];
    for (let index = 0; index < (lines || 3); index += 1) {
      container.appendChild(node('span', `fo-skeleton ${widths[index % widths.length]}`.trim()));
    }
  }

  // The Look / Raw text buttons of a preview box (partials/discord_preview.html) show one of its two panes.
  document.addEventListener('change', (event) => {
    const input = event.target;
    if (!(input instanceof HTMLInputElement) || input.type !== 'radio') {
      return;
    }
    const box = input.closest('[data-fo-preview]');
    if (!box) {
      return;
    }
    const prefix = box.id.replace(/-box$/, '');
    const look = document.getElementById(`${prefix}-look`);
    const raw = document.getElementById(`${prefix}-preview`);
    if (look) {
      look.hidden = input.value === 'raw';
    }
    if (raw) {
      raw.hidden = input.value !== 'raw';
    }
  });

  window.FleetOpsPreview = { discord, motd, motdColour };
  window.FleetOpsPreview.skeleton = skeleton;
})();
