// The "Run everything now" estimate of the Queue tab's mode section (GET /api/v2/queue `run_all_estimate`, tools/preview/v2/run_estimate.py).
// Pure text helpers: no DOM. `estimateLine` is the one-line summary, `estimateConfirmLines` the extra lines of the Run now confirmation.
import { ago } from './util.js';

const num = value => `${+Number(value).toFixed(2)}`;
const plural = (n, one, many = `${one}s`) => `${n} ${n === 1 ? one : many}`;

const isPixal = e => e?.tripo_mode === 'pixal3d' || e?.tripo_mode === 'auto_pixal3d';   // the modules Tripo does not take go to Pixal3D (free, local)
const usesTripo = e => e?.tripo_mode === 'auto' || e?.tripo_mode === 'auto_pixal3d';

/** "about 45 min" / "about 3.5 h" of local GPU time. */
function durationText(seconds) {
  const minutes = seconds / 60;
  return minutes >= 120 ? `about ${num(minutes / 60)} h` : `about ${Math.max(1, Math.round(minutes))} min`;
}

/** "3 on Pixal3D (free, about 14 min)" for a part (or the total) that has Pixal3D modules; '' otherwise. */
const pixalText = part => (part?.pixal3d_modules ? `${part.pixal3d_modules} on Pixal3D (free${part.pixal3d_seconds ? `, ${durationText(part.pixal3d_seconds)}` : ''})` : '');

/** "about 70 Hyper3D credits", "about 25 Tripo + 3 Hyper3D credits", "3 on Pixal3D (free, about 14 min)"; null while the split is unknown (see `range`). */
function creditsText(part) {
  if (!part || part.credits == null) return null;
  const tripo = part.tripo_credits || 0;
  const hyper = part.hyper3d_credits || 0;
  const pixal = pixalText(part);
  if (pixal) return tripo ? `about ${num(tripo)} Tripo credits + ${pixal}` : pixal;
  if (tripo && hyper) return `about ${num(tripo)} Tripo + ${num(hyper)} Hyper3D credits`;
  if (tripo) return `about ${num(tripo)} Tripo credits`;
  return `about ${num(hyper)} Hyper3D credits`;
}

/** What the credits would be while the Tripo balance is unread: all Hyper3D (or all Pixal3D, free) .. all Tripo. */
const rangeText = (total, e) => (total && total.min != null && total.max != null && total.min !== total.max
  ? `between ${num(total.min)} (all ${isPixal(e) ? 'Pixal3D' : 'Hyper3D'}) and ${num(total.max)} (all Tripo) credits` : null);

function balanceText(e) {
  const hyper = e.balances?.hyper3d;
  const tripo = e.balances?.tripo;
  const parts = e.tripo_mode === 'pixal3d' ? ['Pixal3D needs no credits'] : [hyper?.credits != null ? `${num(hyper.credits)} Hyper3D` : 'Hyper3D balance unknown'];
  if (usesTripo(e)) parts.push(tripo?.known ? `${num(tripo.credits)} Tripo` : 'Tripo balance not read yet');
  return parts.join(' + ');
}

/** The one-line summary, or '' when the server sent no estimate (an older server). */
export function estimateLine(e) {
  if (!e || typeof e !== 'object') return '';
  const head = 'Run everything now: ';
  if (e.pending) return `${head}estimating...`;
  if (!e.ready && !e.coming && !e.monsters) return `${head}estimate unavailable${e.error ? ` (${e.error})` : ''}`;
  const price = key => creditsText(e[key]) || partRange(e, key) || 'price unknown';
  const ready = e.ready?.modules ? `${plural(e.ready.modules, 'module')} in ${plural(e.ready.jobs, 'job')} ready, ${price('ready')}` : 'no modules ready';
  const coming = e.coming?.modules ? ` (+ ${e.coming.modules} still getting views, ${price('coming')})` : '';
  const body = ready + coming;
  const pieces = [body];
  if (e.monsters && (e.monsters.queued || e.monsters.failed_retryable)) {
    pieces.push(`monsters ${e.monsters.queued} queued${e.monsters.queued && e.monsters.credits != null ? `, about ${num(e.monsters.credits)} Hyper3D` : ''}`
      + `${e.monsters.failed_retryable ? ` (${e.monsters.failed_retryable} failed, needs Retry, not counted)` : ''}`);
  }
  const t = e.total;
  const more = (e.coming?.modules || 0) + (e.monsters?.queued || 0) > 0;
  if (more && t) {
    const whole = t.credits != null ? (isPixal(e) ? [`about ${num(t.credits)} credits`, pixalText(t)].filter(Boolean).join(', ') : `about ${num(t.credits)} credits`) : rangeText(t, e);
    if (whole) pieces.push(`total ${whole}`);
  }
  pieces.push(`balance ${balanceText(e)}`);
  if (e.error) pieces.push(`(${e.error})`);
  return head + pieces.join(' · ');
}

// While the split is unknown the server gives the range of the total only: a part's range is all Hyper3D .. all Tripo for its modules
function partRange(e, key) {
  const cost = e.costs || {};
  if (!e[key] || cost.hyper3d_per_module == null || cost.tripo_per_module == null) return null;
  return rangeText({ min: isPixal(e) ? 0 : e[key].modules * cost.hyper3d_per_module, max: e[key].modules * cost.tripo_per_module }, e);
}

/** Extra lines of the Run now confirmation: the same numbers, one per backend. */
export function estimateConfirmLines(e) {
  if (!e || e.pending || (!e.ready && !e.coming && !e.monsters)) return [];
  const lines = [];
  const t = e.total;
  if (t?.credits != null) {
    const split = [t.tripo_credits ? `${num(t.tripo_credits)} Tripo` : '', pixalText(t), !isPixal(e) || t.hyper3d_credits ? `${num(t.hyper3d_credits || 0)} Hyper3D` : ''].filter(Boolean).join(' + ');
    lines.push(`Estimated cost: about ${num(t.credits)} credits (${split}) `
      + `for ${plural(t.modules, 'module')}${e.monsters?.queued ? ` and ${plural(e.monsters.queued, 'monster task')}` : ''}; ${e.coming?.modules ? `${e.coming.modules} of the modules are still getting views, so they follow later. ` : ''}`
      + `Balance: ${balanceText(e)}.`);
  } else if (t) {
    lines.push(`Estimated cost: ${rangeText(t, e) || 'unknown'} for ${plural(t.modules, 'module')} (the Tripo balance has not been read, so the split is unknown). Balance: ${balanceText(e)}.`);
  }
  if (e.computed_at) lines.push(`Estimate computed ${ago(e.computed_at)}${e.stale ? ' (refreshing)' : ''}.`);
  return lines;
}

/** Hover text of the line: when the estimate was computed and what it counts. */
export function estimateTitle(e) {
  if (!e || e.pending) return '';
  return `Counted ${e.computed_at ? ago(e.computed_at) : 'a while ago'} from the convert worker's own rules (listed sets, no excludes, fewer than 3 failures). `
    + `Prices: Tripo ${e.costs?.tripo_per_module ?? '?'} and Hyper3D ${e.costs?.hyper3d_per_module ?? '?'} credits per module, monsters ${e.costs?.monster_per_task ?? '?'} per task`
    + `${isPixal(e) ? `; Pixal3D is free (${durationText(e.costs?.pixal3d_seconds_per_module || 270)} per module on the local GPU)` : ''}.`;
}
