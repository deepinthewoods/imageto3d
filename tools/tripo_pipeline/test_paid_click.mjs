/** paid_click.mjs with a stub control (recheck-before-cutover finding 6; final-verify row 6): the gate is asked only AFTER the
 * actionability wait, the dispatch follows it on the pinned element WITHOUT any auto-wait (forced, bounded), a hold that takes
 * effect while the dispatch is under way never ends as `clicked`, and an uncertain delivery is reported as such. No browser.
 *
 *     node --test tools/tripo_pipeline/test_paid_click.mjs
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { gatedPaidClick, DISPATCH_TIMEOUT_MS } from './paid_click.mjs';

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
