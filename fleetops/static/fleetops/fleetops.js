// FleetOps page helpers. Every time here is EVE time (UTC), whatever the browser's time zone.
(() => {
  const pad = (number) => String(number).padStart(2, '0');

  // "1 h 20 min" or "45 min".
  function duration(minutes) {
    const hours = Math.floor(minutes / 60);
    return hours ? `${hours} h ${pad(minutes % 60)} min` : `${minutes} min`;
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
        element.textContent = `in ${duration(minutes)}`;
      } else if (minutes === 0) {
        element.textContent = 'now';
      } else {
        element.textContent = `${duration(-minutes)} ago`;
      }
    });
  }

  window.FleetOps = { duration, eveClock, eveDate, roundedEveTime, updateCountdowns };
  updateCountdowns();
  setInterval(updateCountdowns, 30000);
})();
