// SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
// SPDX-License-Identifier: GPL-3.0-only

// Previews of a Discord ping and of an in-game MOTD, shared by the start page and the Send Ping page.
// This is the contract of the start page's preview; its renderers replace these plain-text versions:
//   FleetOpsPreview.discord(container, text, {channel, mentions})   the ping as Discord would show it
//   FleetOpsPreview.motd(container, html)                           the MOTD as the game would show it
//   FleetOpsPreview.motdColour(value)                               a game colour (#AARRGGBB) as CSS, or null
// Both renderers build DOM nodes only and never put text into the page as HTML.
(() => {
  function clear(container) {
    while (container.firstChild) {
      container.removeChild(container.firstChild);
    }
  }

  function discord(container, text) {
    clear(container);
    container.textContent = text;
  }

  function motd(container, html) {
    clear(container);
    container.textContent = html;
  }

  function motdColour() {
    return null;
  }

  window.FleetOpsPreview = { discord, motd, motdColour };
})();
