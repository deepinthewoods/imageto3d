/** paid_click.mjs with a stub control (recheck-before-cutover finding 6; final-verify row 6): the gate is asked only AFTER the
 * actionability wait, the dispatch follows it on the pinned element WITHOUT any auto-wait (forced, bounded), a hold that takes
 * effect while the dispatch is under way never ends as `clicked`, and an uncertain delivery is reported as such. No browser.
 *
 *     node --test tools/tripo_pipeline/test_paid_click.mjs
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { spawn, spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { gatedPaidClick, takeGateMutex, DISPATCH_TIMEOUT_MS, MUTEX_DIR } from './paid_click.mjs';

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

/** A control that becomes actionable `readyAfter` ms after creation, like a button under a closing overlay. Like Playwright, a
 * click that is not forced AUTO-WAITS (`autoWait` ms, the reviewer's fake control) before it lands; a forced click on the pinned
 * element (elementHandle) lands at once (`dispatch`, optional, models a slow or failing delivery). `control.clicks` = when each
 * click landed. */
function stubControl({readyAfter = 0, enabled = true, dispatch = null, autoWait = 0} = {}) {
  const born = Date.now();
  const log = [];
  const land = async (name, timeout, force, noWaitAfter) => {
    log.push([name, timeout, noWaitAfter, force]);
    if (!force && autoWait) await sleep(autoWait);      // Playwright's actionability wait before the dispatch
    if (dispatch) await dispatch(timeout);
    control.clicks.push(Date.now());
  };
  const control = {
    log,
    enabled,
    clicks: [],
    disposed: 0,
    async waitFor({state, timeout}) { log.push(['waitFor', state, timeout]); },
    async isEnabled() { log.push(['isEnabled']); return control.enabled; },
    async click({trial = false, timeout, noWaitAfter = false, force = false} = {}) {
      if (trial) {
        log.push(['trial', timeout]);
        const left = readyAfter - (Date.now() - born);
        if (left > timeout) { await sleep(timeout); throw new Error(`Timeout ${timeout}ms exceeded.\nwaiting for element to receive pointer events`); }
        if (left > 0) await sleep(left);          // Playwright's auto-wait, finished here
        return;
      }
      await land('locatorClick', timeout, force, noWaitAfter);
    },
    async elementHandle({timeout} = {}) {
      log.push(['elementHandle', timeout]);
      return {
        async isEnabled() { log.push(['handleEnabled']); return control.enabled; },
        async click({timeout: t, noWaitAfter = false, force = false} = {}) { await land('click', t, force, noWaitAfter); },
        async dispose() { control.disposed++; },
      };
    },
  };
  return control;
}

const names = control => control.log.map(entry => entry[0]);

test('the gate is asked after the delayed control became actionable, then the click is dispatched at once (forced, short timeout)', async () => {
  const control = stubControl({readyAfter: 300});
  const order = [];
  const held = false;
  const started = Date.now();
  const result = await gatedPaidClick({
    control,
    gateClosed: () => { order.push(['gate', Date.now() - started]); return held ? 'held' : null; },
    writeIntent: () => order.push(['intent']),
  });
  assert.deepEqual(result, {outcome: 'clicked'});
  assert.ok(order[0][1] >= 250, `gate asked ${order[0][1]} ms in, before the control was actionable`);
  assert.deepEqual(names(control), ['waitFor', 'isEnabled', 'trial', 'elementHandle', 'handleEnabled', 'click']);
  assert.deepEqual(order.map(entry => entry[0]), ['gate', 'intent', 'gate']);          // asked again after the dispatch
  assert.deepEqual(control.log.at(-1), ['click', DISPATCH_TIMEOUT_MS, true, true]);     // noWaitAfter, force: no auto-wait
  assert.ok(DISPATCH_TIMEOUT_MS <= 5000);
  assert.equal(control.disposed, 1);
});

test("the reviewer's case: a hold that appears while the control is covered stops the click and writes no intent", async () => {
  const control = stubControl({readyAfter: 300});
  let held = false, intents = 0;
  setTimeout(() => { held = true; }, 100);           // the hold takes effect during Playwright's wait
  const result = await gatedPaidClick({control, gateClosed: () => (held ? 'conversions are held (CONVERT_HOLD: restart)' : null),
                                       writeIntent: () => { intents++; }});
  assert.deepEqual(result, {outcome: 'gate_closed', gate: 'conversions are held (CONVERT_HOLD: restart)'});
  assert.equal(intents, 0);
  assert.equal(control.clicks.length, 0);
});

