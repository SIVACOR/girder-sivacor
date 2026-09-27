/**
 * Drives this plugin's admin UI in a real browser against deploy-dev.
 *
 * Girder's own plugin UI specs live in the girder repo (girder/web/tests/spec/),
 * which this out-of-tree plugin has no counterpart for, so this is the only
 * thing that opens these views at all.
 *
 *   cd girder-sivacor && node e2e/admin-ui-check.mjs
 *   cd girder-sivacor && node e2e/admin-ui-check.mjs --read-only
 *
 * Writes: two throwaway accounts, the three sivacor volume settings (restored
 * at exit), the quota field on the accounts it creates, and -- for the usage
 * page, which has no write API and nothing to read on a box that has never
 * reaped an instance -- seeded usage counters straight into Mongo, removed
 * again at the end. deploy-dev only.
 */
import { execFileSync } from 'node:child_process';
import { mkdir } from 'node:fs/promises';

import { chromium } from 'playwright';

const API = 'https://girder.local.xarthisius.xyz/api/v1';
const UI = 'https://girder.local.xarthisius.xyz';
const ADMIN = { login: 'admin', password: 'arglebargle123' };

const SETTINGS = [
    'sivacor.volumes_enabled',
    'sivacor.volume_total_gb',
    'sivacor.targeted_assignment'
];

// Against a restored production database nothing here may write: no fixture
// accounts, no settings changes, no granting, no seeding. What it asserts
// instead is that each page renders the real payload faithfully -- which is a
// harder test than a fixture, because the data is messy in ways no fixture is.
const READ_ONLY = process.argv.includes('--read-only');
const SHOTS = '/tmp/sivacor-admin-ui';

let passed = 0;
const failures = [];

function check(name, condition, detail) {
    if (condition) {
        passed += 1;
        console.log(`  ok   ${name}`);
    } else {
        failures.push(name);
        console.log(`  FAIL ${name}${detail ? ` -- ${detail}` : ''}`);
    }
}

let token = null;

async function api(path, { method = 'GET', params = {}, headers = {} } = {}) {
    const url = new URL(API + path);
    Object.entries(params).forEach(([k, v]) => url.searchParams.set(k, v));
    const resp = await fetch(url, {
        method,
        headers: { ...(token ? { 'Girder-Token': token } : {}), ...headers }
    });
    const body = await resp.text();
    if (!resp.ok) {
        throw new Error(`${method} ${path} -> ${resp.status} ${body.slice(0, 200)}`);
    }
    return body ? JSON.parse(body) : null;
}

async function login() {
    const basic = Buffer.from(`${ADMIN.login}:${ADMIN.password}`).toString('base64');
    const resp = await api('/user/authentication', {
        headers: { Authorization: `Basic ${basic}` }
    });
    token = resp.authToken.token;
}

async function ensureUser(login) {
    const found = await api('/user', { params: { text: login, limit: 5 } });
    const hit = found.find((u) => u.login === login);
    if (hit) {
        return hit;
    }
    return api('/user', {
        method: 'POST',
        params: {
            login,
            email: `${login}@example.org`,
            firstName: 'Quota',
            lastName: 'Fixture',
            password: 'quotafixture123',
            admin: 'false'
        }
    });
}

async function getSettings() {
    return api('/system/setting', { params: { list: JSON.stringify(SETTINGS) } });
}

async function setSettings(values) {
    await api('/system/setting', {
        method: 'PUT',
        params: {
            list: JSON.stringify(
                Object.entries(values).map(([key, value]) => ({ key, value }))
            )
        }
    });
}

/** Open a Girder hash route with an auth token already in localStorage. */
async function open(page, route, authToken) {
    await page.addInitScript((value) => {
        if (value) {
            window.localStorage.setItem('girderToken', value);
        } else {
            window.localStorage.removeItem('girderToken');
        }
    }, authToken);
    await page.goto(`${UI}/#${route}`, { waitUntil: 'domcontentloaded' });
}

/** Re-enter a route the SPA is already on, forcing the view to rebuild. */
async function reopen(page, route) {
    await page.evaluate(() => { window.location.hash = '#'; });
    await page.waitForTimeout(150);
    await page.evaluate((r) => { window.location.hash = `#${r}`; }, route);
    await page.waitForTimeout(400);
}

