// Approved modal for monster variants (`monster:<Monster>/<variant>` cards in the Approved tab): the four views large (front / left /
// back / right) with an Enabled tick each, the state of the Codex task per view (queued / running / failed + Retry), "Use enabled views"
// (verb `convert`: confirms the exact ticked files, then freezes them and queues the Hyper3D conversion), the frozen state, and the conversion progress
// (submit / preview / collect / reduce, paid flag, error, record status and screenshots, "waiting: ..." with Run now, Retry of a failed
// conversion). A flat pick made before v2 (`importable`) shows Import instead. Item shape: tools/preview/v2/DESIGN_PHASE5.md section H.
import { api } from './api.js';
import { confirmDialog } from './confirm.js';
import { showImage } from './modal.js';
import { toast } from './toast.js';
import { refresh } from './state.js';
import { conflictMessage, damageText, isDamaged, viewFile } from './monster_common.js';
import { ago, badge, blockRepeat, button, h, pretty } from './util.js';

const NAMES = ['front', 'left', 'back', 'right'];
const STEPS = ['submit', 'preview', 'collect', 'reduce'];
const WAITING = { paused: 'paused', user_present: 'you are at the PC', browser_busy: 'the Hyper3D browser is busy',
  held: 'conversions are held until the supervisor runs (CONVERT_HOLD)' };
const TASK_TONE = { queued: 'info', running: 'info', done: 'ok', failed: 'error' };
const POLL_ACTIVE_MS = 4000;
const POLL_IDLE_MS = 10000;

const generating = view => ['queued', 'running'].includes(view?.task?.state);
const hasFile = view => !!view?.url;
const waitingText = reason => WAITING[reason] || reason || 'unknown';

/**
 * Run now of a monster conversion task: POST /api/v2/queue {op: run_now, id, key, generation, confirm?, pause_id?}. `task` = {id, key,
 * generation} of the task AS DISPLAYED (the server finds it by key + generation; the row number is only a hint; 409 `changed` when that
 * task is gone). While conversions are paused by hand the server needs the user's yes FOR THAT PAUSE: `confirm: true` plus the
 * `pause_id` the user was shown. `askFirst` + `pauseId` (the item says it is paused, and by which pause) ask before the first request;
 * otherwise a 409 `confirm` carries the `pause_id` to ask about. A pause that changed while the dialog was open is asked about again.
 * Resolves the response, or null when the user said no. Errors other than the confirmation are thrown.
 */
export async function runNowTask(task, { askFirst = false, pauseId = null } = {}) {
  const base = { op: 'run_now', id: task.id, key: task.key, generation: task.generation };
  const ask = async message => confirmDialog({
    title: 'Run now although conversions are paused?',
    lines: [message || 'Conversions were paused by hand. Run now starts this conversion anyway (Hyper3D, paid: 0.5 credits).'],
    confirm: 'Run now',
    danger: true,
  });
  let confirmed = null;              // the pause_id the user said yes to
  if (askFirst && pauseId) {
    if (!(await ask())) return null;
    confirmed = pauseId;
  }
  for (let round = 0; round < 3; round += 1) {
    try {
      return await api.queuePost(confirmed ? { ...base, confirm: true, pause_id: confirmed } : base);
    } catch (error) {
      if (error.code !== 'confirm') throw error;
      const shown = error.body?.pause_id;        // the pause in force now: the one the user is asked about
      if (!shown) throw error;
      if (!(await ask(error.message))) return null;
      confirmed = shown;
    }
  }
  throw new Error('the pause kept changing; nothing was started');
}