test("the final-verify reviewer's case: a hold during what used to be the dispatch wait never ends as clicked after the hold", async () => {
  // The fake control auto-waits 400 ms in an ordinary click (the old dispatch waited up to 2.5 s); the hold takes effect 100 ms
  // after the gate was asked. The dispatch must not go through that wait: it lands at once, before the hold.
  const control = stubControl({autoWait: 400});
  let heldAt = null, intents = 0, asked = 0;
  const result = await gatedPaidClick({control, writeIntent: () => { intents++; },
    gateClosed: () => { if (!asked++) setTimeout(() => { heldAt = Date.now(); }, 100); return heldAt ? 'conversions are held (CONVERT_HOLD: restart)' : null; }});
  assert.deepEqual(result, {outcome: 'clicked'});
  assert.equal(heldAt, null, 'the hold took effect before gatedPaidClick returned: the dispatch waited');
  assert.equal(control.clicks.length, 1);
  assert.ok(!names(control).includes('locatorClick'), 'an auto-waiting locator click was used for the dispatch');
  assert.deepEqual(control.log.at(-1), ['click', DISPATCH_TIMEOUT_MS, true, true]);
  await sleep(150);
  assert.ok(heldAt !== null && control.clicks[0] < heldAt, 'the click landed after the hold');
  assert.equal(intents, 1);
});

test('a hold that takes effect while a slow dispatch is under way is needs-review (uncertain), never clicked', async () => {
  let held = false, intents = 0;
  const control = stubControl({dispatch: async () => { await sleep(150); }});             // delivery slow although forced
  const result = await gatedPaidClick({control, writeIntent: () => { intents++; setTimeout(() => { held = true; }, 40); },
                                       gateClosed: () => (held ? 'conversions are held (CONVERT_HOLD: restart)' : null)});
  assert.equal(result.outcome, 'uncertain');
  assert.match(result.error, /paid gate closed while the click was dispatched: conversions are held/);
  assert.equal(intents, 1);                                             // the protected intent stays
  assert.equal(control.clicks.length, 1);                               // never clicked again
  // a gate that cannot be asked after the dispatch is not `clicked` either
  let calls = 0;
  const broken = await gatedPaidClick({control: stubControl(), writeIntent: () => {},
                                       gateClosed: () => { if (calls++) throw new Error('spawn failed'); return null; }});
  assert.equal(broken.outcome, 'uncertain');
  assert.match(broken.error, /could not be asked after the dispatch \(spawn failed\)/);
});

test('a control that never becomes actionable is not clicked and the gate is not even asked', async () => {
  let asked = 0, intents = 0;
  const control = stubControl({readyAfter: 10000});
  const result = await gatedPaidClick({control, gateClosed: () => { asked++; return null; }, writeIntent: () => { intents++; },
                                       actionableTimeout: 100});
  assert.equal(result.outcome, 'not_actionable');
  assert.match(result.error, /Timeout 100ms exceeded/);
  assert.deepEqual([asked, intents], [0, 0]);
  const disabled = stubControl({enabled: false});
  assert.deepEqual(await gatedPaidClick({control: disabled, gateClosed: () => null, writeIntent: () => { intents++; }}),
                   {outcome: 'not_actionable', error: 'the control is disabled'});
  assert.equal(intents, 0);
});

test('the caller\'s own last look (stillReady) and a control disabled during the gate check stop before the intent', async () => {
  let intents = 0;
  const first = await gatedPaidClick({control: stubControl(), gateClosed: () => assert.fail('gate asked'),
                                      writeIntent: () => { intents++; }, stillReady: async () => 'Upload slot emptied: back.png'});
  assert.deepEqual(first, {outcome: 'not_actionable', error: 'Upload slot emptied: back.png'});
  const control = stubControl();
  const second = await gatedPaidClick({control, gateClosed: () => { control.enabled = false; return null; },
                                       writeIntent: () => { intents++; }});
  assert.deepEqual(second, {outcome: 'not_actionable', error: 'the control became disabled during the gate check'});
  assert.equal(intents, 0);
  assert.equal(control.clicks.length, 0);
  assert.equal(control.disposed, 1);
});