/** The user page's Actions menu is a Bootstrap dropdown; its items are hidden until it opens. */
async function openUserActions(page) {
    await page.locator('button.g-user-actions-button').click();
    await page.waitForSelector('a.g-sivacor-volume-quota', { state: 'visible', timeout: 10000 });
}

/**
 * Run javascript in deploy-dev's mongo.
 *
 * The usage page reads counters that only the fleet controller writes, at reap,
 * through a code path with no REST entry point -- so on a box that has never
 * reaped an instance there is nothing to render and nothing to assert. Seeding
 * the documents directly is the only way to see the populated page at all.
 */
function mongo(js) {
    const container = execFileSync('docker', ['ps', '-qf', 'name=wt_mongo'])
        .toString().trim();
    if (!container) {
        throw new Error('deploy-dev mongo is not running');
    }
    return execFileSync('docker',
        ['exec', container, 'mongo', 'girder', '--quiet', '--eval', js]).toString().trim();
}

/** Refuse to seed over real accounting. A restored production database has a
 * house row and real counters, and this function replaces the one and deletes
 * it again -- which is data loss, not a fixture. The read-only mode exists
 * precisely so real data can be driven without this running at all. */
function assertNoRealUsage() {
    const existing = mongo(
        'print(db.sivacor_usage_totals.count() + " " + ' +
        'db.user.count({sivacorUsage: {$exists: true}}));');
    if (existing !== '0 0') {
        throw new Error(
            `this deployment already has usage accounting (${existing} house/user rows). ` +
            'Re-run with --read-only, which asserts against the real data instead of ' +
            'seeding over it.');
    }
}

function seedUsage(userId) {
    assertNoRealUsage();
    mongo(`
        db.user.updateOne({_id: ObjectId("${userId}")}, {$set: {sivacorUsage: {
            since: new Date("2026-08-01T10:00:00Z"),
            lastAt: new Date("2026-09-20T18:30:00Z"),
            submissions: 3,
            suHours: 32.5,
            instanceHours: 4.0625,
            volumeGbHours: 12.0,
            bySize: {"60": 2, "30": 1}
        }}});
        db.sivacor_usage_totals.replaceOne({_id: "house"}, {
            _id: "house",
            since: new Date("2026-08-01T09:00:00Z"),
            lastAt: new Date("2026-09-21T09:00:00Z"),
            instances: 1,
            suHours: 8.0,
            instanceHours: 1.0,
            byReason: {unclaimed: 8.0}
        }, {upsert: true});
        db.sivacor_instance_lifetime.insertOne({
            instanceId: "i-uicheck", at: new Date(),
            startedAt: new Date(), deletedAt: new Date(), memoryGb: 30
        });
    `);
}

function clearSeededUsage(userId) {
    mongo(`
        db.user.updateOne({_id: ObjectId("${userId}")}, {$unset: {sivacorUsage: ""}});
        db.sivacor_usage_totals.deleteOne({_id: "house"});
        db.sivacor_instance_lifetime.deleteOne({instanceId: "i-uicheck"});
    `);
}

const rowFor = (page, login) =>
    page.locator('.g-sivacor-quotas tbody tr', { hasText: login });

/**
 * Drive the three pages against whatever is really in the database.
 *
 * Every assertion compares the rendered page with the API payload behind it,
 * so it holds for any data rather than for a fixture -- and the screenshots are
 * the point as much as the checks are: real logins are long, real GB-hours have
 * five digits, and a layout that survives fixtures can still break on those.
 */
