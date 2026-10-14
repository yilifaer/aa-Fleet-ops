// SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
// SPDX-License-Identifier: GPL-3.0-only

// FleetOps page helpers. Every time here is EVE time (UTC), whatever the browser's time zone.
// Texts come from the JavaScript catalog (gettext, ngettext, pgettext, interpolate) that the base page loads.
(() => {
  const pad = (number) => String(number).padStart(2, '0');

  // "1 h 20 min" or "45 min".
  function duration(minutes) {
    const hours = Math.floor(minutes / 60);
    return hours
      ? interpolate(gettext('%(hours)s h %(minutes)s min'), { hours, minutes: pad(minutes % 60) }, true)
      : interpolate(gettext('%(minutes)s min'), { minutes }, true);
  }

  function eveClock(date) {
    return `${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}`;
  }

  function eveDate(date) {
    return `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())}`;
  }

  // The current EVE time rounded up to five minutes, plus an offset; for quick fleet time buttons.
  function roundedEveTime(now, offsetMinutes) {
    const time = new Date(now.getTime());
    time.setUTCSeconds(0, 0);
    time.setUTCMinutes(Math.ceil(time.getUTCMinutes() / 5) * 5 + offsetMinutes);
    return { date: eveDate(time), clock: eveClock(time) };
  }

  // Elements with data-countdown-utc="2026-10-12T19:00:00Z" show the time left until then.
  function updateCountdowns() {
    document.querySelectorAll('[data-countdown-utc]').forEach((element) => {
      const minutes = Math.round((Date.parse(element.dataset.countdownUtc) - Date.now()) / 60000);
      if (Number.isNaN(minutes)) {
        element.textContent = '';
      } else if (minutes > 0) {
        element.textContent = interpolate(gettext('in %(duration)s'), { duration: duration(minutes) }, true);
      } else if (minutes === 0) {
        element.textContent = gettext('now');
      } else {
        element.textContent = interpolate(gettext('%(duration)s ago'), { duration: duration(-minutes) }, true);
      }
    });
  }

  // The name of a fleet position from ESI's code, such as "squad_commander"; an unknown code is shown as it is.
  function roleLabel(code) {
    switch (code) {
      case 'fleet_commander':
        return pgettext('fleet position', 'Fleet Commander');
      case 'wing_commander':
        return pgettext('fleet position', 'Wing Commander');
      case 'squad_commander':
        return pgettext('fleet position', 'Squad Commander');
      case 'squad_member':
        return pgettext('fleet position', 'Squad Member');
      default:
        return code;
    }
  }

  // The lines under the character field of the start pages. `data` is the answer of the detect route.
  const detection = {
    select: () => gettext('Select a character to detect its current ESI fleet.'),
    checking: () => gettext('Checking fleet…'),
    failed: () => '⚠ ' + gettext('Fleet detection request failed.'),
    result(data) {
      if (!data.ok) {
        return '⚠ ' + data.message;
      }
      if (data.is_fleet_boss) {
        const text = interpolate(gettext('Fleet %(fleet_id)s — role: %(role)s — Fleet Boss'), { fleet_id: data.fleet_id, role: roleLabel(data.role) }, true);
        return '✓ ' + text;
      }
      const text = interpolate(gettext('Fleet %(fleet_id)s — %(message)s'), { fleet_id: data.fleet_id, message: data.message }, true);
      return '⚠ ' + text;
    },
  };

  window.FleetOps = { duration, eveClock, eveDate, roundedEveTime, updateCountdowns, roleLabel, detection };
  updateCountdowns();
  setInterval(updateCountdowns, 30000);
})();
