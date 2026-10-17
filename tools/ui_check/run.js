#!/usr/bin/env node
// SPDX-FileCopyrightText: 2026 yilifaer and AA FleetOps contributors
// SPDX-License-Identifier: GPL-3.0-only

// Browser check of the FleetOps pages. See README.md.
//
//   node tools/ui_check/run.js --python .venv/bin/python [--pages start,fleet-active-manager] [--lane A]
//        [--themes darkly,flatly] [--widths 375] [--lang auto|en|zh-hans|all] [--jobs 3] [--out DIR]
//
// It starts a throw-away Redis, builds the example database (seed.py), starts the site with the test settings,
// opens every page in Chromium in each theme and width, runs the contrast and layout checks, takes screenshots
// and writes report.md, report.json and a contact sheet. The theme is switched through Alliance Auth's own theme
// form. Nothing leaves the machine: the CDN files come from the verified cache (fetch_assets.py).
// Exit codes: 0 no failure, 1 at least one FAIL, 2 the run itself broke.
'use strict';

const childProcess = require('child_process');
const crypto = require('crypto');
const fs = require('fs');
const http = require('http');
const net = require('net');
const path = require('path');

const { writeReports, summarize } = require('./report');
const { states, ERROR_ELEMENTS } = require('./states');

const HERE = __dirname;
const REPO = path.resolve(HERE, '..', '..');
const THEME_ATTRS = {
  bootstrap: { 'data-theme': 'bootstrap' },
  'bootstrap-dark': { 'data-theme': 'bootstrap-dark', 'data-bs-theme': 'dark' },
  flatly: { 'data-theme': 'flatly' },
  darkly: { 'data-theme': 'darkly' },
  materia: { 'data-theme': 'materia' },
};
const ALL_THEMES = Object.keys(THEME_ATTRS);
const ZH_THEMES = ['flatly', 'darkly'];
const ZH_WIDTHS = [375, 1280];
const ALL_WIDTHS = [375, 768, 1280];

// ---- options --------------------------------------------------------------------------------------------

function usage() {
  return `Usage: node tools/ui_check/run.js [options]

  --python PATH      Python of the Alliance Auth environment (default: FLEETOPS_UI_PYTHON, .venv/bin/python, python3)
  --pages LIST       page ids or patterns, comma separated, for example start,fleet-*,dashboard (default: all)
  --lane LETTER      the pages of one lane: A B C D E F, S0 (checks of Step 0) or all
  --themes LIST      ${ALL_THEMES.join(',')} (default: all five)
  --widths LIST      375,768,1280 (default: all three)
  --lang MODE        auto: English everywhere, Chinese at 375 and 1280 in flatly and darkly (default)
                     en | zh-hans | all
  --jobs N           browser contexts at the same time (default 3)
  --redis-server     use a throw-away Redis of its own (the default)
  --redis-url URL    use this Redis instead (shared: make sure nobody else uses the database)
  --out DIR          output folder (default tools/ui_check/out/<name>)
  --name NAME        run name (default: the start time)
  --shots MODE       all | fail | none (default all)
  --port N           port of the site (default: a free one)
  --keep             keep the database and server log in the output folder
  --no-focus         skip the keyboard focus check
  --fail-on-warn     exit 1 on warnings too
  --list             print the pages and exit
  --help
`;
}

