/** Gate-checked dispatch of a credit-capable click (recheck-before-cutover finding 6, final-verify row 6; DESIGN_PHASE4 I.8). Only
 * node built-ins are imported, so studio.mjs and the node test (test_paid_click.mjs, a stub control) share it.
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
 * Last-check row 6: steps 2-4 run under the paid-gate TRANSITION LOCK when the caller passes `takeMutex` (studio.mjs does for
 * a gated run). Every writer of CONVERT_HOLD / CONVERT_DRAIN / PAUSE / supervisor_pause.json / supervisor_mode.json takes it too,
 * so a hold cannot be written between the final gate check and the dispatch: it is written before the check (gate_closed, nothing
 * clicked) or after the click was delivered. A lock that stays busy counts as a closed gate (nothing clicked).
 *
 * Post-cut-over review, blocker 2: the lock is the OS byte lock of tools/paid_gate.py (transition_lock on
 * `<root>/.codex_jobs/_batch/gate.lock`), which never expires and which the kernel releases when its owner exits. node cannot take
 * such a lock itself, so takeGateMutex keeps a small python helper alive for the critical section: `python tools/paid_gate.py hold`
 * takes the lock, prints "held" and holds it until its stdin closes. release() closes that pipe; if node dies the pipe closes by
 * itself. However long the gate check, the intent write or the dispatch stall, nobody else gets the lock meanwhile. (The former
 * `gate_mutex/` directory was reclaimed after 30 s, which let a second holder in while a stalled first one was still inside.)
 *
 * Final-live review, earlier blocker 2 ("simulated Node helper loss during the intent write still produced a click"): the lock
 * belongs to the helper, not to node, and `owned()` used to be looked at only BEFORE the intent write. Now the LAST thing before
 * the dispatch, after the intent write, is `mutex.verify()`: node writes `ping <nonce>` to the helper's stdin and the helper
 * answers `held <nonce>` only after it re-checked its own lock handle (paid_gate.transition_lock.verify: the handle is open and
 * the lock byte is still locked). No answer within PING_TIMEOUT_MS, `lost`, a helper that is gone, or a pipe error = the lock is
 * not ours: NOTHING is clicked, the intent written a moment earlier is withdrawn (`withdrawIntent(reason)`, the caller's
 * undo of writeIntent) and the outcome is gate_closed {intent_withdrawn}. After the answer only a synchronous `owned()` look
 * stands between it and the dispatch call (no await, no I/O of ours).
 * RESIDUAL (documented, accepted): node cannot hold the OS lock itself, so the helper can still die in the window between its
 * `held` answer and the moment the browser receives the forced click (the pipe read plus one Playwright round trip: of the order
 * of a millisecond or a few). A gate transition would have to be WRITTEN in exactly that window, by a writer that got the freed
 * lock, to be followed by a click; the gate is asked again after the dispatch, which then reports `uncertain` (needs review),
 * never `clicked`. Hyper3D (python, paid_gate.checked_dispatch) owns the OS lock in the clicking process and has no such window.
 *
 * Outcomes ({outcome, ...}):
 *   not_actionable {error}  nothing was clicked, no intent written: the caller may wait and try again, or give up
 *   gate_closed {gate}      nothing was clicked, no intent written: the caller exits with the gate-closed code
 *   gate_closed {gate, intent_withdrawn}
 *                           nothing was clicked; the lock was lost AFTER the intent was written and before the dispatch.
 *                           intent_withdrawn true: withdrawIntent put the record back; false: there was no withdrawIntent or it
 *                           failed (`withdraw_error`), so the caller's record still shows an intent for a click that was not made
 *   clicked                 the click was dispatched at once and the gate was still open when it had been
 *   uncertain {error}       the dispatch failed or timed out AFTER the intent was written, or the gate closed while it was under
 *                           way: the click may have happened. The caller must keep the intent protected (needs review) and
 *                           never click again by itself.
 */
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

export const DISPATCH_TIMEOUT_MS = 2500;
export const PING_TIMEOUT_MS = 3000;            // verify(): how long the lock helper gets to answer a ping (it only re-checks a handle)
const GATE_TOOL = fileURLToPath(new URL('../paid_gate.py', import.meta.url));
const HELPER_START_MS = 15000;                  // on top of `wait`: python start-up of the lock helper
const HELPER_EXIT_MS = 5000;                    // release(): how long the helper gets to exit before it is killed
const firstLine = err => String(err?.message ?? err).split('\n')[0].slice(0, 300);
/** `promise`, or `ms` milliseconds, whichever ends first (the timer is cleared, so it never keeps the process alive). */
const within = (promise, ms) => new Promise(resolve => {
  const timer = setTimeout(resolve, ms);
  promise.then(() => { clearTimeout(timer); resolve(); });
});

/** Take the paid-gate transition lock of `root` (tools/paid_gate.py transition_lock, an OS byte lock) through a helper process
 * that holds it for us: `python paid_gate.py hold --root <root>`. Resolves to {owned(), release(), pid}, or to null when the lock
 * stayed busy for `wait` ms or the helper could not be started (the caller treats both as a closed gate).
 *   owned()    the helper is still alive, so the lock is still ours (an OS lock is never taken from a live holder);
 *   verify()   asks the helper itself (`ping <nonce>` on its stdin): resolves true only when it answered `held <nonce>` in
 *              time, i.e. it is alive, it re-checked its own lock handle and still holds the lock; false otherwise (gone,
 *              `lost`, no answer within `timeout`, pipe error). The last look before a dispatch (earlier blocker 2);
 *   release()  closes the helper's stdin and waits for it to exit (killed after HELPER_EXIT_MS; the kernel then releases). */
