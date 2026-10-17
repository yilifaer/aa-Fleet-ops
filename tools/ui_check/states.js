// SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
// SPDX-License-Identifier: GPL-3.0-only

// Scripted states of pages. run.js loads the page of a seeded page entry, calls the function named in its
// "states" list with {page, entry, lang, base} and checks the page it leaves behind like any other page.
// A state returns {expect: RegExp|null, note: string}: when "expect" is given and the page is in English, the
// text must be on the page afterwards, otherwise the state did not produce what it is for (a failure of its own).
//
// Selectors use field names, not ids or layout, so a redesigned page keeps working. A state that cannot find
// what it needs throws an Error that names it; run.js reports that as a failure of the page.
'use strict';

const ERROR_ELEMENTS = '.alert-danger, .alert-warning, .errorlist, [role=alert], .invalid-feedback, .fo-callout';

async function need(page, selector, what) {
  const handle = await page.$(selector);
  if (!handle) throw new Error(`state cannot find ${what} (${selector})`);
  return handle;
}

async function selectFirstOption(page, selector, what) {
  await need(page, selector, what);
  await page.evaluate((sel) => {
    const select = document.querySelector(sel);
    const option = [...select.options].find((o) => o.value);
    if (option) {
      select.value = option.value;
      select.dispatchEvent(new Event('change', { bubbles: true }));
    }
  }, selector);
}

// Send the form of an element the way a script-less browser would: no submit event, no script checks.
async function submitFormOf(page, selector) {
  await Promise.all([
    page.waitForNavigation({ waitUntil: 'load', timeout: 20000 }),
    page.evaluate((sel) => {
      const form = document.querySelector(sel).closest('form');
      HTMLFormElement.prototype.submit.call(form);
    }, selector),
  ]);
}

const states = {
  // FB02: the start page with a ping Discord would refuse; the server answers with a page-level error.
  async 'start-error'({ page }) {
    await selectFirstOption(page, 'select[name=fc_character_id]', 'the FC character list');
    await selectFirstOption(page, 'select[name=fleet_type]', 'the fleet type list');
    await need(page, 'input[name=formup]', 'the form-up field');
    await page.fill('input[name=formup]', 'Jita');
    const channel = await page.evaluate(() => {
      const select = document.querySelector('select[name=ping_channel]');
      if (!select) return false;
      const option = [...select.options].find((o) => /Alliance Pings/.test(o.textContent));
      if (!option) return false;
      select.value = option.value;
      select.dispatchEvent(new Event('change', { bubbles: true }));
      return true;
    });
    if (!channel) throw new Error('state cannot find the ping channel "Alliance Pings"');
    // "Send ping" is the full operation mode; the field exists in every layout of the page.
    await page.evaluate(() => {
      const mode = document.querySelector('input[name=operation_mode][value=full]');
      if (mode) mode.checked = true;
      const timing = document.querySelector('input[name=fleet_timing][value=ping_now]');
      if (timing) timing.checked = true;
    });
    await need(page, 'textarea[name=additional_message]', 'the additional information field');
    await page.fill('textarea[name=additional_message]', 'x'.repeat(2100));
    await submitFormOf(page, 'textarea[name=additional_message]');
    return { expect: /Discord accepts at most 2000/, note: 'the server answered with the ping-too-long error' };
  },

  // 5-13: the PAP tab of a running fleet, where the control for the PAP characters per player sits above the preview.
  async 'pap-tab'({ page }) {
    await need(page, '[data-bs-target="#attendance"]', 'the PAP tab');
    await page.click('[data-bs-target="#attendance"]');
    await page.waitForSelector('#attendance.show.active', { timeout: 5000 });
    // The pointer would otherwise stay on the tab and the check would measure its hover colour.
    await page.mouse.move(0, 0);
    return { expect: /PAP characters per player/, note: 'the PAP tab shows the control for the limit' };
  },

  // FB02: an SRP payout rule with an amount but neither ship nor doctrine; the error belongs to the whole form.
  async 'config-error'({ page }) {
    await need(page, 'input[name=amount]', 'the amount field');
    await page.fill('input[name=amount]', '5000000');
    await submitFormOf(page, 'input[name=amount]');
    return { expect: /Choose a ship type, a doctrine, or both/, note: 'the server answered with the form-level error' };
  },
};

module.exports = { states, ERROR_ELEMENTS };