function parseArgs(argv) {
  const options = {
    python: process.env.FLEETOPS_UI_PYTHON || '', pages: '', lane: '', themes: ALL_THEMES, widths: ALL_WIDTHS, lang: 'auto', jobs: 3,
    redisUrl: '', out: '', name: '', shots: 'all', port: 0, keep: false, focus: true, failOnWarn: false, list: false,
    timeout: 30000,
  };
  const flags = new Set(['--redis-server', '--keep', '--no-focus', '--fail-on-warn', '--list', '--help']);
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    const [name, inline] = arg.startsWith('--') && arg.includes('=') ? [arg.slice(0, arg.indexOf('=')), arg.slice(arg.indexOf('=') + 1)] : [arg, undefined];
    const value = () => {
      if (inline !== undefined) return inline;
      i += 1;
      if (i >= argv.length) throw new Error(`${name} needs a value`);
      return argv[i];
    };
    if (flags.has(name)) {
      if (name === '--keep') options.keep = true;
      else if (name === '--no-focus') options.focus = false;
      else if (name === '--fail-on-warn') options.failOnWarn = true;
      else if (name === '--list') options.list = true;
      else if (name === '--help') options.help = true;
      continue;
    }
    switch (name) {
      case '--python': options.python = value(); break;
      case '--pages': options.pages = value(); break;
      case '--lane': options.lane = value().toUpperCase(); break;
      case '--themes': options.themes = value().split(',').map((s) => s.trim()).filter(Boolean); break;
      case '--widths': options.widths = value().split(',').map((s) => parseInt(s, 10)).filter(Boolean); break;
      case '--lang': options.lang = value().toLowerCase().replace('zh_hans', 'zh-hans'); break;
      case '--jobs': options.jobs = Math.max(1, parseInt(value(), 10) || 1); break;
      case '--redis-url': options.redisUrl = value(); break;
      case '--out': options.out = path.resolve(value()); break;
      case '--name': options.name = value(); break;
      case '--shots': options.shots = value(); break;
      case '--port': options.port = parseInt(value(), 10) || 0; break;
      case '--timeout': options.timeout = parseInt(value(), 10) || 30000; break;
      default: throw new Error(`unknown option ${arg}`);
    }
  }
  for (const theme of options.themes) if (!THEME_ATTRS[theme]) throw new Error(`unknown theme ${theme} (use ${ALL_THEMES.join(', ')})`);
  if (!['auto', 'en', 'zh-hans', 'all'].includes(options.lang)) throw new Error('--lang is auto, en, zh-hans or all');
  if (!['all', 'fail', 'none'].includes(options.shots)) throw new Error('--shots is all, fail or none');
  return options;
}

// ---- small helpers -----------------------------------------------------------------------------------------

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function freePort() {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address();
      server.close(() => resolve(port));
    });
    server.on('error', reject);
  });
}

function wildcard(pattern) {
  return new RegExp('^' + pattern.replace(/[.+^${}()|[\]\\]/g, '\\$&').replace(/\*/g, '.*').replace(/\?/g, '.') + '$');
}

function pythonCommand(options) {
  if (options.python) return path.resolve(options.python);
  const venv = path.join(REPO, '.venv', 'bin', 'python');
  if (fs.existsSync(venv)) return venv;
  return 'python3';
}

function run(command, args, env, cwd = REPO) {
  return childProcess.spawnSync(command, args, { cwd, env: { ...process.env, ...env }, encoding: 'utf8' });
}

function loadPlaywright() {
  const attempts = [process.cwd(), REPO, HERE];
  for (const base of attempts) {
    try { return require(require.resolve('playwright', { paths: [base] })); } catch { /* try the next place */ }
  }
  try {
    const root = childProcess.execSync('npm root -g', { encoding: 'utf8' }).trim();
    return require(path.join(root, 'playwright'));
  } catch {
    throw new Error('the playwright package is not installed (npm install -g playwright); never run "playwright install" here, Chromium is in PLAYWRIGHT_BROWSERS_PATH');
  }
}

function waitForPort(port, timeoutMs, label) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    const attempt = () => {
      const socket = net.connect(port, '127.0.0.1');
      socket.once('connect', () => { socket.destroy(); resolve(); });
      socket.once('error', () => {
        socket.destroy();
        if (Date.now() > deadline) reject(new Error(`${label} did not start on port ${port}`));
        else setTimeout(attempt, 200);
      });
    };
    attempt();
  });
}

function httpStatus(url) {
  return new Promise((resolve) => {
    http.get(url, (response) => { response.resume(); resolve(response.statusCode); }).on('error', () => resolve(0));
  });
}

// ---- assets served from the cache ---------------------------------------------------------------------------

function loadAssets() {
  const cache = path.join(HERE, '.cache');
  const manifestPath = path.join(cache, 'manifest.json');
  if (!fs.existsSync(manifestPath)) return null;
  const manifest = JSON.parse(fs.readFileSync(manifestPath, 'utf8'));
  const assets = new Map();
  for (const [url, entry] of Object.entries(manifest)) {
    const file = path.join(cache, 'files', entry.file);
    if (!fs.existsSync(file)) continue;
    const body = fs.readFileSync(file);
    const sha = crypto.createHash('sha256').update(body).digest('hex');
    if (sha !== entry.sha256) throw new Error(`the cached copy of ${url} does not match its recorded hash; run fetch_assets.py --force`);
    assets.set(url, { body, type: entry.type });
  }
  return assets;
}

const PLACEHOLDER_SVG = '<svg xmlns="http://www.w3.org/2000/svg" width="64" height="64" viewBox="0 0 64 64"><rect width="64" height="64" fill="#888"/></svg>';

// ---- the run -------------------------------------------------------------------------------------------------

