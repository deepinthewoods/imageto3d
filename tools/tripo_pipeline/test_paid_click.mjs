/** paid_click.mjs with a stub control (recheck-before-cutover finding 6): the gate is asked only AFTER the actionability wait,
 * the dispatch is bounded, and an uncertain delivery is reported as such. No browser.
 *
 *     node --test tools/tripo_pipeline/test_paid_click.mjs
 */
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { gatedPaidClick, DISPATCH_TIMEOUT_MS } from './paid_click.mjs';

const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

/** A control that becomes actionable `readyAfter` ms after creation, like a button under a closing overlay. */
function stubControl({readyAfter = 0, enabled = true, dispatch = null} = {}) {
  const born = Date.now();
  const log = [];
  const control = {
    log,
    enabled,
    async waitFor({state, timeout}) { log.push(['waitFor', state, timeout]); },
    async isEnabled() { log.push(['isEnabled']); return control.enabled; },
    async click({trial = false, timeout, noWaitAfter = false} = {}) {
      if (trial) {
        log.push(['trial', timeout]);
        const left = readyAfter - (Date.now() - born);
        if (left > timeout) { await sleep(timeout); throw new Error(`Timeout ${timeout}ms exceeded.\nwaiting for element to receive pointer events`); }
        if (left > 0) await sleep(left);          // Playwright's auto-wait, finished here
        return;
      }
      log.push(['click', timeout, noWaitAfter]);
      if (dispatch) await dispatch(timeout);
    },
  };
  return control;
}

const names = control => control.log.map(entry => entry[0]);

test('the gate is asked after the delayed control became actionable, then the click is dispatched with a short timeout', async () => {
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
  assert.deepEqual(names(control), ['waitFor', 'isEnabled', 'trial', 'isEnabled', 'click']);
  assert.deepEqual(order.map(entry => entry[0]), ['gate', 'intent']);
  assert.deepEqual(control.log.at(-1), ['click', DISPATCH_TIMEOUT_MS, true]);
  assert.ok(DISPATCH_TIMEOUT_MS <= 5000);
});

test("the reviewer's case: a hold that appears while the control is covered stops the click and writes no intent", async () => {
  const control = stubControl({readyAfter: 300});
  let held = false, intents = 0;
  setTimeout(() => { held = true; }, 100);           // the hold takes effect during Playwright's wait
  const result = await gatedPaidClick({control, gateClosed: () => (held ? 'conversions are held (CONVERT_HOLD: restart)' : null),
                                       writeIntent: () => { intents++; }});
  assert.deepEqual(result, {outcome: 'gate_closed', gate: 'conversions are held (CONVERT_HOLD: restart)'});
  assert.equal(intents, 0);
  assert.ok(!names(control).includes('click'));
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
  assert.equal(second.outcome, 'not_actionable');
  assert.equal(intents, 0);
  assert.ok(!names(control).includes('click'));
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
  assert.deepEqual(order, ['gate', 'intent', 'click']);
});
