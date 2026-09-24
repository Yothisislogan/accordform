// Run with Node and Playwright available: node tests/hedge_session_browser.cjs
// All API responses are synthetic; no Hedge or Google connections are made.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

(async () => {
  const root = path.resolve(__dirname, '..');
  const server = http.createServer((req, res) => {
    const file = req.url === '/hedge' ? 'static/hedge.html' :
      req.url === '/static/styles.css' ? 'static/styles.css' : null;
    if (!file) { res.writeHead(404).end(); return; }
    res.setHeader('Content-Type', file.endsWith('.css') ? 'text/css' : 'text/html');
    res.end(fs.readFileSync(path.join(root, file)));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({
    headless: true,
    ...(process.env.CHROMIUM_EXECUTABLE ? { executablePath: process.env.CHROMIUM_EXECUTABLE } : {}),
    args: JSON.parse(process.env.CHROMIUM_ARGS || '[]'),
  });
  const context = await browser.newContext();
  let completed = 0;

  async function scenario(settings, check) {
    const page = await context.newPage();
    const requests = [], errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.addInitScript(() => {
      window.fetchOptions = [];
      const original = window.fetch.bind(window);
      window.fetch = (url, options) => {
        window.fetchOptions.push({ url, cache: options?.cache, credentials: options?.credentials });
        return original(url, options);
      };
    });
    await page.route('**/*', async route => {
      const request = route.request(), url = new URL(request.url());
      if (url.origin !== base) { await route.abort(); return; }
      if (!url.pathname.startsWith('/api/') && !url.pathname.startsWith('/auth/')) {
        await route.continue(); return;
      }
      requests.push({ path: url.pathname, method: request.method(), headers: request.headers() });
      let status = 200, body = {};
      if (url.pathname === '/auth/me') {
        status = settings.loggedOut ? 401 : 200;
        body = settings.loggedOut ? { authenticated: false } :
          { authenticated: true, email: 'test@example.com', role: settings.role || 'admin' };
      } else if (url.pathname === '/api/config') {
        if (settings.configWait) await settings.configWait;
        status = settings.configStatus || 200;
        body = settings.noToken ? {} : { csrf_token: settings.token || 'synthetic-csrf' };
      } else if (url.pathname === '/api/hedge/status') {
        body = settings.connection || { signed_in: false, auth_mode: 'oauth', env: 'prod' };
      } else if (url.pathname === '/api/forms') body = { forms: [] };
      else if (url.pathname === '/api/hedge/pipeline') body = { submissions: [] };
      else if (url.pathname === '/api/hedge/preview-body') {
        if (settings.stale) {
          status = 403; body = { error: 'invalid or missing CSRF token' };
        } else if (settings.upstreamUnauthorized) {
          status = 401; body = { error: 'Hedge credentials rejected', signed_in: false };
        } else body = { body: {}, missing: [], address_status: 'complete' };
      } else throw new Error('Unexpected API request: ' + url.pathname);
      await route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) });
    });
    try {
      await page.goto(base + '/hedge');
      await check(page, requests);
      assert.deepEqual(errors, []);
      const options = await page.evaluate(() => window.fetchOptions);
      assert(options.every(option => option.cache === 'no-store' && option.credentials === 'same-origin'));
      completed++;
    } finally { await page.close(); }
  }

  try {
    await scenario({ loggedOut: true }, async (page, requests) => {
      await page.locator('#wit-session-block').waitFor({ state: 'visible' });
      assert.equal(await page.locator('#signin-block').isVisible(), false);
      assert.equal(await page.getByRole('link', { name: 'Sign in to WiT Forms' }).getAttribute('href'), '/auth/login');
      await page.locator('#preview-body-btn').click();
      await page.locator('#notice').waitFor({ state: 'visible' });
      assert.equal(requests.filter(r => r.method === 'POST').length, 0);
      assert.equal(requests.some(r => r.path === '/api/config'), false);
    });
    for (const settings of [{ configStatus: 401 }, { configStatus: 503 }, { noToken: true }]) {
      await scenario(settings, async (page, requests) => {
        await page.locator('#wit-session-block').waitFor({ state: 'visible' });
        assert.equal(await page.locator('#signin-block').isVisible(), false);
        assert.equal(requests.some(r => r.path === '/api/hedge/status'), false);
      });
    }
    let releaseConfig;
    const configWait = new Promise(resolve => { releaseConfig = resolve; });
    await scenario({ configWait }, async (page, requests) => {
      assert.equal(await page.locator('#signin-block').isVisible(), false);
      await page.locator('#preview-body-btn').click();
      await page.locator('#notice').waitFor({ state: 'visible' });
      assert.equal(requests.filter(r => r.method === 'POST').length, 0);
      releaseConfig();
      await page.locator('#signin-block').waitFor({ state: 'visible' });
      await page.locator('#preview-body-btn').click();
      await page.locator('#payload').waitFor({ state: 'visible' });
      const post = requests.find(r => r.method === 'POST');
      assert.equal(post.headers['x-csrf-token'], 'synthetic-csrf');
    });
    for (const signedIn of [true, false]) {
      await scenario({ connection: { signed_in: signedIn, auth_mode: 'client_credentials', env: 'prod',
        ...(signedIn ? {} : { error: 'Hedge rejected the machine credentials.' }) } }, async page => {
        await page.waitForFunction(() => document.querySelector('#signed-state').textContent.includes('verified') ||
          document.querySelector('#signed-state').textContent.includes('rejected'));
        assert.equal(await page.locator('#signin-block').isVisible(), false);
        assert.equal(await page.locator('#wit-session-block').isVisible(), false);
      });
    }
    await scenario({ role: 'user' }, async page => {
      await page.waitForFunction(() => document.querySelector('#signed-state').textContent.includes('administrator'));
      assert.equal(await page.locator('#signin-block').isVisible(), false);
    });
    const stale = { stale: true };
    await scenario(stale, async (page, requests) => {
      await page.locator('#signin-block').waitFor({ state: 'visible' });
      await page.locator('#preview-body-btn').click();
      await page.locator('#wit-session-block').waitFor({ state: 'visible' });
      assert.match(await page.locator('#notice').innerText(), /session changed/);
      await page.locator('#preview-body-btn').click();
      assert.equal(requests.filter(r => r.method === 'POST').length, 1, 'Never replay a rejected action automatically');
      stale.stale = false;
      stale.token = 'fresh-synthetic-csrf';
      await page.locator('#reload-session-btn').click();
      await page.locator('#signin-block').waitFor({ state: 'visible' });
      await page.locator('#preview-body-btn').click();
      await page.locator('#payload').waitFor({ state: 'visible' });
      assert.equal(requests.filter(r => r.method === 'POST').length, 2);
      assert.equal(requests.filter(r => r.method === 'POST')[1].headers['x-csrf-token'], 'fresh-synthetic-csrf');
    });
    await scenario({ upstreamUnauthorized: true }, async page => {
      await page.locator('#signin-block').waitFor({ state: 'visible' });
      await page.locator('#preview-body-btn').click();
      await page.locator('#notice').waitFor({ state: 'visible' });
      assert.match(await page.locator('#notice').innerText(), /Hedge credentials rejected/);
      assert.equal(await page.locator('#wit-session-block').isVisible(), false);
    });
    console.log(`PASS: ${completed} browser session scenarios; no JavaScript errors or external requests.`);
  } finally {
    await browser.close();
    await new Promise(resolve => server.close(resolve));
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