async function main() {
  let options;
  try {
    options = parseArgs(process.argv.slice(2));
  } catch (error) {
    console.error(`error: ${error.message}\n\n${usage()}`);
    return 2;
  }
  if (options.help) { console.log(usage()); return 0; }
  const python = pythonCommand(options);
  const started = new Date();
  options.name = options.name || started.toISOString().replace(/[:T]/g, '-').slice(0, 19);
  const outDir = options.out || path.join(HERE, 'out', options.name);

  if (options.list) {
    const args = [path.join(HERE, 'seed.py'), '--list'];
    if (options.lane && options.lane !== 'ALL') args.push('--lane', options.lane);
    if (options.pages) args.push('--pages', options.pages);
    const listing = run(python, args, { DJANGO_SETTINGS_MODULE: 'tools.ui_check.settings' });
    process.stdout.write(listing.stdout);
    process.stderr.write(listing.stderr);
    return listing.status || 0;
  }

  fs.mkdirSync(outDir, { recursive: true });
  const workDir = path.join(HERE, '.work');
  fs.mkdirSync(workDir, { recursive: true });
  const runDir = fs.mkdtempSync(path.join(workDir, 'run-'));
  const children = [];
  const cleanup = () => {
    for (const child of children) { try { child.kill('SIGTERM'); } catch { /* already gone */ } }
    if (!options.keep) { try { fs.rmSync(runDir, { recursive: true, force: true }); } catch { /* best effort */ } }
  };
  process.on('SIGINT', () => { cleanup(); process.exit(2); });
  process.on('SIGTERM', () => { cleanup(); process.exit(2); });

  try {
    return await execute(options, { python, started, outDir, workDir, runDir, children });
  } catch (error) {
    console.error(`error: ${error.stack || error.message}`);
    return 2;
  } finally {
    cleanup();
  }
}

