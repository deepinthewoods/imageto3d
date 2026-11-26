// Queue tab: phase 4 sections above (services, mode / pause, credits and disk, views, conversions, holds, settings: queue_sections.js),
// then the phase 3 "Layer builds" table: the tasks of GET /api/v2/queue with state, elapsed, attempts, the error tail, Retry / Cancel.
// Refreshes every 3 s while the tab is visible (and the page is).
// Phase 3 review fixes: a task that is `stalled` has "Kill & retry" (POST /api/v2/queue {op: 'kill_retry', id}); "Rebuild stale"
// (POST /api/v2/queue {op: 'rebuild_stale'}) re-queues every stale layer build of the sets in Aligned / Completed / 3D; the queue's
// own health errors (state.queue_health, or `queue_health` / `health` of the queue response) are shown at the top.
import { api } from './api.js';
import { state, refresh, subscribe, queueHealthErrors } from './state.js';
import { toast } from './toast.js';
import { badge, blockRepeat, button, duration, h, pretty } from './util.js';
import { createSections } from './queue_sections.js';
import { runNowTask } from './monster_views.js';   // phase 5: Run now of a monster conversion (409 `confirm` while paused by hand)

const MONSTER_LANE = 'monster_convert';

// Phase 3 re-check: the queue response carries `totals` (every state, whole table) and `page` (what was cut); the active tasks (starting /
// running / stalled / orphan) are always shown first with their controls; an `orphan` (an adopted builder whose wrapper died) has Kill.
// kill_retry carries the `token` of the attempt the user saw (the server refuses a kill of a newer attempt).
const TONE = { running: 'info', starting: 'info', queued: 'info', done: 'ok', failed: 'error', cancelled: 'warn', stalled: 'error', orphan: 'error' };
const OP_TEXT = { retry: ['Retrying', 'Retry'], cancel: ['Cancelled', 'Cancel'], kill_retry: ['Killed and re-queued', 'Kill & retry'], kill: ['Killed', 'Kill'] };
const ACTIVE = ['running', 'starting', 'stalled', 'orphan'];
const isActive = task => ACTIVE.includes(task.state);
/** What identifies a displayed task beyond its row number: {key, generation?, conversion_seq?} (generation: the frozen view list of a
 * monster conversion; conversion_seq: the disposition revision a failed / cancelled monster row is shown with, which Retry echoes). */
const identity = task => ({ ...(task.key ? { key: task.key } : {}), ...(task.generation ? { generation: task.generation } : {}),
  ...(Number.isInteger(task.conversion_seq) ? { conversion_seq: task.conversion_seq } : {}) });

/** Sets whose card carries a "stale n" badge (the stale scan found their published builds, or their bare builds, out of date). */
const staleSets = () => ['3d', 'aligned', 'completed'].flatMap(tab => state.tabs[tab] || [])
  .filter(card => (card.badges || []).some(b => /^stale\b/i.test(b.text || '')));
const POLL_MS = 3000;