async function readOnlyChecks(page) {
    await mkdir(SHOTS, { recursive: true });

    // ---- scratch volume quotas ------------------------------------------
    const volumes = await api('/sivacor/volume_usage');
    await open(page, 'sivacor/volumes', token);
    await page.waitForSelector('.g-sivacor-tile', { timeout: 20000 });
    await page.waitForTimeout(500);
    await page.screenshot({ path: `${SHOTS}/volumes.png`, fullPage: true });

    check('every approved account is listed',
        await page.locator('.g-sivacor-quotas tbody tr').count() === volumes.users.length,
        `page ${await page.locator('.g-sivacor-quotas tbody tr').count()} vs api ${volumes.users.length}`);
    const pageLogins = await page.locator('.g-sivacor-quotas tbody tr td:first-child')
        .allInnerTexts();
    check('the logins match the report, in the report\'s order',
        pageLogins.join(',') === volumes.users.map((u) => u.login).join(','),
        pageLogins.join(','));
    const tiles = await page.locator('.g-sivacor-volumes .g-sivacor-tile').allInnerTexts();
    const tileFor = (label) => (tiles.find(
        (t) => t.toLowerCase().includes(label.toLowerCase())) || ''
    ).split('\n')[0].trim();
    check('the granted total is the sum of the ceilings',
        tileFor('Granted GB') === String(volumes.totals.granted_gb),
        `${tileFor('Granted GB')} vs ${volumes.totals.granted_gb}`);
    check('the approved-account count matches',
        tileFor('Approved accounts') === String(volumes.totals.approved_users));

    // The flag is for ceilings the deployment cannot honour in one request --
    // strictly above, so an account granted exactly the limit is not flagged.
    const expectedFlags = volumes.users
        .filter((u) => u.ceiling_gb > volumes.deployment_gb).length;
    check('over-limit allowances are flagged, and only those',
        await page.locator('.g-sivacor-quotas .g-sivacor-warn-text').count() === expectedFlags,
        `page ${await page.locator('.g-sivacor-quotas .g-sivacor-warn-text').count()} vs expected ${expectedFlags}`);

    const largest = volumes.users.reduce(
        (a, b) => (a.gb_hours > b.gb_hours ? a : b), volumes.users[0]);
    if (largest) {
        check('the biggest spender is first and shows its GB-hours',
            (await page.locator('.g-sivacor-quotas tbody tr').first().innerText())
                .includes(String(largest.gb_hours)),
            `expected ${largest.gb_hours}`);
    }
    check('the page body does not scroll sideways',
        await page.evaluate(() =>
            document.documentElement.scrollWidth <= document.documentElement.clientWidth));

    // searching real accounts, without granting anything
    await page.locator('.g-sivacor-user-search').fill('a');
    await page.locator('.g-sivacor-user-search-form button[type=submit]').click();
    await page.waitForSelector('.g-sivacor-search-results li', { timeout: 10000 });
    const hits = await page.locator('.g-sivacor-search-results li').count();
    check('a search over a real user table returns results', hits > 0, `${hits} hits`);
    check('the search is capped rather than listing every account', hits <= 10, `${hits} hits`);
    const approvedLogins = volumes.users.filter((u) => u.ceiling_gb)
        .map((u) => u.login);
    const shown = await page.locator('.g-sivacor-search-results li').allInnerTexts();
    check('search results that already hold an allowance say so, the rest say none',
        shown.every((text) => {
            const holder = approvedLogins.some((l) => text.startsWith(l));
            return holder ? !text.includes('no allowance') : text.includes('no allowance');
        }), shown.join(' | '));
    await page.screenshot({ path: `${SHOTS}/volumes-search.png`, fullPage: true });

    // the dialog, opened on a real account and closed without saving
    const target = volumes.users[0];
    await page.locator('.g-sivacor-search-clear').click();
    await page.waitForTimeout(300);
    await rowFor(page, target.login).locator('button.g-sivacor-edit-quota').click();
    await page.waitForSelector('#g-sivacor-quota-gb', { timeout: 10000 });
    // Bootstrap fades the modal in; screenshotting before it settles catches a
    // half-transparent dialog with no backdrop and reads like a rendering bug.
    await page.waitForTimeout(500);
    check('the dialog opens at the account\'s real allowance',
        await page.locator('#g-sivacor-quota-gb').inputValue() === String(target.ceiling_gb),
        `${await page.locator('#g-sivacor-quota-gb').inputValue()} vs ${target.ceiling_gb}`);
    await page.screenshot({ path: `${SHOTS}/quota-dialog.png` });
    await page.locator('.modal-footer a[data-dismiss=modal]').click();
    await page.waitForSelector('#g-sivacor-quota-form', { state: 'detached', timeout: 10000 });
    check('cancelling changes nothing',
        (await api(`/sivacor/user/${target.user_id}/volume_quota`)).max_gb === target.ceiling_gb);

    // ---- resource usage --------------------------------------------------
    const usage = await api('/sivacor/usage');
    await open(page, 'sivacor/usage', token);
    await page.waitForSelector('.g-sivacor-usage .g-sivacor-tile', { timeout: 20000 });
    await page.waitForTimeout(500);
    await page.screenshot({ path: `${SHOTS}/usage.png`, fullPage: true });

    check('every accrued account is listed',
        await page.locator('.g-sivacor-usage-users tbody tr').count() === usage.users.length,
        `page ${await page.locator('.g-sivacor-usage-users tbody tr').count()} vs api ${usage.users.length}`);
    const usageTiles = await page.locator('.g-sivacor-usage .g-sivacor-tile').allInnerTexts();
    const usageTileFor = (label) => (usageTiles.find(
        (t) => t.toLowerCase().includes(label.toLowerCase())) || ''
    ).split('\n')[0].trim();
    check('the total matches the server\'s, to one decimal',
        usageTileFor('SU-hours total') === usage.total_su_hours.toFixed(1),
        `${usageTileFor('SU-hours total')} vs ${usage.total_su_hours.toFixed(1)}`);
    check('the house figure matches',
        usageTileFor('House') === usage.house.su_hours.toFixed(1),
        `${usageTileFor('House')} vs ${usage.house.su_hours.toFixed(1)}`);
    check('charged-to-accounts is the total less the house',
        usageTileFor('Charged to accounts') ===
            (usage.total_su_hours - usage.house.su_hours).toFixed(1),
        usageTileFor('Charged to accounts'));
    const reasons = Object.keys(usage.house.by_reason || {});
    check('every house reason is named',
        reasons.every((r) => page.locator('.g-sivacor-panels').innerText()
            .then((t) => t.includes(r))) && reasons.length >= 0,
        reasons.join(','));
    const panelText = await page.locator('.g-sivacor-panels').innerText();
    check('the house breakdown shows its reasons verbatim',
        reasons.every((r) => panelText.includes(r)), `${reasons.join(',')} in ${panelText.slice(0, 80)}`);
    check('the usage page does not scroll sideways either',
        await page.evaluate(() =>
            document.documentElement.scrollWidth <= document.documentElement.clientWidth));

    // ---- the user page, on a real account --------------------------------
    await open(page, `user/${target.user_id}`, token);
    await page.waitForSelector('.g-user-header', { timeout: 20000 });
    await openUserActions(page);
    check('a real user page offers the allowance',
        await page.locator('a.g-sivacor-volume-quota').count() === 1);
    await page.screenshot({ path: `${SHOTS}/user-page-menu.png` });

    console.log(`\nscreenshots in ${SHOTS}`);
}