async function execute(options, env) {
  const { python, started, outDir, runDir, children } = env;
  // 1. cached CDN files
  let assets = loadAssets();
  if (!assets) {
    console.log('fetching the theme files once (tools/ui_check/fetch_assets.py) ...');
    const fetched = run(python, [path.join(HERE, 'fetch_assets.py')], {});
    process.stdout.write(fetched.stdout);
    if (fetched.status !== 0) throw new Error(`fetch_assets.py failed:\n${fetched.stderr}`);
    assets = loadAssets();
  }

  // 2. Redis
  let redisUrl = options.redisUrl;
  if (!redisUrl) {
    const redisPort = await freePort();
    const redis = childProcess.spawn('redis-server', ['--port', String(redisPort), '--bind', '127.0.0.1', '--save', '', '--appendonly', 'no', '--dir', runDir], { stdio: 'ignore' });
    redis.on('error', () => {});
    children.push(redis);
    await waitForPort(redisPort, 10000, 'redis-server (is it installed?)');
    redisUrl = `redis://127.0.0.1:${redisPort}/0`;
  }

  // 3. database and logins
  const seedFile = path.join(runDir, 'seed.json');
  const dbFile = path.join(runDir, 'ui.sqlite3');
  const pythonEnv = {
    DJANGO_SETTINGS_MODULE: 'tools.ui_check.settings', FLEETOPS_UI_REDIS: redisUrl, FLEETOPS_UI_DB: dbFile,
    FLEETOPS_UI_WORK: path.join(HERE, '.work'), PYTHONDONTWRITEBYTECODE: '1',
  };
  console.log('building the example database ...');
  const seeded = run(python, [path.join(HERE, 'seed.py'), '--out', seedFile, '--db', dbFile, '--logins', String(options.jobs)], pythonEnv);
  if (seeded.status !== 0) throw new Error(`seed.py failed:\n${seeded.stdout}\n${seeded.stderr}`);
  const seed = JSON.parse(fs.readFileSync(seedFile, 'utf8'));
  const versions = run(python, ['-c', 'import allianceauth, django, esi, sys; print("Python %s, Alliance Auth %s, Django %s, django-esi %s" % (sys.version.split()[0], allianceauth.__version__, django.__version__, esi.__version__))'], pythonEnv);
  const pythonInfo = (versions.stdout || '').trim() || python;

  // 4. which pages
  const patterns = options.pages ? options.pages.split(',').map((s) => s.trim()).filter(Boolean).map(wildcard) : null;
  const selected = seed.pages.filter((p) => {
    if (patterns && !patterns.some((re) => re.test(p.id))) return false;
    if (options.lane && options.lane !== 'ALL' && p.lane !== options.lane) return false;
    return true;
  });
  if (!selected.length) throw new Error('no page matches --pages / --lane');
  const skipped = selected.filter((p) => p.skip);
  const pages = selected.filter((p) => !p.skip);

  // 5. modes
  const themes = options.themes;
  const widths = options.widths;
  const combos = [];
  const langs = new Set();
  for (const theme of themes) {
    for (const width of widths) {
      if (options.lang === 'en' || options.lang === 'auto' || options.lang === 'all') combos.push({ lang: 'en', theme, width });
      const zh = options.lang === 'zh-hans' || options.lang === 'all' || (options.lang === 'auto' && ZH_THEMES.includes(theme) && ZH_WIDTHS.includes(width));
      if (zh) combos.push({ lang: 'zh-hans', theme, width });
    }
  }
  combos.forEach((c) => langs.add(c.lang));
  const langList = ['en', 'zh-hans'].filter((l) => langs.has(l));

  // 6. site
  const port = options.port || await freePort();
  const base = `http://127.0.0.1:${port}`;
  const serverLog = path.join(outDir, 'server.log');
  const logFd = fs.openSync(serverLog, 'w');
  const server = childProcess.spawn(python, [path.join(REPO, 'manage.py'), 'runserver', '--insecure', '--noreload', `127.0.0.1:${port}`], {
    cwd: REPO, env: { ...process.env, ...pythonEnv }, stdio: ['ignore', logFd, logFd],
  });
  server.on('error', () => {});
  children.push(server);
  await waitForPort(port, 60000, 'the site');
  const deadline = Date.now() + 30000;
  while (await httpStatus(`${base}/fleetops/`) !== 302) {
    if (Date.now() > deadline) throw new Error(`the site did not answer with a login redirect on ${base}/fleetops/ (see ${serverLog})`);
    await sleep(300);
  }

  // The menu of Alliance Auth creates its rows the first time it is shown; two first requests at the same time
  // would collide on them, so every role asks once before the browsers start.
  for (const cookies of Object.values(seed.logins)) {
    await new Promise((resolve) => {
      http.get(`${base}/fleetops/`, { headers: { cookie: `sessionid=${cookies[0]}` } }, (response) => { response.resume(); response.on('end', resolve); }).on('error', resolve);
    });
  }

  // 7. browser
  if (!process.env.PLAYWRIGHT_BROWSERS_PATH && fs.existsSync('/opt/pw-browsers')) process.env.PLAYWRIGHT_BROWSERS_PATH = '/opt/pw-browsers';
  const playwright = loadPlaywright();
  const browser = await playwright.chromium.launch();
  const scripts = {
    contrast: path.join(HERE, 'contrast.js'),
    checks: path.join(HERE, 'checks.js'),
  };
  const rules = JSON.parse(fs.readFileSync(path.join(HERE, 'rules.json'), 'utf8'));
  const unmapped = new Set();
  const results = [];
  const ctx = { options, seed, base, assets, unmapped, scripts, rules, outDir, browser, playwright, results };

  console.log(`checking ${pages.length} pages in ${combos.length} modes (${combos.length * pages.length} page loads, ${options.jobs} at a time) -> ${outDir}`);
  const queue = combos.map((combo, index) => ({ combo, index }));
  let done = 0;
  const workers = Array.from({ length: Math.min(options.jobs, queue.length) }, async (_unused, slot) => {
    while (queue.length) {
      const job = queue.shift();
      const begun = Date.now();
      const modeResults = await runMode(ctx, job.combo, pages, slot);
      results.push(...modeResults);
      done += 1;
      const fails = modeResults.reduce((n, r) => n + r.findings.filter((f) => f.level === 'FAIL').length, 0);
      const warns = modeResults.reduce((n, r) => n + r.findings.filter((f) => f.level === 'WARN').length, 0);
      console.log(`[${done}/${combos.length}] ${job.combo.lang} ${job.combo.theme} ${job.combo.width}: ${modeResults.length} pages, ${fails} FAIL, ${warns} WARN (${Math.round((Date.now() - begun) / 1000)} s)`);
    }
  });
  await Promise.all(workers);
  await browser.close();

  // 8. reports
  const order = (r) => [pages.findIndex((p) => p.id === r.page), langList.indexOf(r.lang), themes.indexOf(r.theme), widths.indexOf(r.width)];
  results.sort((a, b) => { const x = order(a); const y = order(b); for (let i = 0; i < 4; i += 1) if (x[i] !== y[i]) return x[i] - y[i]; return 0; });
  const logText = fs.readFileSync(serverLog, 'utf8');
  const serverProblems = [];
  const traces = logText.split('\n').filter((l) => /NetworkBlocked|Traceback|Internal Server Error|Error[:(]|database is locked/.test(l));
  for (const line of [...new Set(traces)].slice(0, 15)) serverProblems.push(line.slice(0, 240));
  const git = (args) => childProcess.spawnSync('git', args, { cwd: REPO, encoding: 'utf8' }).stdout.trim();
  const run_ = {
    name: options.name, started: started.toISOString(), commit: git(['rev-parse', '--short', 'HEAD']), dirty: git(['status', '--porcelain', '--untracked-files=no']).length > 0,
    pythonInfo, pageCount: pages.length + skipped.length, skipped: skipped.map((p) => ({ id: p.id, lane: p.lane, skip: p.skip })), themes, widths, langs: langList,
    modes: combos, unmapped: [...unmapped].sort(), serverProblems, redis: options.redisUrl ? 'given' : 'throw-away',
  };
  writeReports(outDir, run_, results);
  if (!options.keep) {
    for (const child of children) { try { child.kill('SIGTERM'); } catch { /* gone */ } }
  }
  const summary = summarize(results);
  console.log(`\n${summary.distinct.FAIL} FAIL (${summary.counts.FAIL} occurrences), ${summary.distinct.WARN} WARN, ${summary.distinct.INFO} INFO`);
  console.log(`report: ${path.join(outDir, 'report.md')}\ncontact sheet: ${path.join(outDir, 'index.html')}`);
  if (skipped.length) console.log(`${skipped.length} page(s) are not in this tree: ${skipped.map((p) => p.id).join(', ')}`);
  if (summary.distinct.FAIL) return 1;
  if (options.failOnWarn && summary.distinct.WARN) return 1;
  return 0;
}

// ---- one mode: a language, a theme and a width, over all pages ------------------------------------------------

function contextOptions(playwright, combo) {
  const devices = playwright.devices;
  const common = { locale: combo.lang === 'en' ? 'en-US' : 'zh-CN', timezoneId: 'UTC', reducedMotion: 'reduce', deviceScaleFactor: 1, colorScheme: 'light' };
  if (combo.width === 375) return { ...devices['iPhone SE'], ...common, viewport: { width: 375, height: 667 } };
  if (combo.width === 768) return { ...devices['iPad Mini'], ...common, viewport: { width: 768, height: 1024 } };
  return { ...common, viewport: { width: combo.width, height: 900 } };
}

async function installRoutes(ctx, context) {
  const { assets, unmapped, base } = ctx;
  await context.route('**/*', async (route) => {
    const request = route.request();
    const url = request.url();
    if (url.startsWith(base) || url.startsWith('data:') || url.startsWith('blob:')) return route.continue();
    const bare = url.split('?')[0].split('#')[0];
    const cors = { 'access-control-allow-origin': '*' };
    const hit = assets && (assets.get(url) || assets.get(bare));
    if (hit) return route.fulfill({ status: 200, body: hit.body, contentType: hit.type, headers: cors });
    if (/images\.evetech\.net/.test(url)) return route.fulfill({ status: 200, body: PLACEHOLDER_SVG, contentType: 'image/svg+xml', headers: cors });
    if (/fonts\.googleapis\.com/.test(url)) return route.fulfill({ status: 200, body: '', contentType: 'text/css', headers: cors });
    if (/fonts\.gstatic\.com/.test(url)) return route.fulfill({ status: 204, body: '', headers: cors });
    unmapped.add(bare);
    const type = /\.css$/.test(bare) ? 'text/css' : (/\.js$/.test(bare) ? 'application/javascript' : 'text/plain');
    return route.fulfill({ status: /\.(css|js)$/.test(bare) ? 200 : 204, body: '', contentType: type, headers: cors });
  });
}

async function setTheme(ctx, context, combo, entry) {
  // Alliance Auth's own theme form: choosing a theme there stores it for the logged-in user. Alliance Auth
  // swallows a failed save (for example a locked SQLite file under load), so the result is checked and the
  // choice is repeated a few times before the run gives up.
  const page = await context.newPage();
  const want = THEME_ATTRS[combo.theme];
  let last = '';
  try {
    for (let attempt = 1; attempt <= 5; attempt += 1) {
      await page.goto(`${ctx.base}/fleetops/`, { waitUntil: 'load', timeout: ctx.options.timeout });
      const hook = ctx.seed.themes[combo.theme];
      if (!hook) throw new Error(`the theme ${combo.theme} is not registered in this Alliance Auth`);
      const found = await page.evaluate(() => !!document.querySelector('form[action^="/theme/"] select[name="theme"]'));
      if (!found) {
        last = 'the theme form of Alliance Auth is not on the page';
        await sleep(300 * attempt);
        continue;
      }
      await Promise.all([
        page.waitForNavigation({ waitUntil: 'load', timeout: ctx.options.timeout }),
        page.evaluate((value) => {
          const form = document.querySelector('form[action^="/theme/"]');
          form.querySelector('select[name="theme"]').value = value;
          form.submit();
        }, hook),
      ]);
      await page.goto(`${ctx.base}/fleetops/`, { waitUntil: 'load', timeout: ctx.options.timeout });
      const attrs = await page.evaluate(() => ({ theme: document.documentElement.getAttribute('data-theme'), bs: document.documentElement.getAttribute('data-bs-theme') }));
      if (attrs.theme === want['data-theme'] && (!want['data-bs-theme'] || attrs.bs === want['data-bs-theme'])) return;
      last = `the theme ${combo.theme} was not applied (html data-theme=${attrs.theme}, data-bs-theme=${attrs.bs})`;
      await sleep(300 * attempt);
    }
    throw new Error(last);
  } finally {
    await page.close();
  }
}

async function runMode(ctx, combo, pages, slot) {
  const { options, seed, base } = ctx;
  const context = await ctx.browser.newContext(contextOptions(ctx.playwright, combo));
  context.setDefaultTimeout(options.timeout);
  await installRoutes(ctx, context);
  const out = [];
  try {
    const roles = [...new Set(pages.map((p) => p.role))];
    for (const role of roles) {
      const logins = seed.logins[role];
      const rolePages = pages.filter((p) => p.role === role);
      const failAll = (message) => rolePages.forEach((p) => out.push(harnessFailure(p, combo, message)));
      if (!logins) { failAll(`no login for the role ${role}`); continue; }
      await context.clearCookies();
      await context.addCookies([
        { name: 'sessionid', value: logins[slot % logins.length], url: base },
        { name: seed.language_cookie || 'django_language', value: combo.lang, url: base },
      ]);
      try {
        await setTheme(ctx, context, combo, rolePages[0]);
      } catch (error) {
        failAll(`theme switch failed: ${error.message}`);
        continue;
      }
      for (const entry of rolePages) out.push(await checkPage(ctx, context, combo, entry));
    }
  } finally {
    await context.close();
  }
  return out;
}

function harnessFailure(entry, combo, message) {
  return {
    page: entry.id, lane: entry.lane, role: entry.role, url: entry.url, ...combo, status: 0, expected: entry.status,
    findings: [{ check: 'harness', level: 'FAIL', selector: '', message, count: 1 }], screenshot: '',
  };
}

// ---- one page ----------------------------------------------------------------------------------------------------

const FOCUSABLE = 'a[href], button, input:not([type=hidden]), select, textarea, summary, [tabindex]';

async function checkPage(ctx, context, combo, entry) {
  const { options, base, scripts, rules, outDir } = ctx;
  const started = Date.now();
  const result = { page: entry.id, lane: entry.lane, role: entry.role, url: entry.url, ...combo, status: 0, expected: entry.status, findings: [], screenshot: '' };
  const add = (check, level, message, extra = {}) => result.findings.push({ check, level, selector: '', message, count: 1, ...extra });
  const page = await context.newPage();
  const consoleSeen = new Set();
  page.on('console', (message) => {
    if (message.type() !== 'error') return;
    const text = message.text();
    const location = message.location().url || '';
    if ([...ctx.unmapped].some((u) => text.includes(u) || location.includes(u))) return;
    // Failed loads of local files are reported by the response listener with their address.
    if (/Failed to load resource/.test(text) && location.startsWith(base)) return;
    if (consoleSeen.has(text)) return;
    consoleSeen.add(text);
    add('console', 'FAIL', `console error: ${text.slice(0, 200)}${location ? ` (${location.replace(base, '')})` : ''}`);
  });
  page.on('pageerror', (error) => add('console', 'FAIL', `script error: ${String(error.message).slice(0, 200)}`));
  page.on('response', (response) => {
    const url = response.url();
    if (!url.startsWith(base) || response.status() < 400) return;
    if (/\/favicon\.ico$/.test(url)) return;
    const request = response.request();
    if (request.resourceType() === 'document' && request.frame() === page.mainFrame() && response.status() === entry.status) return;
    add('http', 'FAIL', `${response.status()} for ${url.replace(base, '')}`);
  });
  page.on('requestfailed', (request) => {
    const url = request.url();
    if (url.startsWith(base) && !/ERR_ABORTED/.test(request.failure()?.errorText || '')) add('http', 'FAIL', `request failed: ${url.replace(base, '')} (${request.failure()?.errorText})`);
  });
  try {
    const response = await page.goto(base + entry.url, { waitUntil: 'load', timeout: options.timeout });
    result.status = response ? response.status() : 0;
    const finalPath = new URL(page.url()).pathname;
    const wantPath = entry.url.split('?')[0];
    if (finalPath !== wantPath) {
      add('status', 'FAIL', `redirected to ${finalPath} instead of showing ${wantPath}`);
    } else if (result.status !== entry.status) {
      add('status', 'FAIL', `answered ${result.status}, ${entry.status} was expected`);
    }
    await settle(page);
    const shown = await page.evaluate(() => ({ theme: document.documentElement.getAttribute('data-theme'), bs: document.documentElement.getAttribute('data-bs-theme') }));
    const wanted = THEME_ATTRS[combo.theme];
    if (shown.theme && (shown.theme !== wanted['data-theme'] || (wanted['data-bs-theme'] && shown.bs !== wanted['data-bs-theme']))) {
      add('harness', 'FAIL', `the page shows the theme ${shown.theme} instead of ${combo.theme}`);
    }

    for (const name of entry.states || []) {
      const state = states[name];
      if (!state) { add('state', 'FAIL', `the state ${name} is not defined in states.js`); continue; }
      try {
        const outcome = await state({ page, entry, lang: combo.lang, base });
        await settle(page);
        if (outcome && outcome.expect && combo.lang === 'en') {
          const text = await page.evaluate(() => document.body.innerText);
          if (!outcome.expect.test(text)) add('state', 'FAIL', `after the state ${name} the page does not show the expected message ${outcome.expect}`);
        }
        if (outcome && outcome.expect && combo.lang !== 'en') {
          const shown = await page.evaluate((sel) => !!document.querySelector(sel), ERROR_ELEMENTS);
          if (!shown) add('state', 'FAIL', `after the state ${name} the page shows no error`);
        }
      } catch (error) {
        add('state', 'FAIL', `the state ${name} failed: ${String(error.message).split('\n')[0]}`);
      }
    }

    await page.addScriptTag({ path: scripts.contrast });
    await page.addScriptTag({ path: scripts.checks });
    const contrast = await page.evaluate(() => window.__foContrast.run());
    result.contrastChecked = contrast.checked;
    result.contrastExempt = contrast.exempt;
    result.findings.push(...contrast.findings);
    const checks = await page.evaluate((opts) => window.__foChecks.run(opts), { width: combo.width, forbiddenClassPatterns: rules.forbidden_class_patterns });
    result.findings.push(...checks.findings);

    // Motion: with reduced motion asked for, nothing may be running.
    const animations = await page.evaluate(() => document.getAnimations().filter((a) => a.playState === 'running' && a.effect && a.effect.target)
      .map((a) => `${a.effect.target.tagName.toLowerCase()}${a.effect.target.className && typeof a.effect.target.className === 'string' ? '.' + a.effect.target.className.trim().split(/\s+/).slice(0, 2).join('.') : ''}`));
    for (const target of [...new Set(animations)].slice(0, 5)) add('motion', 'FAIL', `an animation is running although reduced motion is asked for`, { selector: target });
    const livedot = await page.evaluate(() => [...document.querySelectorAll('.fo-live-dot')].some((el) => getComputedStyle(el).animationName !== 'none'));
    if (livedot) add('motion', 'FAIL', 'the live dot still animates although reduced motion is asked for', { selector: '.fo-live-dot' });

    if (options.focus) result.focusChecked = await focusCheck(page, add);

    if (options.shots === 'all' || (options.shots === 'fail' && result.findings.some((f) => f.level === 'FAIL'))) {
      result.screenshot = await screenshot(page, outDir, combo, entry.id);
    }
  } catch (error) {
    add('harness', 'FAIL', `the page could not be checked: ${String(error.message).split('\n')[0]}`);
    if (options.shots !== 'none') {
      try { result.screenshot = await screenshot(page, outDir, combo, entry.id); } catch { /* nothing to show */ }
    }
  } finally {
    await page.close();
  }
  result.durationMs = Date.now() - started;
  return result;
}

async function settle(page) {
  try { await page.waitForLoadState('networkidle', { timeout: 6000 }); } catch { /* polling pages never go quiet */ }
  try { await page.evaluate(() => document.fonts && document.fonts.ready); } catch { /* page is gone */ }
  await page.waitForTimeout(120);
}

async function focusCheck(page, add) {
  // Returns how many controls were reached.
  // Tab through the controls of the FleetOps container: each must show where the focus is.
  await page.keyboard.press('Tab');
  const started = await page.evaluate((selector) => {
    const root = document.querySelector('.fleetops');
    if (!root) return false;
    const visible = (el) => el.checkVisibility && el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true }) && !el.closest('[inert]');
    const first = [...root.querySelectorAll(selector)].find((el) => !el.matches(':disabled') && el.tabIndex >= 0 && visible(el));
    if (!first) return false;
    first.focus();
    return true;
  }, FOCUSABLE);
  if (!started) return 0;
  const seen = new Set();
  let reached = 0;
  for (let i = 0; i < 40; i += 1) {
    const info = await page.evaluate(() => {
      const el = document.activeElement;
      const root = document.querySelector('.fleetops');
      if (!el || el === document.body || !root || !root.contains(el)) return null;
      const cs = getComputedStyle(el);
      const path = [];
      for (let n = el; n && n.nodeType === 1 && path.length < 3; n = n.parentElement) {
        path.unshift(n.tagName.toLowerCase() + (n.id ? '#' + n.id : '') + ([...n.classList].slice(0, 2).map((c) => '.' + c).join('')));
        if (n.id) break;
      }
      // An outline outside the control (offset 0 or more) is drawn on what lies behind the control; one inside it
      // (a negative offset) is drawn on the control itself.
      const behind = parseFloat(cs.outlineOffset) >= 0 && el.parentElement ? el.parentElement : el;
      const { candidates } = window.__foContrast.backgrounds(behind);
      const outlineColour = window.__foContrast.rgba(cs.outlineColor);
      const ratios = candidates.map((bg) => window.__foContrast.ratio(window.__foContrast.over(outlineColour, bg), bg));
      return {
        selector: path.join(' > '), text: (el.innerText || el.value || el.getAttribute('aria-label') || '').trim().slice(0, 40),
        outlineStyle: cs.outlineStyle, outlineWidth: parseFloat(cs.outlineWidth), boxShadow: cs.boxShadow, ratio: Math.min(...ratios),
      };
    });
    if (!info) break;
    const key = info.selector + info.text;
    if (seen.has(key)) break;
    seen.add(key);
    reached += 1;
    const hasOutline = info.outlineStyle !== 'none' && info.outlineWidth > 0;
    const hasShadow = info.boxShadow && info.boxShadow !== 'none';
    if (!hasOutline && !hasShadow) {
      add('focus', 'FAIL', 'the focused control shows no focus indicator', { selector: info.selector, text: info.text });
    } else if (hasOutline && info.outlineStyle !== 'auto' && info.ratio < 3) {
      add('focus', 'FAIL', `the focus outline has ${info.ratio.toFixed(2)}:1 against the background, 3:1 is needed`, { selector: info.selector, text: info.text });
    }
    await page.keyboard.press('Tab');
  }
  await page.evaluate(() => { if (document.activeElement && document.activeElement.blur) document.activeElement.blur(); });
  return reached;
}

