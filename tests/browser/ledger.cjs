/** Real-browser acceptance for an isolated examples.ledger_demo fixture.
 * Usage: node tests/browser/ledger.cjs /path/to/demo-manifest.json
 * Requires an installed playwright package and local Chrome. No app dependency.
 */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { execFileSync } = require('node:child_process');
const { chromium } = require('playwright');

let browser;
(async () => {
  const fixture = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  assert.equal(fixture.simulated, true, 'Browser signing tests require an isolated demo.');
  assert.ok(path.basename(fixture.root).startsWith('noname-ledger-demo-'));
  browser = await chromium.launch({headless:true, channel:'chrome'});
  const context = await browser.newContext({viewport:{width:1440,height:1050}, timezoneId:'Asia/Shanghai'});
  const page = await context.newPage();
  const errors = [];
  const mutate = code => execFileSync(process.env.NONAME_PYTHON || '.venv/bin/python',
    ['-c', 'import sys,json\nfrom pathlib import Path\nfrom noname_harness.store import HarnessStore\n' + code, fixture.db], {encoding:'utf8'});
  const appendEvidence = () => mutate('with HarnessStore(Path(sys.argv[1])) as s:\n s.append_event("browser-updates", "human.note", {"text":"New isolated evidence"})');
  const headers = {'Origin':new URL(fixture.url).origin,
    'X-NoName-Token':new URL(fixture.url).pathname.split('/')[1]};
  page.on('pageerror', error => errors.push(error.message));
  const navigate = async key => {
    await page.locator(`[data-nav][href="#${key}"]`).click();
    await page.waitForFunction(key => !document.getElementById(key).hidden, key);
  };
  await page.goto(fixture.url);
  assert.equal(await page.locator('[data-panel]:visible').count(), 1);
  assert.equal(await page.locator('#now pre:visible').count(), 0);
  assert.match(await page.locator('#now').innerText(), /SQLite/);
  const fields = await page.locator('#now .state-card .readable dt').allTextContents();
  assert.deepEqual(fields.slice(0, 2), ['内容', '理由']);

  const search = page.getByRole('searchbox');
  await search.fill('SQLite');
  assert.equal(await page.locator('#now [data-search]:visible').count(), 1);
  await search.fill('完全不存在的内容');
  await page.locator('#filter-empty').waitFor({state:'visible'});
  await page.getByRole('button', {name:'清除', exact:true}).click();
  assert.equal(await page.locator('#now [data-search]:visible').count(), 2);
  await page.evaluate(() => document.activeElement.blur());
  const initialRevision = await page.locator('body').getAttribute('data-ledger-revision');
  appendEvidence();
  await Promise.all([page.waitForEvent('load'), page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')))]);
  assert.notEqual(await page.locator('body').getAttribute('data-ledger-revision'), initialRevision);

  await navigate('archive');
  assert.equal(await page.locator('#archive [data-signal="record"]:visible').count(), 0);
  await page.getByLabel('活动类型', {exact:true}).selectOption('all');
  assert.ok(await page.locator('#archive [data-signal="record"]:visible').count() >= 4);
  await search.fill('演示验证');
  assert.equal(await page.locator('#archive [data-search]:visible').count(), 4);
  await page.getByRole('button', {name:'清除', exact:true}).click();

  await navigate('inbox');
  let form = page.locator('.review-form[data-kind="state_proposal"]').first();
  await form.locator('[name="reviewer_id"]').fill('隔离浏览器验收（模拟）');
  await form.locator('[name="reason"]').fill('确认工作区内的模拟修正');
  appendEvidence();
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
  await page.locator('#live-updates').waitFor({state:'visible'});
  assert.equal(await form.locator('[name="reason"]').inputValue(), '确认工作区内的模拟修正');
  await form.locator('.edit-details > summary').click();
  await form.locator('[data-edit-field="text"]').fill('数据存储改用 PostgreSQL，先保留迁移回退。');
  await page.reload();
  await page.waitForSelector('#inbox:not([hidden])');
  form = page.locator('.review-form[data-kind="state_proposal"]').first();
  assert.equal(await form.locator('[name="reviewer_id"]').inputValue(), '隔离浏览器验收（模拟）');
  await form.locator('.edit-details > summary').click();
  assert.match(await form.locator('[data-edit-field="text"]').inputValue(), /迁移回退/);
  const structured = JSON.parse(await form.locator('[name="edited_content"]').inputValue());
  assert.match(structured.text, /迁移回退/);
  assert.equal(structured.reason, '开始支持多人协作，需要共享数据库。');
  await Promise.all([page.waitForEvent('load'), form.locator('button[value="edit"]').click()]);
  assert.match(await page.locator('#review-notice').innerText(), /修改已确认并记住/);
  await navigate('now');
  assert.match(await page.locator('#now').innerText(), /PostgreSQL，先保留迁移回退/);
  assert.doesNotMatch(await page.locator('#now').innerText(), /首版使用 SQLite/);
  await navigate('history');
  await page.locator('.diff-group > details > summary').first().click();
  assert.match(await page.locator('#history').innerText(), /SQLite/);
  await search.fill('SQLite');
  assert.ok(await page.locator('#history [data-search]:visible').count() > 0);
  assert.equal(await page.locator('#filter-empty').isVisible(), false);
  assert.match(await page.locator('#history').innerText(), /首版使用 SQLite/);
  await page.getByRole('button', {name:'清除', exact:true}).click();

  // Two unapproved corrections share a topic. Draft tokens must survive a
  // new approved head and repeated autosaves until explicit reconciliation.
  const peers = JSON.parse(mutate('with HarnessStore(Path(sys.argv[1])) as s:\n e=s.append_event("browser-alternatives", "codex.note", {"text":"Compare alternative decisions"})\n a=s.create_proposal("high", "alternatives", {"text":"SQLite alternative"}, [e.id])\n b=s.create_proposal("high", "alternatives", {"text":"PostgreSQL alternative"}, [e.id])\n print(json.dumps([a["id"], b["id"]]))'));
  await page.reload();
  await navigate('inbox');
  const alternatives = page.locator('.review-card').filter({hasText:'同一主题还有 1 条待确认内容'});
  assert.equal(await alternatives.count(), 2);
  const older = page.locator(`.review-form[data-id="${peers[0]}"]`);
  const newer = page.locator(`.review-form[data-id="${peers[1]}"]`);
  await older.locator('[name="reviewer_id"]').fill('旧草稿验收（模拟）');
  await older.locator('[name="reason"]').fill('旧草稿理由');
  const newerPayload = await newer.evaluate(form => ({kind:form.dataset.kind,id:form.dataset.id,
    expected_version:form.dataset.version,expected_head_id:form.dataset.expectedHeadId,
    action:'accept',reviewer_id:'另一页（模拟）'}));
  assert.equal((await context.request.post(new URL('api/review',fixture.url).href,{data:newerPayload,headers})).status(),200);
  await page.reload();
  let restored = page.locator(`.review-form[data-id="${peers[0]}"]`);
  await restored.locator('.reconcile-draft').waitFor({state:'visible'});
  await restored.locator('[name="reason"]').fill('只修改理由，仍未核对新版本');
  await page.reload();
  restored = page.locator(`.review-form[data-id="${peers[0]}"]`);
  assert.equal(await restored.locator('.reconcile-draft').isVisible(),true);
  await restored.locator('button[value="accept"]').click();
  assert.match(await restored.locator('.review-feedback').innerText(),/请先核对最新内容/);
  await restored.locator('.reconcile-draft').click();
  assert.equal(await restored.locator('.reconcile-draft').isVisible(),false);
  await Promise.all([page.waitForEvent('load'), restored.locator('button[value="reject"]').click()]);

  // A second page changes a candidate after the first page captured its version.
  await navigate('inbox');
  let cardForm = page.locator('.review-form[data-kind="taste_card_candidate"]');
  await cardForm.locator('[name="reviewer_id"]').fill('冲突验收（模拟）');
  const staleRequest = await cardForm.evaluate(form => ({
    kind:form.dataset.kind,id:form.dataset.id,expected_version:form.dataset.version,
    action:'edit',reviewer_id:'另一页（模拟）',reason:'先行修改隔离候选',
    edited_content:{...JSON.parse(form.elements.namedItem('edited_content').value),attitude:'另一页先行修正的品味'},
  }));
  const response = await context.request.post(new URL('api/review',fixture.url).href, {
    data:staleRequest, headers,
  });
  assert.equal(response.status(), 200);
  await cardForm.locator('button[value="accept"]').click();
  await cardForm.locator('.review-feedback.error').waitFor({state:'visible'});
  assert.match(await cardForm.locator('.review-feedback').innerText(), /已变化|已被审核/);
  assert.equal(await cardForm.locator('[name="reviewer_id"]').inputValue(), '冲突验收（模拟）');
  assert.equal(await cardForm.locator('.reload-page').isVisible(), true);

  // Narrow layout plus actual keyboard access.
  await page.setViewportSize({width:390,height:844});
  await navigate('now');
  await page.evaluate(() => document.activeElement.blur());
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
  await page.keyboard.press('Tab');
  assert.equal(await page.evaluate(() => !!document.activeElement && document.activeElement !== document.body), true);

  const staticPage = await context.newPage();
  const externalRequests = [];
  staticPage.on('request', request => {if (/^https?:/.test(request.url())) externalRequests.push(request.url());});
  await staticPage.goto(pathToFileURL(fixture.snapshot).href);
  await staticPage.locator('[data-nav][href="#inbox"]').click();
  await staticPage.locator('#inbox').waitFor({state:'visible'});
  assert.equal(await staticPage.locator('.review-form').count(), 0);
  assert.equal(await staticPage.locator('#inbox').isVisible(), true);
  assert.equal(await staticPage.locator('body').getAttribute('data-review-token'), null);
  assert.deepEqual(externalRequests, []);
  await staticPage.goto(pathToFileURL(fixture.empty).href);
  assert.match(await staticPage.locator('#now').innerText(), /还没有需要接续的工作/);
  assert.equal(await staticPage.locator('#now .state-card').count(), 0);
  const noScript = await browser.newContext({javaScriptEnabled:false});
  const fallback = await noScript.newPage();
  await fallback.goto(pathToFileURL(fixture.snapshot).href);
  assert.equal(await fallback.locator('[data-panel]:visible').count(), 5);
  assert.equal(await fallback.locator('.review-form').count(), 0);
  await noScript.close();
  assert.deepEqual(errors, []);
  await browser.close();
  console.log('PASS: navigation, current-state reading, literal/history search, archive filters, plain-text editing, draft restoration, signed correction, version history, same-topic alternatives, stale-draft reconciliation, stale-review recovery, automatic updates with draft protection, 390px layout, keyboard access, offline search, empty state, no-JS fallback; no browser errors.');
})().catch(async error => {console.error(error);if(browser)await browser.close();process.exitCode=1});