export function takeGateMutex(root, {wait = 2000, what = 'paid click', python = process.env.PYTHON ?? 'python',
                                     startTimeout = wait + HELPER_START_MS} = {}) {
  return new Promise(resolve => {
    let child;
    try {
      child = spawn(python, ['-X', 'utf8', GATE_TOOL, 'hold', '--root', root, '--wait', String(wait / 1000), '--what', what],
                    {stdio: ['pipe', 'pipe', 'ignore'], windowsHide: true});
    } catch { resolve(null); return; }
    let out = '', settled = false, gone = false, pings = 0;
    const exited = new Promise(done => {
      child.once('exit', () => { gone = true; done(); });
      child.once('error', () => { gone = true; done(); });   // could not be spawned: there is no exit event
    });
    child.stdin.on('error', () => {});                        // the helper went away first: nothing to close
    const lock = {
      pid: child.pid,
      owned: () => !gone && child.exitCode === null && child.signalCode === null,
      verify: (timeout = PING_TIMEOUT_MS) => new Promise(answer => {
        if (!lock.owned()) { answer(false); return; }
        if (!(timeout > 0)) { answer(false); return; }   // no time to answer = not verified (deterministic; a fast helper must not win a 0 ms race)
        const nonce = `${process.pid}-${++pings}`;
        let seen = '', done = false;
        const finish = ok => {
          if (done) return;
          done = true;
          clearTimeout(timer);
          child.stdout.off('data', onData);
          answer(Boolean(ok) && lock.owned());
        };
        const onData = chunk => {
          seen += chunk;
          const lines = seen.split(/\r?\n/);
          if (lines.includes(`held ${nonce}`)) finish(true);
          else if (lines.includes(`lost ${nonce}`)) finish(false);
        };
        const timer = setTimeout(() => finish(false), timeout);
        child.stdout.on('data', onData);
        exited.then(() => finish(false));
        try { child.stdin.write(`ping ${nonce}\n`, err => { if (err) finish(false); }); } catch { finish(false); }
      }),
      release: async () => {
        if (gone) return;
        try { child.stdin.end(); } catch {}
        await within(exited, HELPER_EXIT_MS);
        if (!gone) { try { child.kill(); } catch {} await within(exited, HELPER_EXIT_MS); }
      },
    };
    const finish = value => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (value) resolve(value); else lock.release().then(() => resolve(null));
    };
    const timer = setTimeout(() => finish(null), startTimeout);
    child.stdout.on('data', chunk => { out += chunk; if (/^held\r?$/m.test(out)) finish(lock); });
    exited.then(() => finish(null));                          // "busy" (exit 3), or it failed
  });
}

export async function gatedPaidClick({control, gateClosed, writeIntent, withdrawIntent = null, stillReady = null,
                                      actionableTimeout = 15000, dispatchTimeout = DISPATCH_TIMEOUT_MS, takeMutex = null}) {
  let handle = null, mutex = null;
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
    // Last-check row 6: the final gate check, the intent and the dispatch are ONE critical section against every gate transition.
    if (takeMutex) {
      try { mutex = await takeMutex(); } catch (err) {
        return {outcome: 'gate_closed', gate: `the paid-gate transition lock could not be taken (${firstLine(err)})`};
      }
      if (!mutex) return {outcome: 'gate_closed', gate: 'a paid-gate transition is in progress (the transition lock stayed busy)'};
    }
    const gate = await gateClosed();
    if (gate) return {outcome: 'gate_closed', gate: String(gate)};
    // From here nothing waits. The gate check is a child process: one instant look (on the pinned element, no lookup) that the
    // control did not go away or get disabled meanwhile.
    let enabled = false;
    try { enabled = await handle.isEnabled(); } catch (err) { return {outcome: 'not_actionable', error: firstLine(err)}; }
    if (!enabled) return {outcome: 'not_actionable', error: 'the control became disabled during the gate check'};
    // the lock never expires; it is lost only when its helper process is gone (killed): then nothing is serialized, so no click
    if (mutex && !mutex.owned()) return {outcome: 'gate_closed', gate: 'the paid-gate transition lock was lost (its holder process is gone)'};
    await writeIntent();
    // Earlier blocker 2: the intent write took time and the lock is the HELPER's. The last look before the dispatch asks the
    // helper itself (it re-checks its lock handle); anything but "held" = not ours = no click, and the intent is withdrawn.
    if (mutex) {
      let held = false, why = 'its holder process is gone or did not confirm that it still holds it';
      try { held = mutex.verify ? await mutex.verify() : mutex.owned(); } catch (err) { why = firstLine(err); }
      if (!held || !mutex.owned()) {       // owned(): synchronous, nothing of ours runs between this look and the dispatch call
        const gate = `the paid-gate transition lock was lost after the intent was written (${why}); nothing was clicked`;
        let withdrawn = false, withdrawError = null;
        if (withdrawIntent) {
          try { await withdrawIntent(gate); withdrawn = true; } catch (err) { withdrawError = firstLine(err); }
        }
        return {outcome: 'gate_closed', gate, intent_withdrawn: withdrawn, ...(withdrawError ? {withdraw_error: withdrawError} : {})};
      }
    }
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
    if (mutex) { try { await mutex.release(); } catch {} }
    if (handle?.dispose) await handle.dispose().catch(() => {});
  }
}