export async function showMonsterViews(modal, host) {
  const { card } = host;
  const root = h('div', { class: 'mv-root' }, h('p', { text: 'Loading...' }));
  blockRepeat(root);                 // a held Enter never repeats Retry / Run now
  modal.body.append(root);
  let item = null;
  let timer = null;
  let converting = false;            // "Use enabled views" / Import is on the wire (the item is not refreshed meanwhile)
  let preparing = false;             // "Use enabled views" waits for a tick write / its confirmation
  let runningNow = false;
  let retryingConversion = false;
  const retrying = new Set();
  let writes = Promise.resolve();    // tick writes go out in order
  let pendingWrites = 0;
  let epoch = 0;                     // bumped by every tick / convert: an item fetched before it is stale and dropped
  const local = {};                  // ticks the user set; dropped when a refreshed item (which carries them) is applied, or when their write failed
  const figs = new Map();

  const views = () => item?.views || [];
  const viewOf = name => views().find(v => v.name === name);
  const importing = () => item?.importable === true;
  const ticked = view => (view.name in local ? local[view.name] : view.enabled) === true && hasFile(view) && view.valid !== false;
  const enabledNames = () => views().filter(ticked).map(v => v.name);
  const enabledFiles = () => views().filter(ticked).map(viewFile);
  const damaged = () => isDamaged(item);
  const tickMap = () => Object.fromEntries(views().filter(hasFile).map(v => [v.name, ticked(v)]));   // only views that exist: the server merges it

  // ---- the four views ----------------------------------------------------------------------------------------------
  function makeFigure(name) {
    const img = h('img', { alt: name, title: 'Click for full size' });
    const holder = h('div', { class: 'mv-frame' });
    const tick = h('input', { type: 'checkbox', dataset: { view: name }, onchange: () => setTick(name, tick.checked) });
    const label = h('label', { class: 'mv-tick', title: 'Use this view for the conversion' }, tick, 'Enabled');
    const state = h('span', { class: 'mv-state' });
    const error = h('div', { class: 'unit-error', hidden: true });
    const retry = button('Retry', () => retryView(name), '', 'Make this view again');
    retry.dataset.action = 'retry';
    retry.hidden = true;
    // a good picture whose record (provenance) is missing or broken: the record is written again, the picture is NOT drawn again
    const repair = button('Repair provenance', () => repairView(name), '', 'Write the missing record of this picture again; the picture itself is kept');
    repair.dataset.action = 'repair';
    repair.hidden = true;
    const node = h('figure', { class: 'mv-fig', dataset: { view: name } },
      holder, h('figcaption', null, h('strong', { text: pretty(name) }), label, state, retry, repair), error);
    const fig = { node, img, holder, tick, state, error, retry, repair, label, shownUrl: null };
    figs.set(name, fig);
    return fig;
  }

  function syncFigure(name) {
    const fig = figs.get(name) || makeFigure(name);
    const view = viewOf(name) || { name, url: null, enabled: false, task: null };
    if (hasFile(view)) {
      if (fig.shownUrl !== view.url) {
        fig.img.src = view.url;
        fig.img.onclick = () => showImage(view.url);
        fig.shownUrl = view.url;
      }
      if (fig.img.parentNode !== fig.holder) fig.holder.replaceChildren(fig.img);
    } else {
      fig.shownUrl = null;
      const task = view.task;
      const text = importing() ? 'Drawn after Import' : !task ? (view.retry ? 'Not queued (Retry writes the task)' : 'Not started')
        : task.state === 'failed' ? 'Failed' : task.state === 'running' ? 'Being drawn...' : 'Waiting in the queue';
      fig.holder.replaceChildren(h('div', { class: 'mv-pending', text }));
    }
    const unusable = hasFile(view) && view.valid === false;       // the picture exists but cannot be used as it is (view.problem)
    fig.tick.checked = ticked(view) && !unusable;
    fig.tick.disabled = !hasFile(view) || unusable || importing() || damaged();
    fig.label.title = importing() ? 'Import the pick first' : unusable ? `Not usable: ${view.problem || 'invalid'}`
      : hasFile(view) ? 'Use this view for the conversion' : 'The picture does not exist yet';
    const task = view.task;
    const frozenHas = !!item?.frozen && frozenNames().includes(name);
    fig.state.replaceChildren(...[
      task ? badge(task.state === 'running' ? `running${task.attempts ? ` (try ${task.attempts}${task.max_attempts ? `/${task.max_attempts}` : ''})` : ''}` : task.state, TASK_TONE[task.state] || '') : null,
      frozenHas ? badge('frozen', 'ok') : null,
    ].filter(Boolean));
    const failedText = task?.state === 'failed' ? `Failed after ${task.attempts ?? '?'} attempt${task.attempts === 1 ? '' : 's'}${task.error ? `: ${task.error}` : ''}` : '';
    const problemText = view.repair === 'provenance' ? `The picture is good, but its record is not: ${view.problem || 'provenance missing'}. Repair provenance writes the record again (nothing is drawn again).`
      : unusable ? `Not usable: ${view.problem || 'invalid picture'}` : '';
    fig.error.hidden = !(failedText || problemText);
    fig.error.textContent = view.repair === 'provenance' ? problemText : (failedText || problemText);
    fig.error.dataset.problem = view.repair === 'provenance' ? 'provenance' : unusable ? 'invalid' : '';
    fig.retry.hidden = !view.retry || damaged();
    fig.retry.disabled = retrying.has(name);
    fig.repair.hidden = view.repair !== 'provenance' || damaged();
    fig.repair.disabled = retrying.has(name);
    fig.node.classList.toggle('disabled', hasFile(view) && !ticked(view));
  }

  function setTick(name, on) {
    epoch += 1;
    local[name] = on;
    pendingWrites += 1;
    syncAll();
    const enabled = tickMap();
    writes = writes.then(async () => {
      try {
        await api.action(card.id, 'views', { args: { enabled } });
      } catch (error) {
        if (!error.conflict) toast(`Ticks not saved: ${error.message}`);
        delete local[name];
      } finally {
        pendingWrites -= 1;            // `local` stays until the refreshed item is applied: the ticks never fall back to stale item data
      }
      if (!pendingWrites) await load();
    });
  }

  /** Retry of a failed (or never written) view task: the monster verb `view_retry` (args.view). */
  async function retryView(name) {
    if (!viewOf(name)?.retry || retrying.has(name)) return;
    retrying.add(name);
    syncAll();
    try {
      await api.action(card.id, 'view_retry', { args: { view: name } });
      toast(`Drawing the ${name} view again`, { ms: 3000 });
    } catch (error) {
      if (!error.conflict) toast(`Retry failed: ${error.message}`);
    } finally {
      retrying.delete(name);
    }
    refresh().catch(() => {});
    await load();
  }

  /** "Repair provenance": the monster verb `view_repair` (args.view). The picture stays; only its record is written again. */
  async function repairView(name) {
    if (viewOf(name)?.repair !== 'provenance' || retrying.has(name)) return;
    retrying.add(name);
    syncAll();
    try {
      await api.action(card.id, 'view_repair', { args: { view: name } });
      toast(`The record of the ${name} view was written again; the picture was kept`, { ms: 4000 });
    } catch (error) {
      if (!error.conflict) toast(`Repair failed: ${error.message}`);
    } finally {
      retrying.delete(name);
    }
    refresh().catch(() => {});
    await load();
  }

  // ---- Use enabled views / Import -------------------------------------------------------------------------------------
  const frozenNames = () => {
    const frozen = item?.frozen;
    if (!frozen) return [];
    return frozen.names || (frozen.files || []).map(file => String(file).replace(/\.[a-z]+$/i, ''));
  };

  /** A frozen list may be chosen again only while nothing was spent on it: its conversion failed before any Hyper3D record existed. */
  const refreezable = () => item?.conversion?.state === 'failed' && !item.conversion.paid && !item.conversion.record;

  /** Why "Use enabled views" cannot be pressed now (null = it can). */
  function convertBlock() {
    if (!item) return 'Loading...';
    if (damaged()) return damageText(item);
    if (item.stage && item.stage !== 'approved') return `This variant is already in ${pretty(item.stage)}`;
    if (item.frozen && !refreezable()) return 'The views are frozen; Reconvert is in the 3D tab';
    if (!views().length) return 'This variant has no view choice';
    if (!enabledNames().length) return 'Tick at least one view';
    if (converting || preparing) return 'Starting...';
    return null;
  }

  const useButton = button('Use enabled views', () => convert(), 'primary', 'Freeze the ticked views and start the conversion');
  useButton.dataset.action = 'convert';
  const importButton = button('Import', () => importPick(), 'primary', 'Copy this pick into views/ and draw the missing views');
  importButton.dataset.action = 'import';
  importButton.hidden = true;
  const reason = h('span', { class: 'mv-reason' });
  modal.actions.append(useButton, importButton, reason, button('Previous', () => host.go(-1)), button('Next', () => host.go(1)));

  async function convert() {
    if (convertBlock()) return;
    preparing = true;
    syncAll();
    try {
      // a tick write still on the wire is waited for (its refresh included): the list below is then what the server holds
      if (pendingWrites) await writes;
      if (!host.isCurrent()) return;
      const blocked = convertBlock();
      if (blocked && blocked !== 'Starting...') { toast(blocked); return; }
      // the exact list the user confirms: captured here, together with the views revision it belongs to, never rebuilt from item data later
      const files = enabledFiles();
      const names = enabledNames();
      const viewsRev = item.views_rev;
      if (!files.length) { toast('Use enabled views: tick at least one view'); return; }
      const busy = views().filter(generating);
      const ok = await confirmDialog({
        title: 'Use these views for the conversion?',
        lines: [`Exactly these pictures are used: ${files.join(', ')}. The conversion is a Hyper3D job (0.5 credits).`,
          ...(busy.length ? [`${busy.map(v => pretty(v.name)).join(', ')} ${busy.length === 1 ? 'is' : 'are'} still being drawn; a view that arrives later is not used.`] : [])],
        confirm: 'Use enabled views',
      });
      if (!ok || !host.isCurrent()) return;
      converting = true;
      epoch += 1;
      syncAll();
      try {
        const result = await api.action(card.id, 'convert', { args: { files, views_rev: viewsRev }, quiet: true });
        toast(`Using ${names.length} view${names.length === 1 ? '' : 's'} (${names.join(', ')}): the conversion is queued${result?.warning ? ` · Warning: ${result.warning}` : ''}`, { ms: 6000 });
      } catch (error) {
        if (error.conflict) toast(conflictMessage(error, 'views'), { ms: 6000 });
        else toast(`Use enabled views failed: ${error.message}`);
      } finally {
        converting = false;
      }
    } finally {
      preparing = false;
      if (item && host.isCurrent()) syncAll();
    }
    refresh().catch(() => {});
    await load();
  }

  async function importPick() {
    if (!importing() || converting || damaged()) return;
    const tick = h('input', { type: 'checkbox', id: 'mv-import-unattended' });
    const content = h('label', { class: 'mc-unattended', title: 'Skip the "Choose views" step for this variant' },
      tick, h('span', null, h('b', { text: 'Unattended: convert straight away with all views' }),
        h('small', { text: 'Off: the card stops at "Choose views" and nothing is spent until you press Use enabled views.' })));
    const ok = await confirmDialog({
      title: `Import ${item.monster || card.title} ${item.variant ?? ''}?`.trim(),
      lines: ['This pick was made before the app managed monsters. Its flat files are copied into views/ (they stay where they are), keeping the variant name, and the missing views are drawn.'],
      content,
      confirm: 'Import',
    });
    if (!ok || !host.isCurrent()) return;
    converting = true;
    syncAll();
    try {
      const result = await api.action(card.id, 'import', { args: { unattended: tick.checked } });
      const queued = (result?.tasks || []).length;
      toast(`Imported ${item.monster || ''} ${item.variant ?? ''}: ${queued} view${queued === 1 ? '' : 's'} queued${result?.warning ? ` · Warning: ${result.warning}` : ''}`, { ms: 5000 });
    } catch (error) {
      if (!error.conflict) toast(`Import failed: ${error.message}`);
    } finally {
      converting = false;
    }
    refresh().catch(() => {});
    await load();
  }

  // ---- conversion ---------------------------------------------------------------------------------------------------
  async function runNow() {
    const conversion = item?.conversion;
    if (!conversion?.task_id || runningNow || damaged()) return;
    runningNow = true;
    draw();
    try {
      // the task and the pause exactly as this dialog shows them: the server refuses (409) when either is something else by now
      const result = await runNowTask({ id: conversion.task_id, key: conversion.key, generation: conversion.generation },
        { askFirst: conversion.waiting_reason === 'paused', pauseId: conversion.pause_id });
      if (result) toast(result.warning ? `Run now: ${result.warning}` : 'Conversion starts now', { ms: result.warning ? 8000 : 4000 });
    } catch (error) {
      toast(`Run now failed: ${error.message}`);
    } finally {
      runningNow = false;
      if (item && host.isCurrent()) draw();
    }
    refresh().catch(() => {});
    await load();
  }

  /** Retry of a failed conversion (the queue's Retry): it resumes from the Hyper3D record and never pays a step twice. */
  async function retryConversion() {
    const conversion = item?.conversion;
    if (!conversion?.task_id || retryingConversion || damaged()) return;
    retryingConversion = true;
    draw();
    try {
      await api.queueOp('retry', conversion.task_id, { key: conversion.key, generation: conversion.generation });
      toast('Conversion queued again (it resumes from its record)', { ms: 4000 });
    } catch (error) {
      if (!error.conflict) toast(`Retry failed: ${error.message}`);
    } finally {
      retryingConversion = false;
    }
    refresh().catch(() => {});
    await load();
  }

  function conversionBlock() {
    const c = item?.conversion;
    if (!c) return null;
    const failed = c.state === 'failed';
    const done = c.state === 'done';
    const at = STEPS.indexOf(c.step);
    const steps = h('ol', { class: 'mv-steps' }, STEPS.map((step, i) => {
      const mode = done || (at >= 0 && i < at) ? 'done' : i === at ? (failed ? 'failed' : c.state === 'waiting' ? 'waiting' : 'current') : 'pending';
      return h('li', { class: `mv-step ${mode}`, dataset: { step, mode }, text: step });
    }));
    const head = h('div', { class: 'mv-conv-head' }, h('strong', { text: 'Conversion' }),
      badge(done ? 'converted' : failed ? 'failed' : c.state === 'waiting' ? 'waiting' : c.state === 'queued' ? 'queued' : `running: ${c.step || '...'}`,
        done ? 'ok' : failed ? 'error' : c.state === 'waiting' ? 'warn' : 'info'),
      c.paid ? badge(`paid${c.credits != null ? ` ${c.credits} credits` : ''}`, 'warn') : badge('nothing paid yet', ''),
      c.run_now ? badge('run now', 'info') : null);
    const nodes = [head, steps];
    if (c.state === 'waiting' && !damaged()) {
      const run = button(runningNow ? 'Starting...' : 'Run now', runNow, 'primary', 'Start this conversion now');
      run.disabled = runningNow || !c.task_id || c.run_now && c.waiting_reason === 'held';
      run.dataset.action = 'run-now';
      nodes.push(h('div', { class: 'mv-waiting', dataset: { waiting: c.waiting_reason || '' } }, h('span', { text: `waiting: ${waitingText(c.waiting_reason)}` }),
        c.task_id ? run : null));
    }
    if (c.error) nodes.push(h('div', { class: 'unit-error', text: c.error }));
    if (failed && c.record) {
      nodes.push(h('p', { class: 'mv-note', dataset: { note: 'record' }, text: `Hyper3D record: ${c.record.status || 'unknown'}${c.record.stage ? ` (${c.record.stage})` : ''}${c.record.pending_action ? `, pending ${c.record.pending_action}` : ''}${c.record.why ? ` - ${c.record.why}` : ''}` }));
    }
    if (failed && c.screenshots?.length) {
      nodes.push(h('div', { class: 'mv-shots' }, c.screenshots.map(url => {
        const img = h('img', { src: url, alt: 'screenshot', title: 'Click for full size' });
        img.onclick = () => showImage(url);
        return img;
      })));
    }
    if (failed && c.task_id && !damaged()) {
      const retry = button(retryingConversion ? 'Retrying...' : 'Retry', retryConversion, 'primary', 'Queue the conversion again; it resumes from the Hyper3D record');
      retry.disabled = retryingConversion;
      retry.dataset.action = 'retry-conversion';
      nodes.push(h('div', null, retry));
    }
    if (done && item.stage === 'approved') nodes.push(h('p', { class: 'mv-note', text: 'Reducing is finished; the variant moves to 3D at the next refresh.' }));
    return h('div', { class: 'mv-conv' + (failed ? ' failed' : '') }, ...nodes);
  }

  // ---- drawing ------------------------------------------------------------------------------------------------------
  function frozenBlock() {
    const frozen = item?.frozen;
    if (importing()) {
      return h('p', { class: 'mv-note', dataset: { note: 'import' }, text: 'Picked before views existed: Import copies the pick into views/ and draws the missing views.' });
    }
    if (!frozen) {
      return item?.unattended && !item?.legacy
        ? h('p', { class: 'mv-note', dataset: { note: 'unattended' }, text: 'Unattended: when the views are made, the conversion starts by itself with every view that exists.' })
        : null;
    }
    const names = frozenNames();
    const now = enabledNames();
    const same = names.length === now.length && names.every(n => now.includes(n));
    return h('div', { class: 'mv-frozen' },
      h('p', { text: `Frozen ${frozen.time ? ago(frozen.time) : ''}: ${names.map(pretty).join(', ') || 'no views'}. Reconvert is in the 3D tab.`.replace('Frozen :', 'Frozen:') }),
      same ? null : h('p', { class: 'mv-note warn', dataset: { note: 'draft' }, text: refreezable()
        ? `The ticks now (${now.map(pretty).join(', ') || 'none'}) differ from the frozen views. Nothing was paid yet: Use enabled views chooses them again.`
        : `The ticks now (${now.map(pretty).join(', ') || 'none'}) differ from the frozen views. They are a new draft; nothing converts again until you press Reconvert in the 3D tab.` }));
  }

  function syncAll() {
    for (const name of NAMES) syncFigure(name);
    importButton.hidden = !importing() || damaged();
    importButton.disabled = converting;
    useButton.hidden = importing();
    const block = importing() ? null : convertBlock();
    useButton.disabled = !!block;
    reason.textContent = block && item ? block : '';
    reason.dataset.reason = block ? '1' : '';
    useButton.title = block || 'Freeze the ticked views and start the conversion';
  }

  function draw() {
    // the header badges follow the card as the server sees it now (views a/b, Choose views, conversion state), not as it was when opened
    if (item.card?.badges) modal.setBadges([badge('Monster', 'info'), ...item.card.badges.map(b => badge(b.text, b.tone))]);
    const head = h('div', { class: 'mv-head' },
      h('strong', { text: `${item.monster || card.monster || card.title} · variant ${item.variant ?? ''}` }),
      importing() ? badge('picked, not imported', 'warn')
        : item.legacy ? badge('converted before views existed', '') : badge(item.frozen ? 'frozen' : 'choose views', item.frozen ? 'ok' : 'info'),
      item.unattended ? badge('unattended', 'warn') : null,
      item.library_hint && !(card.badges || []).some(b => b.text === item.library_hint) ? badge(item.library_hint, 'warn') : null);
    const damage = damaged() ? h('div', { class: 'unit-error', dataset: { note: 'damaged' }, text: damageText(item) }) : null;
    const moved = item.stage && item.stage !== 'approved'
      ? h('div', { class: 'held-block' }, h('span', { text: `This variant is now in ${pretty(item.stage)}.` }), button(`Open in ${pretty(item.stage)}`, () => host.openIn?.(item.stage), 'primary'))
      : null;
    const grid = h('div', { class: 'mv-views' }, NAMES.map(name => { syncFigure(name); return figs.get(name).node; }));
    root.replaceChildren(...[head, damage, moved, views().length ? grid : h('p', { class: 'mv-note', text: 'No views recorded for this variant.' }), frozenBlock(), conversionBlock()].filter(Boolean));
    syncAll();
  }

  function active() {
    return views().some(generating) || ['queued', 'running', 'waiting'].includes(item?.conversion?.state);
  }

  function schedule() {
    clearTimeout(timer);
    timer = setTimeout(async () => { if (!document.hidden && host.isCurrent()) await load(); schedule(); }, active() ? POLL_ACTIVE_MS : POLL_IDLE_MS);
  }

  async function load() {
    if (pendingWrites || converting) return;          // do not redraw under a tick that is being saved / a conversion request on the wire
    const started = epoch;
    try {
      const next = await api.item(card.id);
      if (!host.isCurrent() || started !== epoch || pendingWrites || converting) return;   // a tick or convert happened meanwhile: this copy is stale
      item = next;
      for (const key of Object.keys(local)) delete local[key];      // the item carries the saved ticks now
      draw();
    } catch (error) {
      if (host.isCurrent() && !item) root.replaceChildren(h('p', { text: `Details not available: ${error.message}` }));
    }
  }

  modal.setTeardown(() => clearTimeout(timer));
  await load();
  if (host.isCurrent()) schedule();
}
