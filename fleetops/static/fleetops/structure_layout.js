// SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
// SPDX-License-Identifier: GPL-3.0-only

// Live check of a fleet structure layout in the administration form. Saving checks the layout again on the server.
(() => {
  'use strict';

  const MAX_WINGS = 5;
  const MAX_SQUADS = 5;
  const NAME_MAX = 10;

  const clean = (value) => value.replace(/\s+/g, ' ').trim();

  function name(text, line, kind) {
    const value = clean(text);
    if (!value) {
      throw new Error(kind === 'wing'
        ? interpolate(gettext('Line %(line)s: a wing name is empty.'), { line }, true)
        : interpolate(gettext('Line %(line)s: a squad name is empty.'), { line }, true));
    }
    if ([...value].length > NAME_MAX) {
      throw new Error(kind === 'wing'
        ? interpolate(gettext('Line %(line)s: wing name "%(name)s" is longer than %(max)s characters.'), { line, name: value, max: NAME_MAX }, true)
        : interpolate(gettext('Line %(line)s: squad name "%(name)s" is longer than %(max)s characters.'), { line, name: value, max: NAME_MAX }, true));
    }
    return value;
  }

  function parse(text) {
    const wings = [];
    text.split(/\r?\n/).forEach((raw, index) => {
      const line = raw.trim();
      const number = index + 1;
      if (!line || line.startsWith('#')) return;
      if (wings.length === MAX_WINGS) {
        throw new Error(interpolate(gettext('Line %(line)s: EVE allows at most %(max)s wings.'), { line: number, max: MAX_WINGS }, true));
      }
      const colon = line.indexOf(':');
      const wing = name(colon < 0 ? line : line.slice(0, colon), number, 'wing');
      const rest = colon < 0 ? '' : line.slice(colon + 1);
      if (!rest.trim()) {
        throw new Error(interpolate(gettext('Line %(line)s: wing "%(wing)s" needs at least one squad, for example "%(wing)s: Squad 1".'), { line: number, wing }, true));
      }
      if (rest.includes(':')) {
        throw new Error(interpolate(gettext('Line %(line)s: use one ":" per line, between the wing name and its squads.'), { line: number }, true));
      }
      const squads = rest.split(',').map((part) => name(part, number, 'squad'));
      if (squads.length > MAX_SQUADS) {
        throw new Error(interpolate(
          gettext('Line %(line)s: wing "%(wing)s" has %(count)s squads; EVE allows at most %(max)s per wing.'),
          { line: number, wing, count: squads.length, max: MAX_SQUADS },
          true,
        ));
      }
      wings.push({ wing, squads });
    });
    if (!wings.length) {
      throw new Error(gettext('Add at least one wing, for example "Main: Alpha, Bravo".'));
    }
    return wings;
  }

  // {ok: true, text} for a valid layout, {ok: false, text} with the first problem otherwise.
  function describe(text) {
    try {
      const wings = parse(text);
      const squads = wings.reduce((count, item) => count + item.squads.length, 0);
      const wingText = interpolate(ngettext('%(count)s wing', '%(count)s wings', wings.length), { count: wings.length }, true);
      const squadText = interpolate(ngettext('%(count)s squad', '%(count)s squads', squads), { count: squads }, true);
      const layout = wings.map((item) => `${item.wing} (${item.squads.join(', ')})`).join(' · ');
      return {
        ok: true,
        text: interpolate(gettext('%(wings)s with %(squads)s: %(layout)s'), { wings: wingText, squads: squadText, layout }, true),
      };
    } catch (error) {
      return { ok: false, text: error.message };
    }
  }

  window.FleetOpsStructureLayout = { describe };
})();