async function screenshot(page, outDir, combo, id) {
  const rel = path.join(combo.lang, combo.theme, String(combo.width), `${id}.jpg`);
  const file = path.join(outDir, rel);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  try {
    // Alliance Auth scrolls the page area, not the document: make the window as tall as the content.
    const height = await page.evaluate(() => {
      let area = document.querySelector('.fleetops');
      while (area && area !== document.body && !/(auto|scroll)/.test(getComputedStyle(area).overflowY)) area = area.parentElement;
      const top = area && area !== document.body ? area.getBoundingClientRect().top + window.scrollY : 0;
      return Math.max(document.documentElement.scrollHeight, area && area !== document.body ? area.scrollHeight + top : 0);
    });
    const viewport = page.viewportSize();
    await page.setViewportSize({ width: viewport.width, height: Math.min(Math.max(height, viewport.height), 7000) });
    await page.waitForTimeout(80);
    await page.screenshot({ path: file, type: 'jpeg', quality: 70, fullPage: true });
    await page.setViewportSize(viewport);
  } catch (error) {
    console.error(`screenshot of ${id} failed: ${String(error.message).split('\n')[0]}`);
    return '';
  }
  return rel.split(path.sep).join('/');
}

main().then((code) => process.exit(code), (error) => { console.error(error); process.exit(2); });