/** Build the page into `container`. Returns {unmount}. */
export function mountQueue(container) {
  let alive = true;
  let timer = null;
  let busy = new Set();            // task ids with a request in flight
  let lastText = '';
  const open = new Set();          // task ids whose log is expanded

  const summary = h('div', { class: 'queue-summary' });
  const monsterLine = h('div', { class: 'q-monsters', hidden: true });     // phase 5: the monster conversion lane (queue.monsters)
  const table = h('div', { class: 'queue-table' });
  const note = h('div', { class: 'queue-note', hidden: true });
  const health = h('div', { class: 'queue-note health', hidden: true, role: 'alert' });
  const bar = h('div', { class: 'queue-bar' });
  let data = null;
  const sections = createSections({ reload: () => load() });
  const page = h('div', { class: 'queue-page' }, health, note, sections.node,
    h('section', { class: 'q-layers' }, h('h3', { class: 'q-heading', text: 'Layer builds' }), summary, monsterLine, bar, table));
  blockRepeat(page);                  // a held Enter never repeats a write button
  container.replaceChildren(page);
  let rebuilding = false;

  /** Queue health: the state's `queue_health` plus the one of the queue response, e.g. a damaged or unreadable queue database. */
  function drawHealth() {
    const errors = [...new Set([...(state.queueErrors || []), ...queueHealthErrors(data?.queue_health ?? data?.health)])];
    health.hidden = !errors.length;
    health.replaceChildren(...(errors.length ? [h('b', { text: 'Queue problem' }), ...errors.map(text => h('div', { text }))] : []));
  }

  function drawBar() {
    const stale = staleSets();
    const button1 = button(rebuilding ? 'Rebuilding...' : 'Rebuild stale', rebuildStale, stale.length ? 'primary' : '',
      'Queue a rebuild of every stale layer build (built with older layer code)');
    button1.disabled = rebuilding || !stale.length;
    bar.replaceChildren(button1, h('span', { class: 'q-stale', text: stale.length
      ? `${stale.length} set${stale.length === 1 ? '' : 's'} with stale builds: ${stale.map(c => c.title || pretty(c.name)).join(', ')}` : 'No stale builds' }));
  }

  async function rebuildStale() {
    if (rebuilding) return;
    rebuilding = true;
    drawBar();
    try {
      const result = await api.queueOp('rebuild_stale');
      const n = result?.queued;
      toast(n != null ? `Rebuilding ${n} stale build${n === 1 ? '' : 's'}` : 'Stale builds queued', { ms: 2500 });
    } catch (error) {
      if (!error.conflict) toast(`Rebuild stale failed: ${error.message}`);
    } finally {
      rebuilding = false;
      drawBar();
    }
    refresh().catch(() => {});          // the stale badges of the cards go away once the rebuild is under way
    await load();
  }

  /** `kind` is the op sent; `label` picks the texts (Kill of an orphan is the kill_retry op). */
  async function op(kind, task, label = kind) {
    if (busy.has(task.id)) return;
    busy.add(task.id);
    try {
      // the task as displayed: its key (and, for a monster conversion, its frozen generation). The server acts on THAT task and answers
      // 409 `changed` when the row number means another task by now (a rebuilt queue renumbers its rows).
      await api.queueOp(kind, task.id, { ...identity(task), ...(kind === 'kill_retry' && task.token ? { token: task.token } : {}) });
      toast(`${OP_TEXT[label][0]} task ${task.id}`, { ms: 2000 });
    } catch (error) {
      if (!error.conflict) toast(`${OP_TEXT[label][1]} failed: ${error.message}`);
    } finally {
      busy.delete(task.id);
    }
    await load();
  }

  /** Run now of a queued monster conversion: overrides the idle gate; while paused by hand the server asks first (409 `confirm`: its
   * challenge with the pauses in force and their reasons, which the dialog shows; runNowTask). */
  async function runNow(task) {
    if (busy.has(task.id)) return;
    busy.add(task.id);
    try {
      const result = await runNowTask({ id: task.id, ...identity(task) });
      if (result) toast(result.warning ? `Run now: ${result.warning}` : `Task ${task.id} starts now`, { ms: result.warning ? 8000 : 2500 });
    } catch (error) {
      toast(`Run now failed: ${error.message}`);
    } finally {
      busy.delete(task.id);
    }
    await load();
  }

  /** The monster lane: gate, counts and the Hyper3D credits spent (paid_log.jsonl). */
  function drawMonsters(lane) {
    monsterLine.hidden = !lane;
    if (!lane) return;
    if (lane.error) { monsterLine.replaceChildren(h('strong', { text: 'Monster conversions' }), badge(`error: ${lane.error}`, 'error')); return; }
    const gate = lane.gate || {};
    const totals = Object.entries(lane.totals || {}).filter(([, n]) => n);
    monsterLine.replaceChildren(h('strong', { text: 'Monster conversions' }),
      badge(gate.open ? 'gate open' : `waiting: ${gate.reason || gate.code || 'closed'}`, gate.open ? 'ok' : 'warn'),
      ...totals.map(([state, n]) => badge(`${state} ${n}`, TONE[state] || '')),
      h('span', { text: `limit ${lane.limit ?? 1} · paid ${lane.paid?.count ?? 0} (${lane.paid?.credits ?? 0} Hyper3D credits)` }));
  }

  function row(task) {
    const failed = task.state === 'failed';
    const attention = failed || task.state === 'stalled' || task.state === 'orphan';
    const log = task.log_tail || '';
    const errorText = task.error || '';
    const actions = h('span', { class: 'queue-actions' },
      failed || task.state === 'cancelled' ? button('Retry', () => op('retry', task), 'primary', 'Run this task again') : null,
      task.state === 'stalled' ? button('Kill & retry', () => op('kill_retry', task), 'danger', 'Stop the stuck build and queue it again') : null,
      task.state === 'orphan' ? button('Kill', () => op('kill_retry', task, 'kill'), 'danger',
        'The build is still running but its wrapper died: stop it (the work is queued again when the approval still stands)') : null,
      task.state === 'queued' && task.lane === MONSTER_LANE
        ? button('Run now', () => runNow(task), 'primary', 'Start this monster conversion now (Hyper3D, paid), even while you are at the PC') : null,
      task.state === 'queued' ? button('Cancel', () => op('cancel', task), '', 'Remove it from the queue (a running build finishes on its own)') : null);
    const expanded = open.has(task.id);
    return h('div', { class: 'queue-row' + (attention ? ' failed' : ''), dataset: { id: task.id, state: task.state } },
      h('span', { class: 'q-id', text: `#${task.id}` }),
      h('span', { class: 'q-lane', text: task.lane || '' }),
      h('span', { class: 'q-what' }, h('b', { text: task.set ? pretty(task.set) : '' }), task.unit ? ` ${pretty(task.unit)}` : ''),
      h('span', { class: 'q-state' }, badge(task.state, TONE[task.state] || ''), task.attempts > 1 ? ` attempt ${task.attempts}` : ''),
      h('span', { class: 'q-time', text: duration(task.elapsed) }),
      actions,
      errorText ? h('pre', { class: 'q-error', text: errorText }) : null,
      log ? h('details', { class: 'q-log', open: expanded, ontoggle: event => { if (event.target.open) open.add(task.id); else open.delete(task.id); } },
        h('summary', { text: 'log' }), h('pre', { text: log })) : null);
  }

  /** "Showing 25 of 1,040 open tasks and 50 of 3,000 recent tasks": the server cut the list (page.truncated). */
  function pageText(page, shown) {
    if (!page) return '';
    const n = value => Number(value).toLocaleString('en-US');
    const parts = [];
    if (page.open_total != null && page.open_shown != null && page.open_shown < page.open_total) parts.push(`${n(page.open_shown)} of ${n(page.open_total)} open tasks`);
    if (page.recent_total != null && page.recent_shown != null && page.recent_shown < page.recent_total) parts.push(`${n(page.recent_shown)} of ${n(page.recent_total)} recent tasks`);
    if (!parts.length && page.truncated) parts.push(`${n(shown)} tasks`);
    return parts.length ? `Showing ${parts.join(' and ')} - the rest is hidden (active tasks are always listed first).` : '';
  }

  function draw(data) {
    // active tasks first (stable: the server's order within each group)
    const tasks = [...(data?.tasks || [])].map((task, index) => [task, index])
      .sort((a, b) => (isActive(b[0]) - isActive(a[0])) || a[1] - b[1]).map(([task]) => task);
    const workers = data?.workers;
    const counts = {};
    for (const t of tasks) counts[t.state] = (counts[t.state] || 0) + 1;
    const totals = data?.totals && typeof data.totals === 'object' ? data.totals : counts;     // the whole table, not the returned rows
    const cut = pageText(data?.page, tasks.length);
    summary.replaceChildren(...[
      h('strong', { text: 'Queue' }),
      ...Object.entries(totals).filter(([, n]) => n).map(([state, n]) => badge(`${state} ${n}`, TONE[state] || '')),
      workers ? h('span', { class: 'q-workers', text: `workers ${workers.running ?? 0}/${workers.limit ?? '?'} · ${workers.present ? 'you are at the PC' : 'away'}` }) : null,
      cut ? h('span', { class: 'q-page', text: cut }) : null].filter(Boolean));     // (a null child would be drawn as the text "null")
    table.replaceChildren(...(tasks.length ? tasks.map(row) : [h('div', { class: 'queue-empty', text: 'No tasks in the last 24 hours.' })]));
  }

  async function load() {
    if (!alive) return;
    try {
      data = await api.queue();
      if (!alive) return;
      note.hidden = true;
      const text = JSON.stringify({ ...data, services: undefined, mode: undefined, credits: undefined, disk: undefined, views: undefined,
        conversions: undefined, holds: undefined, settings: undefined, monsters: undefined });    // the table redraws only for its own data
      if (text !== lastText) { lastText = text; draw(data); }
      drawMonsters(data.monsters);
      sections.update(data);
      drawHealth();
      drawBar();
    } catch (error) {
      if (!alive) return;
      note.hidden = false;
      note.textContent = `Queue not available: ${error.message}`;
    }
  }

  const unsubscribe = subscribe(() => { drawHealth(); drawBar(); });   // stale badges and queue health arrive with the state
  drawHealth();
  drawBar();
  load();
  timer = setInterval(() => { if (!document.hidden) load(); }, POLL_MS);
  return {
    unmount() {
      alive = false;
      unsubscribe();
      sections.dispose();
      clearInterval(timer);
      container.replaceChildren();
    },
  };
}
