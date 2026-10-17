// SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
// SPDX-License-Identifier: GPL-3.0-only

// Writes report.json, report.md and the contact sheet (index.html) of a run.
'use strict';

const fs = require('fs');
const path = require('path');

const LEVELS = ['FAIL', 'WARN', 'INFO'];

function modeName(r) {
  return `${r.theme}/${r.width}${r.lang === 'en' ? '' : '/' + r.lang}`;
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

// Findings of all results grouped by what is wrong and where; a finding that repeats in several modes becomes one entry.
function groupFindings(results, level) {
  const groups = new Map();
  for (const r of results) {
    for (const f of r.findings) {
      if (f.level !== level) continue;
      const detail = f.check === 'contrast' ? `${f.kind || 'text'}|${f.note || ''}` : (f.message || '').replace(/[\d.]+/g, '#');
      const key = [r.page, f.check, f.selector || '', detail].join('\u0001');
      if (!groups.has(key)) groups.set(key, { page: r.page, lane: r.lane, role: r.role, check: f.check, selector: f.selector, sample: f, modes: new Map(), occurrences: 0 });
      const g = groups.get(key);
      const mode = modeName(r);
      g.modes.set(mode, f);
      g.occurrences += f.count || 1;
      // Keep the worst contrast as the sample.
      if (f.check === 'contrast' && f.ratio < g.sample.ratio) g.sample = f;
    }
  }
  return [...groups.values()].sort((a, b) => a.page.localeCompare(b.page) || a.check.localeCompare(b.check) || (a.selector || '').localeCompare(b.selector || ''));
}

function describeFinding(g) {
  const f = g.sample;
  if (f.check === 'contrast') {
    const text = f.text ? ` "${f.text}"` : '';
    return `\`${f.selector}\`${text}: ${f.ratio}:1, needs ${f.need}:1 (${f.fg} on ${f.bg}${f.size ? `, ${f.size}px/${f.weight}` : ''})${f.note ? ' - ' + f.note : ''}`;
  }
  const text = f.text ? ` "${f.text}"` : '';
  return `${f.selector ? '`' + f.selector + '`' : ''}${text}: ${f.message}`;
}

function summarize(results) {
  const counts = { FAIL: 0, WARN: 0, INFO: 0 };
  const distinct = { FAIL: 0, WARN: 0, INFO: 0 };
  for (const level of LEVELS) {
    for (const g of groupFindings(results, level)) {
      distinct[level] += 1;
      counts[level] += g.occurrences;
    }
  }
  return { counts, distinct };
}

function writeMarkdown(file, run, results) {
  const lines = [];
  const summary = summarize(results);
  lines.push('# Interface check report', '');
  lines.push(`- Run: ${run.name} (${run.started})`);
  lines.push(`- Checkout: ${run.commit || 'unknown'}${run.dirty ? ' (uncommitted changes)' : ''}`);
  lines.push(`- Python: ${run.pythonInfo}`);
  lines.push(`- Pages: ${run.pageCount} (${run.skipped.length} skipped), modes: ${run.modes.length}, page loads: ${results.length}`);
  lines.push(`- Result: **${summary.distinct.FAIL} FAIL** (${summary.counts.FAIL} occurrences), ${summary.distinct.WARN} WARN (${summary.counts.WARN}), ${summary.distinct.INFO} INFO`);
  lines.push('', '## Findings by check', '');
  lines.push('| check | FAIL | WARN |', '|---|---|---|');
  const checks = [...new Set(results.flatMap((r) => r.findings.map((f) => f.check)))].sort();
  for (const check of checks) {
    const subset = results.map((r) => ({ ...r, findings: r.findings.filter((f) => f.check === check) }));
    lines.push(`| ${check} | ${groupFindings(subset, 'FAIL').length} | ${groupFindings(subset, 'WARN').length} |`);
  }
  lines.push('', 'Counts are distinct findings: the same problem on one page counts once however often it repeats or in how many modes.');
  lines.push('', '## Failures by mode', '');
  const themes = run.themes;
  const widths = run.widths;
  for (const lang of run.langs) {
    lines.push(`Language ${lang} (FAIL findings per theme and width; a repeated element counts once per page load)`, '');
    lines.push(`| theme | ${widths.join(' | ')} |`, `|---|${widths.map(() => '---').join('|')}|`);
    for (const theme of themes) {
      const cells = widths.map((w) => {
        const subset = results.filter((r) => r.lang === lang && r.theme === theme && r.width === w);
        if (!subset.length) return '-';
        return String(subset.reduce((n, r) => n + r.findings.filter((f) => f.level === 'FAIL').length, 0));
      });
      lines.push(`| ${theme} | ${cells.join(' | ')} |`);
    }
    lines.push('');
  }
  lines.push('## Failures by page', '');
  lines.push('| page | lane | role | FAIL | WARN |', '|---|---|---|---|---|');
  const pages = [...new Set(results.map((r) => r.page))];
  for (const page of pages) {
    const subset = results.filter((r) => r.page === page);
    const fail = groupFindings(subset, 'FAIL').length;
    const warn = groupFindings(subset, 'WARN').length;
    lines.push(`| ${page} | ${subset[0].lane} | ${subset[0].role} | ${fail} | ${warn} |`);
  }
  lines.push('', '## Failures', '');
  const fails = groupFindings(results, 'FAIL');
  if (!fails.length) lines.push('None.', '');
  let current = '';
  for (const g of fails) {
    if (g.page !== current) {
      current = g.page;
      lines.push(`### ${g.page} (lane ${g.lane}, role ${g.role})`, '');
    }
    const modes = [...g.modes.keys()];
    const shown = modes.length > 6 ? `${modes.slice(0, 6).join(', ')} and ${modes.length - 6} more` : modes.join(', ');
    lines.push(`- [${g.check}] ${describeFinding(g)}. Seen in ${shown}.`);
  }
  lines.push('', '## Warnings (the theme owns some of these)', '');
  const warns = groupFindings(results, 'WARN');
  if (!warns.length) lines.push('None.', '');
  current = '';
  for (const g of warns) {
    if (g.page !== current) {
      current = g.page;
      lines.push(`### ${g.page}`, '');
    }
    const modes = [...g.modes.keys()];
    const shown = modes.length > 4 ? `${modes.slice(0, 4).join(', ')} and ${modes.length - 4} more` : modes.join(', ');
    lines.push(`- [${g.check}] ${describeFinding(g)}. Seen in ${shown}.`);
  }
  const infos = groupFindings(results, 'INFO');
  if (infos.length) {
    lines.push('', '## Information', '');
    for (const g of infos.slice(0, 40)) lines.push(`- ${g.page} [${g.check}] ${describeFinding(g)}.`);
    if (infos.length > 40) lines.push(`- ... and ${infos.length - 40} more in report.json`);
  }
  if (run.skipped.length) {
    lines.push('', '## Pages not in this tree', '');
    for (const s of run.skipped) lines.push(`- ${s.id} (${s.lane}): ${s.skip}`);
  }
  if (run.unmapped.length) {
    lines.push('', '## External files that were not served from the cache', '');
    for (const u of run.unmapped) lines.push(`- ${u}`);
  }
  if (run.serverProblems.length) {
    lines.push('', '## Server log', '');
    for (const p of run.serverProblems) lines.push(`- ${p}`);
  }
  lines.push('');
  fs.writeFileSync(file, lines.join('\n'));
}

function writeContactSheet(file, run, results) {
  const pages = [...new Set(results.map((r) => r.page))];
  const rows = [];
  const key = (r) => `${r.lang}|${r.theme}|${r.width}`;
  const byKey = new Map(results.map((r) => [`${r.page}|${key(r)}`, r]));
  for (const page of pages) {
    const first = results.find((r) => r.page === page);
    const cells = [];
    for (const lang of run.langs) {
      for (const theme of run.themes) {
        for (const width of run.widths) {
          const r = byKey.get(`${page}|${lang}|${theme}|${width}`);
          if (!r) continue;
          const fails = r.findings.filter((f) => f.level === 'FAIL').length;
          const warns = r.findings.filter((f) => f.level === 'WARN').length;
          const image = r.screenshot ? `<img loading="lazy" src="${escapeHtml(r.screenshot)}" alt="">` : '<span class="none">no image</span>';
          cells.push(`<a class="cell ${fails ? 'bad' : (warns ? 'warn' : 'ok')}" data-lang="${lang}" data-width="${width}" data-theme="${theme}" href="${escapeHtml(r.screenshot || '#')}" title="${escapeHtml(`${theme} ${width} ${lang}: ${fails} FAIL, ${warns} WARN`)}">${image}<span>${escapeHtml(theme)} ${fails ? fails + ' FAIL' : ''}</span></a>`);
        }
      }
    }
    rows.push(`<section><h2>${escapeHtml(page)} <small>lane ${first.lane}, ${escapeHtml(first.role)}</small></h2><div class="row">${cells.join('')}</div></section>`);
  }
  const options = (values, name) => values.map((v, i) => `<option value="${v}"${i === 0 ? ' selected' : ''}>${v}</option>`).join('');
  fs.writeFileSync(file, `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Interface check contact sheet</title>
<style>
body{font:14px/1.4 system-ui,sans-serif;margin:16px;background:#fff;color:#222}
header{position:sticky;top:0;background:#fff;padding:8px 0;border-bottom:1px solid #ccc;z-index:5}
select{margin-right:12px}
h2{font-size:16px;margin:16px 0 4px}h2 small{font-weight:400;color:#666}
.row{display:flex;flex-wrap:wrap;gap:8px}
.cell{display:flex;flex-direction:column;width:200px;border:2px solid #9c9;text-decoration:none;color:#222;background:#f6f6f6}
.cell.bad{border-color:#c33}.cell.warn{border-color:#c90}
.cell img{width:100%;max-height:320px;object-fit:cover;object-position:top}
.cell span{padding:2px 4px;font-size:12px}
.none{padding:30px 4px}
</style></head><body>
<header><strong>${escapeHtml(run.name)}</strong> &nbsp;
language <select id="lang">${options(run.langs)}</select>
width <select id="width">${options(run.widths)}</select>
<label><input type="checkbox" id="onlybad"> only pages with failures</label>
</header>
${rows.join('\n')}
<script>
const lang = document.getElementById('lang'), width = document.getElementById('width'), only = document.getElementById('onlybad');
function apply() {
  document.querySelectorAll('.cell').forEach((c) => { c.hidden = !(c.dataset.lang === lang.value && c.dataset.width === width.value); });
  document.querySelectorAll('section').forEach((s) => {
    const shown = [...s.querySelectorAll('.cell')].filter((c) => !c.hidden);
    s.hidden = !shown.length || (only.checked && !shown.some((c) => c.classList.contains('bad')));
  });
}
[lang, width, only].forEach((e) => e.addEventListener('change', apply));
apply();
</script></body></html>
`);
}

function writeReports(outDir, run, results) {
  fs.writeFileSync(path.join(outDir, 'report.json'), JSON.stringify({ run, summary: summarize(results), results }, null, 1));
  writeMarkdown(path.join(outDir, 'report.md'), run, results);
  writeContactSheet(path.join(outDir, 'index.html'), run, results);
}

module.exports = { writeReports, summarize, groupFindings, modeName };
