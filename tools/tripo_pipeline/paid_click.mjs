/** Gate-checked dispatch of a credit-capable click (recheck-before-cutover finding 6; DESIGN_PHASE4 I.8). No imports, so
 * studio.mjs and the node test (test_paid_click.mjs, a stub control) share it.
 *
 * Playwright's click() waits for the control to be actionable (visible, stable, enabled, not covered) for up to its timeout
 * and only then dispatches. A gate checked BEFORE such a click can therefore be minutes old when the click lands. So:
 *
 *   1. finish the actionability wait first: waitFor visible, isEnabled, and a TRIAL click (every check, no click);
 *      `stillReady()` (optional) is the caller's own last look (e.g. the upload slots);
 *   2. THEN ask the paid gate (`gateClosed()` -> null = open, else the reason);
 *   3. write the intent (`writeIntent()`), and dispatch with a short bounded timeout (`dispatchTimeout`, default 2.5 s): the
 *      control was actionable a moment ago, so nothing waits here for long after the gate check.
 *
 * Outcomes ({outcome, ...}):
 *   not_actionable {error}  nothing was clicked, no intent written: the caller may wait and try again, or give up
 *   gate_closed {gate}      nothing was clicked, no intent written: the caller exits with the gate-closed code
 *   clicked                 the click was dispatched
 *   uncertain {error}       the dispatch failed or timed out AFTER the intent was written: the click may have happened.
 *                           The caller must keep the intent protected (needs review) and never click again by itself.
 */
export const DISPATCH_TIMEOUT_MS = 2500;
const firstLine = err => String(err?.message ?? err).split('\n')[0].slice(0, 300);

export async function gatedPaidClick({control, gateClosed, writeIntent, stillReady = null, actionableTimeout = 15000,
                                      dispatchTimeout = DISPATCH_TIMEOUT_MS}) {
  try {
    await control.waitFor({state: 'visible', timeout: actionableTimeout});
    if (!(await control.isEnabled())) throw new Error('the control is disabled');
    await control.click({trial: true, timeout: actionableTimeout});
    const why = stillReady ? await stillReady() : null;
    if (why) throw new Error(why);
  } catch (err) {
    return {outcome: 'not_actionable', error: firstLine(err)};
  }
  const gate = await gateClosed();
  if (gate) return {outcome: 'gate_closed', gate: String(gate)};
  // the gate check is a child process (and may run an extra command): one instant look that the control did not go away meanwhile
  let enabled = false;
  try { enabled = await control.isEnabled(); } catch (err) { return {outcome: 'not_actionable', error: firstLine(err)}; }
  if (!enabled) return {outcome: 'not_actionable', error: 'the control became disabled during the gate check'};
  await writeIntent();
  try {
    await control.click({timeout: dispatchTimeout, noWaitAfter: true});
  } catch (err) {
    return {outcome: 'uncertain', error: firstLine(err)};
  }
  return {outcome: 'clicked'};
}