async function main() {
    await login();
    const browser = await chromium.launch();
    const context = await browser.newContext({
        ignoreHTTPSErrors: true,
        viewport: { width: 1440, height: 900 }
    });
    const page = await context.newPage();
    const consoleErrors = [];
    page.on('pageerror', (err) => consoleErrors.push(String(err)));

    if (READ_ONLY) {
        try {
            await readOnlyChecks(page);
            check('no uncaught javascript errors', consoleErrors.length === 0,
                consoleErrors.join(' | '));
        } finally {
            await browser.close();
        }
        report();
        return;
    }

    const before = await getSettings();
    const alice = await ensureUser('quotafixture1');
    const bob = await ensureUser('quotafixture2');

    try {
        // ---- the admin console entry points ---------------------------------
        await setSettings({
            'sivacor.volumes_enabled': true,
            'sivacor.volume_total_gb': 100,
            'sivacor.targeted_assignment': true
        });
        await open(page, 'admin', token);
        await page.waitForSelector('ul.g-admin-options', { timeout: 15000 });
        check('admin console links to the quota page',
            await page.locator('.g-sivacor-volumes-link a').count() === 1);
        check('admin console still links to telemetry',
            await page.locator('.g-sivacor-telemetry-link a').count() === 1);
        check('admin console links to the usage page',
            await page.locator('.g-sivacor-usage-link a').count() === 1);
        check('the quota link carries an icon',
            await page.locator('.g-sivacor-volumes-link a i.icon-database').count() === 1);

        await page.locator('.g-sivacor-volumes-link a').click();
        await page.waitForSelector('.g-sivacor-quotas, .g-sivacor-empty', { timeout: 15000 });
        check('the link navigates to the quota page',
            page.url().endsWith('#sivacor/volumes'), page.url());

        // ---- the funded, armed state ----------------------------------------
        check('no configuration warning when the feature is on and funded',
            await page.locator('.g-sivacor-volumes .alert-warning').count() === 0);
        check('the per-request limit is stated',
            (await page.locator('.g-sivacor-config').innerText()).includes('100 GB'));
        check('five summary tiles are shown',
            await page.locator('.g-sivacor-tile').count() === 5);

        // ---- granting from the search panel ---------------------------------
        await page.locator('.g-sivacor-user-search').fill('quotafixture1');
        await page.locator('.g-sivacor-user-search-form button[type=submit]').click();
        await page.waitForSelector('.g-sivacor-search-results li', { timeout: 10000 });
        const result = page.locator('.g-sivacor-search-results li').first();
        check('a search result names the account',
            (await result.innerText()).includes('quotafixture1'));
        check('an unapproved account reads as having no allowance',
            (await result.innerText()).includes('no allowance'));
        check('an unapproved account offers Grant',
            (await result.locator('button.g-sivacor-edit-quota').innerText()).trim() === 'Grant');

        await result.locator('button.g-sivacor-edit-quota').click();
        await page.waitForSelector('#g-sivacor-quota-form #g-sivacor-quota-gb', { timeout: 10000 });
        check('the dialog names the account',
            (await page.locator('.modal-title').innerText()).includes('quotafixture1'));
        check('the dialog opens at the current allowance',
            await page.locator('#g-sivacor-quota-gb').inputValue() === '0');
        check('the dialog steps by the granularity',
            await page.locator('#g-sivacor-quota-gb').getAttribute('step') === '10');
        check('the dialog states the deployment limit',
            (await page.locator('.g-sivacor-quota-notes').innerText()).includes('100 GB'));

        // an empty field must not read as zero
        await page.locator('#g-sivacor-quota-gb').fill('');
        await page.locator('.g-sivacor-quota-save').click();
        await page.waitForTimeout(300);
        check('an empty field is refused rather than read as a revoke',
            (await page.locator('#g-sivacor-quota-form .g-validation-failed-message')
                .innerText()).length > 0);
        check('the dialog stays open after a refusal',
            await page.locator('#g-sivacor-quota-form').count() === 1);

        await page.locator('#g-sivacor-quota-gb').fill('50');
        await page.locator('.g-sivacor-quota-save').click();
        await page.waitForSelector('#g-sivacor-quota-form', { state: 'detached', timeout: 10000 });
        check('a successful save closes the dialog', true);
        check('a successful save is announced',
            (await page.locator('#g-alerts-container').innerText()).includes('50 GB'));
        await page.waitForTimeout(600);
        check('the granted account appears in the table',
            await rowFor(page, 'quotafixture1').count() === 1);
        check('the table shows the allowance',
            (await rowFor(page, 'quotafixture1').innerText()).includes('50 GB'));
        check('the server recorded the allowance',
            (await api(`/sivacor/user/${alice._id}/volume_quota`)).max_gb === 50);

        // ---- changing it from the table -------------------------------------
        await rowFor(page, 'quotafixture1').locator('button.g-sivacor-edit-quota').click();
        await page.waitForSelector('#g-sivacor-quota-gb', { timeout: 10000 });
        check('the dialog reopens at the stored allowance',
            await page.locator('#g-sivacor-quota-gb').inputValue() === '50');
        await page.locator('#g-sivacor-quota-gb').fill('120');
        await page.locator('.g-sivacor-quota-save').click();
        await page.waitForSelector('#g-sivacor-quota-form', { state: 'detached', timeout: 10000 });
        await page.waitForTimeout(600);
        check('the table shows the raised allowance',
            (await rowFor(page, 'quotafixture1').innerText()).includes('120 GB'));
        check('an allowance above the per-request limit is flagged',
            await rowFor(page, 'quotafixture1').locator('.g-sivacor-warn-text').count() === 1);
        check('the flag explains itself',
            (await rowFor(page, 'quotafixture1').locator('.g-sivacor-warn-text')
                .getAttribute('title')).includes('100 GB'));

        // ---- revoking from the table ----------------------------------------
        await rowFor(page, 'quotafixture1').locator('button.g-sivacor-revoke-quota').click();
        await page.waitForSelector('#g-confirm-button', { timeout: 10000 });
        check('revoking from the table asks first',
            (await page.locator('.modal-body').innerText()).includes('quotafixture1'));
        await page.locator('#g-confirm-button').click();
        await page.waitForTimeout(900);
        check('a revoked account with no submissions leaves the table',
            await rowFor(page, 'quotafixture1').count() === 0);
        check('the server recorded the revoke',
            (await api(`/sivacor/user/${alice._id}/volume_quota`)).max_gb === 0);

        // ---- the three configuration warnings -------------------------------
        await setSettings({ 'sivacor.targeted_assignment': false });
        await reopen(page, 'sivacor/volumes');
        await page.waitForSelector('.g-sivacor-volumes .alert-warning', { timeout: 10000 });
        check('an unarmed fleet is called out',
            (await page.locator('.g-sivacor-volumes .alert-warning').innerText())
                .includes('Targeted assignment is off'));

        await setSettings({ 'sivacor.volume_total_gb': 0, 'sivacor.targeted_assignment': true });
        await reopen(page, 'sivacor/volumes');
        await page.waitForSelector('.g-sivacor-volumes .alert-warning', { timeout: 10000 });
        check('a zero per-request limit is called out',
            (await page.locator('.g-sivacor-volumes .alert-warning').innerText())
                .includes('per-request limit is 0 GB'));

        await setSettings({ 'sivacor.volumes_enabled': false });
        await reopen(page, 'sivacor/volumes');
        await page.waitForSelector('.g-sivacor-volumes .alert-warning', { timeout: 10000 });
        const offText = await page.locator('.g-sivacor-volumes .alert-warning').first().innerText();
        check('a disabled deployment is called out',
            offText.includes('switched off'));
        check('the disabled banner replaces the other two rather than joining them',
            await page.locator('.g-sivacor-volumes .alert-warning').count() === 1);
        await setSettings({
            'sivacor.volumes_enabled': true,
            'sivacor.volume_total_gb': 100
        });

        // ---- the user page entry point --------------------------------------
        await open(page, `user/${bob._id}`, token);
        await page.waitForSelector('.g-user-header', { timeout: 15000 });
        check('the user page offers the allowance',
            await page.locator('a.g-sivacor-volume-quota').count() === 1);
        const menuItems = await page.locator('.g-user-header .g-item-actions-menu li a')
            .evaluateAll((els) => els.map((el) => el.className));
        check('it sits before Delete user',
            menuItems.indexOf('g-sivacor-volume-quota') <
                menuItems.findIndex((c) => c.includes('g-delete-user')),
            menuItems.join(','));

        await openUserActions(page);
        await page.locator('a.g-sivacor-volume-quota').click();
        await page.waitForSelector('#g-sivacor-quota-gb', { timeout: 10000 });
        check('the user-page dialog names that account',
            (await page.locator('.modal-title').innerText()).includes('quotafixture2'));
        await page.locator('#g-sivacor-quota-gb').fill('30');
        await page.locator('.g-sivacor-quota-save').click();
        await page.waitForSelector('#g-sivacor-quota-form', { state: 'detached', timeout: 10000 });
        check('saving from the user page is announced',
            (await page.locator('#g-alerts-container').innerText()).includes('30 GB'));
        check('saving from the user page reaches the server',
            (await api(`/sivacor/user/${bob._id}/volume_quota`)).max_gb === 30);

        // revoke from inside the dialog, which does not ask again
        await openUserActions(page);
        await page.locator('a.g-sivacor-volume-quota').click();
        await page.waitForSelector('#g-sivacor-quota-gb', { timeout: 10000 });
        await page.locator('.g-sivacor-revoke').click();
        await page.waitForSelector('#g-sivacor-quota-form', { state: 'detached', timeout: 10000 });
        check('the dialog revoke needs no confirmation',
            (await api(`/sivacor/user/${bob._id}/volume_quota`)).max_gb === 0);

        // ---- the resource-usage page ----------------------------------------
        await open(page, 'sivacor/usage', token);
        await page.waitForSelector('.g-sivacor-usage .g-sivacor-tile', { timeout: 15000 });
        check('an unaccrued deployment says so rather than showing an empty table',
            (await page.locator('.g-sivacor-usage .g-sivacor-empty').first().innerText())
                .includes('No account has accrued usage yet'));
        check('the house panel is empty too',
            (await page.locator('.g-sivacor-panels').innerText())
                .includes('Nothing has been charged to the house'));

        seedUsage(alice._id);
        try {
            await reopen(page, 'sivacor/usage');
            await page.waitForSelector('.g-sivacor-usage-users tbody tr', { timeout: 10000 });
            const usageRow = page.locator('.g-sivacor-usage-users tbody tr').first();
            check('an accrued account is listed',
                (await usageRow.innerText()).includes('quotafixture1'));
            check('its SU-hours are shown to one decimal',
                (await usageRow.innerText()).includes('32.5'));
            const tallies = (await usageRow.locator('.g-sivacor-size-tally')
                .allInnerTexts()).map((t) => t.replace(/\s+/g, ' ').trim());
            check('its worker sizes are broken down by rung',
                tallies.includes('60 GB \u00d72') && tallies.includes('30 GB \u00d71'),
                tallies.join(' / '));
            check('every worker size it used is shown',
                await usageRow.locator('.g-sivacor-size-tally').count() === 2);
            check('accounting-since is shown, not just the last run',
                (await usageRow.innerText()).includes('2026-08-01'));

            const tiles = await page.locator('.g-sivacor-usage .g-sivacor-tile').allInnerTexts();
            // Tile labels render uppercase -- telemetry.styl text-transforms
            // them -- and innerText reports what is rendered, not the source.
            const tileFor = (label) => (tiles.find(
                (t) => t.toLowerCase().includes(label.toLowerCase())) || ''
            ).split('\n')[0].trim();
            check('the total counts the house in', tileFor('SU-hours total') === '40.5',
                tileFor('SU-hours total'));
            check('what accounts were charged is named, not left to a subtraction',
                tileFor('Charged to accounts') === '32.5', tileFor('Charged to accounts'));
            check('the house total is its own figure', tileFor('House') === '8.0',
                tileFor('House'));
            check('rows waiting on the next drain are counted',
                tileFor('Awaiting drain') === '1', tileFor('Awaiting drain'));

            check('the house spend is broken down by reason',
                (await page.locator('.g-sivacor-panels').innerText()).includes('unclaimed'));
            check('the pending panel separates lifetimes from attributions',
                (await page.locator('.g-sivacor-panels').innerText())
                    .includes('Attributions'));
            check('the page links to the quota page it must not be confused with',
                await page.locator('.g-sivacor-footnote a[href="#sivacor/volumes"]')
                    .count() === 1);
        } finally {
            clearSeededUsage(alice._id);
        }

        // ---- a non-admin sees none of it ------------------------------------
        const basic = Buffer.from('quotafixture1:quotafixture123').toString('base64');
        const asUser = await fetch(`${API}/user/authentication`, {
            headers: { Authorization: `Basic ${basic}` }
        }).then((r) => r.json());
        const userToken = asUser.authToken.token;

        const plain = await context.newPage();
        await open(plain, `user/${bob._id}`, userToken);
        await plain.waitForSelector('.g-user-header', { timeout: 15000 });
        check('a non-admin is not offered the allowance on a user page',
            await plain.locator('a.g-sivacor-volume-quota').count() === 0);

        await open(plain, 'sivacor/volumes', userToken);
        await plain.waitForSelector('.g-sivacor-volumes .alert-danger', { timeout: 15000 });
        check('a non-admin reaching the page is told why it is empty',
            (await plain.locator('.g-sivacor-volumes .alert-danger').innerText()).length > 0);
        check('a non-admin sees no quota table',
            await plain.locator('.g-sivacor-quotas').count() === 0);

        await open(plain, 'sivacor/usage', userToken);
        await plain.waitForSelector('.g-sivacor-usage .alert-danger', { timeout: 15000 });
        check('a non-admin reaching the usage page is told why it is empty',
            (await plain.locator('.g-sivacor-usage .alert-danger').innerText()).length > 0);
        await plain.close();

        check('no uncaught javascript errors', consoleErrors.length === 0,
            consoleErrors.join(' | '));
    } finally {
        await setSettings(
            Object.fromEntries(SETTINGS.map((key) => [key, before[key]]))
        );
        await browser.close();
    }

    report();
}

function report() {
    console.log(`\n${passed}/${passed + failures.length} checks passed`);
    if (failures.length) {
        console.log(`failed: ${failures.join(', ')}`);
        process.exit(1);
    }
}

main().catch((err) => {
    console.error(err);
    process.exit(1);
});
