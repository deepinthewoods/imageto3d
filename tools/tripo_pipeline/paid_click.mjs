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
 * Last-check row 6: steps 2-4 run under the paid-gate TRANSITION MUTEX when the caller passes `takeMutex` (studio.mjs does for
 * a gated run): the directory `<root>/.codex_jobs/_batch/gate_mutex` (tools/paid_gate.py transition_lock; takeGateMutex below is
 * the same protocol in node). Every writer of CONVERT_HOLD / CONVERT_DRAIN / PAUSE / supervisor_mode.json takes it too, so a hold
 * cannot be written between the final gate check and the dispatch: it is written before the check (gate_closed, nothing
 * clicked) or after the click was delivered. A mutex that stays busy counts as a closed gate (nothing clicked).
 *
 * Outcomes ({outcome, ...}):
 *   not_actionable {error}  nothing was clicked, no intent written: the caller may wait and try again, or give up
 *   gate_closed {gate}      nothing was clicked, no intent written: the caller exits with the gate-closed code
 *   clicked                 the click was dispatched at once and the gate was still open when it had been
 *   uncertain {error}       the dispatch failed or timed out AFTER the intent was written, or the gate closed while it was under
 *                           way: the click may have happened. The caller must keep the intent protected (needs review) and
 *                           never click again by itself.
 */
import fs from 'node:fs';
import path from 'node:path';
import { randomBytes } from 'node:crypto';

export const DISPATCH_TIMEOUT_MS = 2500;
export const MUTEX_DIR = 'gate_mutex';          // = tools/paid_gate.py MUTEX_DIR
export const MUTEX_STALE_MS = 30000;            // = tools/paid_gate.py MUTEX_STALE
export const OWNERLESS_STALE_MS = 2000;         // = tools/paid_gate.py OWNERLESS_STALE (no owner.json: abandoned after 2 s)
const firstLine = err => String(err?.message ?? err).split('\n')[0].slice(0, 300);
const nap = ms => Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms);

function pidAlive(pid) {
  try { process.kill(Number(pid), 0); return true; } catch (err) { return err.code === 'EPERM'; }
}

/** Rename a stale mutex aside (its owner process is gone, or it is older than `stale` ms); true when it is out of the way. */
function reclaimIfStale(dir, stale) {
  let owner = {};
  try { owner = JSON.parse(fs.readFileSync(path.join(dir, 'owner.json'), 'utf8')); } catch {}
  let age;
  try { age = Date.now() - fs.statSync(dir).mtimeMs; } catch (err) { return err.code === 'ENOENT'; }
  const gone = owner && typeof owner === 'object' && owner.pid != null && !pidAlive(owner.pid);
  const ownerless = !(owner && typeof owner === 'object' && owner.pid != null);
  if (!gone && age < (ownerless ? Math.min(stale, OWNERLESS_STALE_MS) : stale)) return false;
  try { fs.renameSync(dir, path.join(path.dirname(dir), `${MUTEX_DIR}.stale-${Date.now()}-${process.pid}`)); } catch { return false; }
  return true;
}

/** Take the paid-gate transition mutex of `root` (tools/paid_gate.py transition_lock: same directory, owner.json and stale rule).
 * Returns {owned(), release()}, or null when it stayed busy for `wait` ms (the caller treats that as a closed gate). Synchronous. */
export function takeGateMutex(root, {wait = 2000, what = 'paid click', stale = MUTEX_STALE_MS} = {}) {
  const dir = path.join(root, '.codex_jobs', '_batch', MUTEX_DIR);
  const ownerFile = path.join(dir, 'owner.json');
  const token = randomBytes(8).toString('hex');
  fs.mkdirSync(path.dirname(dir), {recursive: true});
  const deadline = Date.now() + wait;
  for (let spins = 0; ;) {
    try { fs.mkdirSync(dir); break; } catch (err) {
      if (!['EEXIST', 'EPERM', 'EACCES'].includes(err.code)) throw err;   // EPERM / EACCES: it is being renamed or removed right now
      if (reclaimIfStale(dir, stale) && spins++ < 3) continue;           // out of the way: try at once (bounded, then the timed path)
      if (Date.now() >= deadline) return null;
      nap(10);
    }
  }
  let wrote = false, held = true;
  try { fs.writeFileSync(ownerFile, JSON.stringify({pid: process.pid, time: Date.now() / 1000, what, token})); wrote = true; } catch {}
  const owned = () => {
    if (!held) return false;
    try { return JSON.parse(fs.readFileSync(ownerFile, 'utf8')).token === token; } catch { return !wrote && fs.existsSync(dir); }
  };
  const release = () => {
    if (!held) return;
    const mine = owned();
    held = false;
    if (!mine) return;                           // reclaimed as stale meanwhile: what is there now belongs to another holder
    // release = rename aside first (atomic; Windows may refuse a remove for a moment), then tidy; = paid_gate.transition_lock
    const aside = path.join(path.dirname(dir), `${MUTEX_DIR}.released-${token}`);
    let target = dir;
    for (let i = 0; i < 20; i++) {
      try { fs.renameSync(dir, aside); target = aside; break; }
      catch (err) { if (err.code === 'ENOENT') return; Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 10 * (i + 1)); }
    }
    for (let i = 0; i < 20; i++) {
      try { fs.rmSync(path.join(target, 'owner.json'), {force: true}); fs.rmdirSync(target); return; }
      catch (err) { if (err.code === 'ENOENT') return; Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 10 * (i + 1)); }
    }
  };
  return {owned, release};
}

export async function gatedPaidClick({control, gateClosed, writeIntent, stillReady = null, actionableTimeout = 15000,
                                      dispatchTimeout = DISPATCH_TIMEOUT_MS, takeMutex = null}) {
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
        return {outcome: 'gate_closed', gate: `the paid-gate transition mutex could not be taken (${firstLine(err)})`};
      }
      if (!mutex) return {outcome: 'gate_closed', gate: 'a paid-gate transition is in progress (the transition mutex stayed busy)'};
    }
    const gate = await gateClosed();
    if (gate) return {outcome: 'gate_closed', gate: String(gate)};
    // From here nothing waits. The gate check is a child process: one instant look (on the pinned element, no lookup) that the
    // control did not go away or get disabled meanwhile.
    let enabled = false;
    try { enabled = await handle.isEnabled(); } catch (err) { return {outcome: 'not_actionable', error: firstLine(err)}; }
    if (!enabled) return {outcome: 'not_actionable', error: 'the control became disabled during the gate check'};
    if (mutex && !mutex.owned()) return {outcome: 'gate_closed', gate: 'the paid-gate transition mutex was lost during the gate check'};
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
    if (mutex) { try { mutex.release(); } catch {} }
    if (handle?.dispose) await handle.dispose().catch(() => {});
  }
}
