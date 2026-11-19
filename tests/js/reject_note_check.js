// Browser check of the data-fo-need handler of fleetops.js: Reject with an empty note shows the message and asks
// nothing; Approve with an empty note right after it must still be sent (the message must not stay on the field).
// Usage: node reject_note_check.js <path to fleetops.js>. Prints one JSON line.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const fs = require('fs');

(async () => {
  const script = fs.readFileSync(process.argv[2], 'utf8');
  const browser = await chromium.launch(process.env.PLAYWRIGHT_BROWSERS_PATH ? {} : {});
  const page = await browser.newPage();
  await page.setContent(`<form id="f" method="post" action="/sent"><input name="note" maxlength="255">
    <button id="approve" name="action" value="approve">Approve</button>
    <button id="reject" name="action" value="reject" data-fo-need="note" data-fo-need-message="Give a reason."
      data-fo-confirm="Reject?">Reject</button></form>`);
  await page.evaluate(() => {
    window.sent = [];
    document.getElementById('f').addEventListener('submit', (e) => {
      e.preventDefault();
      window.sent.push(e.submitter && e.submitter.value);
    });
  });
  await page.addScriptTag({ content: script });
  const result = {};
  await page.click('#reject');
  result.afterReject = await page.evaluate(() => ({
    sent: window.sent.slice(),
    dialog: !!document.querySelector('dialog[open], .modal.show, [role="dialog"]'),
    validity: document.querySelector('[name=note]').validationMessage,
  }));
  await page.click('#approve');
  result.afterApprove = await page.evaluate(() => ({ sent: window.sent.slice() }));
  await browser.close();
  console.log(JSON.stringify(result));
})().catch((error) => {
  console.error(String(error));
  process.exit(2);
});