test('a dispatch that fails or times out after the intent is uncertain, never retried here', async () => {
  let intents = 0;
  const control = stubControl({dispatch: async timeout => { await sleep(20); throw new Error(`Timeout ${timeout}ms exceeded.\nlog`); }});
  const result = await gatedPaidClick({control, gateClosed: () => null, writeIntent: () => { intents++; }, dispatchTimeout: 50});
  assert.deepEqual(result, {outcome: 'uncertain', error: 'Timeout 50ms exceeded.'});
  assert.equal(intents, 1);                                             // the intent stays: the click may have happened
  assert.equal(control.log.filter(entry => entry[0] === 'click').length, 1);
});

test('an async gate and intent are awaited in order', async () => {
  const order = [];
  const control = stubControl({dispatch: async () => { order.push('click'); }});
  await gatedPaidClick({control, gateClosed: async () => { await sleep(10); order.push('gate'); return null; },
                        writeIntent: async () => { await sleep(10); order.push('intent'); }});
  assert.deepEqual(order, ['gate', 'intent', 'click', 'gate']);
});

// ------------------------------------------------------------------------------------------------ last-check row 6
// The transition mutex (tools/paid_gate.py transition_lock = paid_click.mjs takeGateMutex), on a temp root, against the real
// python side: the gate is `python tools/paid_gate.py check`, the hold writer is python's transition_lock.
const PY = process.env.PYTHON ?? 'python';
const TOOLS = fileURLToPath(new URL('..', import.meta.url));
const GATE_TOOL = path.join(TOOLS, 'paid_gate.py');

function tempRoot(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'paid_click_mutex_'));
  fs.mkdirSync(path.join(root, '.codex_jobs', '_batch'), {recursive: true});
  t.after(() => fs.rmSync(root, {recursive: true, force: true}));
  return root;
}
const batchOf = root => path.join(root, '.codex_jobs', '_batch');
const gateEnv = root => ({...process.env, IMAGETO3D_PAID_GATE: JSON.stringify({root, override_pause: false})});
const pythonGate = root => () => {
  const run = spawnSync(PY, ['-X', 'utf8', GATE_TOOL, 'check'], {encoding: 'utf8', env: gateEnv(root)});
  return run.status === 0 ? null : (run.stdout || '').trim().split('\n')[0] || `gate check exit ${run.status}`;
};
/** The app's hold writer: CONVERT_HOLD written under python's transition_lock (waiting `wait` s). Exit 0 = written, 7 = GateBusy. */
const HOLD_WRITER = `
import sys, json
sys.path.insert(0, sys.argv[1])
import paid_gate
from pathlib import Path
root, wait = sys.argv[2], float(sys.argv[3])
try:
    with paid_gate.transition_lock(root, wait=wait, what='hold writer'):
        (Path(root) / '.codex_jobs' / '_batch' / 'CONVERT_HOLD').write_text(json.dumps({'reason': 'restart'}))
except paid_gate.GateBusy:
    raise SystemExit(7)
`;
const writeHold = (root, wait) => spawnSync(PY, ['-X', 'utf8', '-c', HOLD_WRITER, TOOLS, root, String(wait)], {encoding: 'utf8'});

test("the last-check reviewer's case: a hold written during the intent write cannot be followed by a click", async t => {
  const root = tempRoot(t);
  const hold = path.join(batchOf(root), 'CONVERT_HOLD');
  const control = stubControl();
  let duringIntent = null, holdExistedAtClick = null;
  const realLand = control.elementHandle;
  control.elementHandle = async opts => {
    const handle = await realLand(opts);
    const click = handle.click;
    handle.click = async o => { holdExistedAtClick = fs.existsSync(hold); return click(o); };
    return handle;
  };
  const result = await gatedPaidClick({
    control, gateClosed: pythonGate(root), takeMutex: () => takeGateMutex(root, {what: 'test click'}),
    // the app tries to hold conversions exactly while the intent is written: its writer must wait for the mutex
    writeIntent: () => { duringIntent = writeHold(root, 0.3); },
  });
  assert.equal(duringIntent.status, 7, `the hold writer got through during the critical section: ${duringIntent.stderr}`);
  assert.equal(holdExistedAtClick, false, 'a hold existed when the click was dispatched');
  assert.deepEqual(result, {outcome: 'clicked'});               // no hold could be written before the click: it is a clean click
  assert.equal(control.clicks.length, 1);
  assert.ok(!fs.existsSync(path.join(batchOf(root), MUTEX_DIR)), 'the mutex was not released');
  // afterwards the writer gets the mutex, and the next click sees the hold under it: nothing clicked, no intent
  assert.equal(writeHold(root, 2).status, 0);
  let intents = 0;
  const second = stubControl();
  const closed = await gatedPaidClick({control: second, gateClosed: pythonGate(root), writeIntent: () => { intents++; },
                                       takeMutex: () => takeGateMutex(root)});
  assert.deepEqual(closed, {outcome: 'gate_closed', gate: 'conversions are held (CONVERT_HOLD: restart)'});
  assert.deepEqual([intents, second.clicks.length], [0, 0]);
  assert.ok(!fs.existsSync(path.join(batchOf(root), MUTEX_DIR)));
});

