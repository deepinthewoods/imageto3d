// Header status chip: `mode · paused? · services ok / n problems · queue health`, from GET /api/v2/state `status`; it links to Queue.
// The shape is read leniently (the server side may name things a little differently):
//   status.mode            'views-run' | 'pause-conversions' | 'pause' | 'run' | 'auto' | {mode, label}      (also status.mode_label)
//   status.pause | paused  false | true | {reason, since, scope: ['conversions', 'views']}                    the PAUSE file
//   status.services        {ok, problems: n | [text], errors: [text]}  |  [text]    (or status.services_problems: n | [text])
//   status.queue_health    'ok' | 'problem' | {ok, errors} | null     (absent: the state's own queue_health, see state.queueErrors)
// A server without `status` shows no chip.
import { queueHealthErrors } from './state.js';

const MODE_TEXT = {
  'views-run': 'Auto', 'pause-conversions': 'Pause conversions', pause: 'Pause all', run: 'Run now', auto: 'Supervisor auto',
};

const list = value => (Array.isArray(value) ? value.filter(Boolean).map(String) : value ? [String(value)] : []);
const count = value => (typeof value === 'number' ? Array(Math.max(0, value)).fill('') : list(value));

/** {text, tone, title} or null. `queueErrors` = the error texts of the state's queue_health. */
export function chipModel(status, queueErrors = []) {
  if (!status || typeof status !== 'object') return null;
  const rawMode = status.mode;
  const modeKey = typeof rawMode === 'string' ? rawMode : rawMode?.mode;
  const mode = status.mode_label || (rawMode && typeof rawMode === 'object' && rawMode.label) || MODE_TEXT[modeKey] || (modeKey ? String(modeKey) : null);
  const pause = status.pause ?? status.paused;
  const paused = !!pause;
  const scope = paused && typeof pause === 'object' ? list(pause.scope) : [];
  let problems = [];
  const services = status.services;
  if (services && typeof services === 'object' && !Array.isArray(services)) {
    problems = count(services.problems ?? services.errors);
    if (!problems.length && services.ok === false) problems = [''];
  } else if (Array.isArray(services)) {
    problems = list(services);
  } else if (status.services_problems != null) {
    problems = count(status.services_problems);
  }
  const health = status.queue_health;
  const healthy = typeof health === 'string' && /^(ok|healthy)$/i.test(health.trim());       // 'ok' is a verdict, not an error text
  const queue = [...(health === undefined || healthy ? [] : queueHealthErrors(health)), ...queueErrors];     // the state's own queue_health always counts
  const queueBad = queue.length > 0;
  const parts = [];
  if (mode) parts.push(mode);
  if (paused) parts.push(scope.length === 1 ? `PAUSED (${scope[0]})` : 'PAUSED');
  parts.push(problems.length ? `${problems.length} service problem${problems.length === 1 ? '' : 's'}` : 'services ok');
  parts.push(queueBad ? 'queue problem' : 'queue ok');
  const tone = problems.length || queueBad ? 'error' : paused ? 'warn' : 'ok';
  const detail = [
    paused && typeof pause === 'object' && pause.reason ? `PAUSE: ${pause.reason}${pause.since ? ` (since ${pause.since})` : ''}` : null,
    ...problems.filter(Boolean), ...queue,
  ].filter(Boolean);
  return { text: parts.join(' · '), tone, title: ['Pipeline status - click for the Queue tab', ...detail].join('\n') };
}

/** Draw the chip into `node` (the <a id="status-chip"> of index.html): hidden without a status. */
export function drawChip(node, status, queueErrors) {
  if (!node) return;
  const model = chipModel(status, queueErrors);
  node.hidden = !model;
  if (!model) return;
  node.textContent = model.text;
  node.title = model.title;
  node.className = `status-chip tone-${model.tone}`;
}
