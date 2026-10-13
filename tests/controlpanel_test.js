const fs = require('fs'), vm = require('vm'), assert = require('assert');
const src = fs.readFileSync(require('path').join(__dirname, '..', 'apps_script', 'ControlPanel.gs'), 'utf8');
function makeEnv(cells, props, fetchImpl) {
  const written = {}, calls = [], triggers = [], deleted = [];
  const sheet = { getName: () => 'Control',
    getRange: (a1) => ({ getValue: () => cells[a1], setValue: (v) => { cells[a1] = v; },
      setValues: (vals) => { vals.forEach((row, i) => { cells['B' + (8 + i)] = row[0]; }); }, getValues: () => [[cells.B8], [cells.B9], [cells.B10], [cells.B11]] }) };
  const env = {
    SpreadsheetApp: { getActive: () => ({ getSheetByName: () => sheet, toast: () => {} }), getUi: () => ({ createMenu: () => ({ addItem() { return this; }, addToUi() {} }), alert: () => {}, prompt: () => ({}), Button: { OK: 1 }, ButtonSet: {} }) },
    PropertiesService: { getScriptProperties: () => ({ getProperties: () => props, setProperties: (p) => Object.assign(props, p) }) },
    UrlFetchApp: { fetch: (url, opts) => { calls.push({ url, opts }); return fetchImpl(url, opts); } },
    Utilities: { formatDate: (d, tz, fmt) => { const p = (n) => String(n).padStart(2, '0');
      return fmt.replace('yyyy', d.getUTCFullYear()).replace('MM', p(d.getUTCMonth() + 1)).replace('dd', p(d.getUTCDate()))
        .replace('HH', p(d.getUTCHours())).replace('mm', p(d.getUTCMinutes())).replace('ss', p(d.getUTCSeconds())).replace(/'/g, ''); } },
    ScriptApp: { getProjectTriggers: () => [], deleteTrigger: (t) => deleted.push(t), newTrigger: () => ({ forSpreadsheet: () => ({ onEdit: () => ({ create: () => triggers.push('edit') }) }) }) },
    JSON, Date, String, Object, Number, console
  };
  vm.createContext(env); vm.runInContext(src + '\nthis.__fns = {startRun_, onControlEdit, runScraper, onOpen, parseStamp_};', env);
  return { env, calls, cells };
}
const ok = () => ({ getResponseCode: () => 204, getContentText: () => '' });
const base = () => ({ B3: 'Gyms and fitness studios', B4: 'Toronto ON', B5: 75, B6: true, B8: 'Idle', B9: '', B10: '', B11: '' });
const props = () => ({ GITHUB_TOKEN: 'tok123', GITHUB_REPO: 'me/pos-leads', GITHUB_REF: 'main' });

// 1. happy path
let t = makeEnv(base(), props(), ok); t.env.__fns.runScraper();
assert.strictEqual(t.calls.length, 1);
assert.strictEqual(t.calls[0].url, 'https://api.github.com/repos/me/pos-leads/actions/workflows/leads.yml/dispatches');
const body = JSON.parse(t.calls[0].opts.payload);
assert.deepStrictEqual([body.ref, body.inputs.category, body.inputs.city, body.inputs.min_score], ['main', 'Gyms and fitness studios', 'Toronto ON', '75']);
assert.ok(/^sheet-\d{8}-\d{6}$/.test(body.inputs.run_id));
assert.strictEqual(t.calls[0].opts.headers.Authorization, 'Bearer tok123');
assert.strictEqual(t.cells.B8, 'Queued'); console.log('1 ok: dispatch payload and Queued status');
// 2. GitHub errors become a readable Failed status
for (const [code, word] of [[401, 'Token'], [404, 'not found'], [422, 'not ready']]) {
  t = makeEnv(base(), props(), () => ({ getResponseCode: () => code, getContentText: () => '{"message":"x"}' })); t.env.__fns.runScraper();
  assert.ok(t.cells.B8.startsWith('Failed: GitHub answered ' + code)); assert.ok(t.cells.B9.includes(word), t.cells.B9);
}
console.log('2 ok: 401, 404, 422 give clear Failed messages');
// 3. network exception
t = makeEnv(base(), props(), () => { throw new Error('boom'); }); t.env.__fns.runScraper();
assert.ok(t.cells.B8.startsWith('Failed')); console.log('3 ok: exception is caught');
// 4. setup missing
t = makeEnv(base(), {}, ok); t.env.__fns.runScraper();
assert.strictEqual(t.calls.length, 0); assert.ok(t.cells.B8.includes('setup not done')); console.log('4 ok: no setup, no call');
// 5. double click guard, and a stale run does not lock you out
const now = new Date().toISOString().slice(0, 16).replace('T', ' ') + ' UTC';
let c = base(); c.B8 = 'Running'; c.B10 = now; c.B11 = 'sheet-old';
t = makeEnv(c, props(), ok); t.env.__fns.runScraper(); assert.strictEqual(t.calls.length, 0); assert.strictEqual(t.cells.B8, 'Running');
c = base(); c.B8 = 'Running'; c.B10 = '2020-01-01 00:00 UTC';
t = makeEnv(c, props(), ok); t.env.__fns.runScraper(); assert.strictEqual(t.calls.length, 1); console.log('5 ok: double-click guard and stale lock');
// 6. empty cells fall back to ALL / 70
c = base(); c.B3 = ''; c.B4 = ''; c.B5 = '';
t = makeEnv(c, props(), ok); t.env.__fns.runScraper();
const b6 = JSON.parse(t.calls[0].opts.payload).inputs; assert.deepStrictEqual([b6.category, b6.city, b6.min_score], ['ALL', 'ALL', '70']); console.log('6 ok: defaults');
// 7. checkbox trigger
function edit(a1, value) { let cells = base(); const e = { value, range: { getSheet: () => ({ getName: () => 'Control' }), getA1Notation: () => a1, getValue: () => cells.B6, setValue: (v) => { cells.B6 = v; } } };
  const t = makeEnv(cells, props(), ok); t.env.__fns.onControlEdit(e); return { t, cells }; }
let r = edit('B6', 'TRUE'); assert.strictEqual(r.t.calls.length, 1); assert.strictEqual(r.t.cells.B6, false);
r = edit('B3', 'TRUE'); assert.strictEqual(r.t.calls.length, 0);
r = edit('B6', 'FALSE'); r.t.cells.B6 = false; console.log('7 ok: only ticking B6 starts a run and the box resets');
console.log('ALL APPS SCRIPT TESTS PASSED');