test('a transition in progress (python holds the mutex) is a closed gate: the gate is not asked, nothing is clicked', async t => {
  const root = tempRoot(t);
  const holder = spawn(PY, ['-X', 'utf8', '-c', `
import sys, time
sys.path.insert(0, sys.argv[1])
import paid_gate
with paid_gate.transition_lock(sys.argv[2], what='slow writer'):
    print('held', flush=True)
    sys.stdin.read()
`, TOOLS, root], {stdio: ['pipe', 'pipe', 'inherit']});
  t.after(() => holder.kill());
  await new Promise(resolve => holder.stdout.once('data', resolve));
  const owner = JSON.parse(fs.readFileSync(path.join(batchOf(root), MUTEX_DIR, 'owner.json'), 'utf8'));
  assert.equal(owner.pid, holder.pid);
  let asked = 0, intents = 0;
  const control = stubControl();
  const result = await gatedPaidClick({control, gateClosed: () => { asked++; return null; }, writeIntent: () => { intents++; },
                                       takeMutex: () => takeGateMutex(root, {wait: 200})});
  assert.equal(result.outcome, 'gate_closed');
  assert.match(result.gate, /transition is in progress/);
  assert.deepEqual([asked, intents, control.clicks.length], [0, 0, 0]);
  holder.stdin.end();                                           // the writer finishes: python releases, node can take it
  await new Promise(resolve => holder.once('exit', resolve));
  const mutex = takeGateMutex(root, {wait: 2000});
  assert.ok(mutex?.owned());
  assert.equal(writeHold(root, 0.2).status, 7);                 // and python sees node's mutex as busy
  mutex.release();
  assert.equal(writeHold(root, 2).status, 0);
});

test('the node mutex reclaims a dead owner, and a holder that lost it neither clicks nor removes the new one', async t => {
  const root = tempRoot(t);
  const dir = path.join(batchOf(root), MUTEX_DIR);
  const dead = spawnSync(PY, ['-c', 'import os; print(os.getpid())'], {encoding: 'utf8'});
  fs.mkdirSync(dir);
  fs.writeFileSync(path.join(dir, 'owner.json'), JSON.stringify({pid: Number(dead.stdout.trim()), time: 0, what: 'crashed'}));
  const first = takeGateMutex(root, {wait: 500});
  assert.ok(first?.owned(), 'the mutex of a dead owner was not reclaimed');
  assert.equal(fs.readdirSync(batchOf(root)).filter(name => name.startsWith(`${MUTEX_DIR}.stale-`)).length, 1);   // renamed aside
  assert.equal(takeGateMutex(root, {wait: 100}), null);         // live and fresh: busy
  // the mutex is lost during a (too slow) gate check: another holder owns it now
  let intents = 0;
  const control = stubControl();
  const result = await gatedPaidClick({control, writeIntent: () => { intents++; }, takeMutex: () => first,
    gateClosed: () => { fs.writeFileSync(path.join(dir, 'owner.json'), JSON.stringify({pid: process.pid, token: 'someone else'})); return null; }});
  assert.deepEqual(result, {outcome: 'gate_closed', gate: 'the paid-gate transition mutex was lost during the gate check'});
  assert.deepEqual([intents, control.clicks.length], [0, 0]);
  assert.ok(fs.existsSync(path.join(dir, 'owner.json')), "the other holder's mutex was removed");
});
