/** Gate-checked dispatch of a credit-capable click (recheck-before-cutover finding 6, final-verify row 6; DESIGN_PHASE4 I.8). No
 * imports, so studio.mjs and the node test (test_paid_click.mjs, a stub control) share it.
 *
 * Playwright's click() waits for the control to be actionable (visible, stable, enabled, not covered) for up to its timeout
 * and only then dispatches. A gate checked BEFORE such a click can therefore be seconds or minutes old when the click lands. So:
 *
 *   1. finish the actionability wait first: waitFor visible, isEnabled, and a TRIAL click (every check, no click);
 *      `stillReady()` (optional) is the caller's own last look (e.g. the upload slots); then pin the element
 *      (`control.elementHandle()`), so nothing after the gate has to look it up (a locator lookup auto-waits);
 *   2. THEN ask the paid gate (`gateClosed()` -> null = open, else the reason);
 *   3. with NO auto-wait in between: one instant look that the pinned element is still enabled, write the intent
 *      (`writeIntent()`), and dispatch on the pinned element with `{force: true, noWaitAfter: true}`: a forced click does not
 *      wait for actionability (step 1 did), it is delivered at once or it fails. `dispatchTimeout` only bounds that one call;
 *   4. ask the gate once more after the dispatch returned: when it closed meanwhile (a hold that took effect while the dispatch
 *      was under way), the click is NOT reported as `clicked` but as `uncertain`.
 *
 * Outcomes ({outcome, ...}):
 *   not_actionable {error}  nothing was clicked, no intent written: the caller may wait and try again, or give up
 *   gate_closed {gate}      nothing was clicked, no intent written: the caller exits with the gate-closed code
 *   clicked                 the click was dispatched at once and the gate was still open when it had been
 *   uncertain {error}       the dispatch failed or timed out AFTER the intent was written, or the gate closed while it was under
 *                           way: the click may have happened. The caller must keep the intent protected (needs review) and
 *                           never click again by itself.
 */
export const DISPATCH_TIMEOUT_MS = 2500;
const firstLine = err => String(err?.message ?? err).split('\n')[0].slice(0, 300);

export async function gatedPaidClick({control, gateClosed, writeIntent, stillReady = null, actionableTimeout = 15000,
                                      dispatchTimeout = DISPATCH_TIMEOUT_MS}) {
  let handle = null;
  try {
    try {
      await control.waitFor({state: 'visible', timeout: actionableTimeout});
      if (!(await control.isEnabled())) throw new Error('the control is disabled');
      await control.click({trial: true, timeout: actionableTimeout});
      const why = stillReady ? await stillReady() : null;
      if (why) throw new Error(why);
      handle = await control.elementHandle({timeout: actionableTimeout});
      if (!handle) throw new Error('the control has no element');
    } catch (err) {
      return {outcome: 'not_actionable', error: firstLine(err)};
    }
    const gate = await gateClosed();
    if (gate) return {outcome: 'gate_closed', gate: String(gate)};
    // From here nothing waits. The gate check is a child process: one instant look (on the pinned element, no lookup) that the
    // control did not go away or get disabled meanwhile.
    let enabled = false;
    try { enabled = await handle.isEnabled(); } catch (err) { return {outcome: 'not_actionable', error: firstLine(err)}; }
    if (!enabled) return {outcome: 'not_actionable', error: 'the control became disabled during the gate check'};
    await writeIntent();
    try {
      await handle.click({force: true, noWaitAfter: true, timeout: dispatchTimeout});
    } catch (err) {
      return {outcome: 'uncertain', error: firstLine(err)};
    }
    let after;
    try { after = await gateClosed(); } catch (err) { after = `the gate could not be asked after the dispatch (${firstLine(err)})`; }
    if (after) return {outcome: 'uncertain', error: `the paid gate closed while the click was dispatched: ${firstLine(after)}`};
    return {outcome: 'clicked'};
  } finally {
    if (handle?.dispose) await handle.dispose().catch(() => {});
  }
}
