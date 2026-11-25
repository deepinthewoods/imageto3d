// Approved modal (claimed sets): per race and module the four views at a readable size (click = full size),
// the unit's status and error, and Drop race / Undrop race (set verbs that write pipeline_v2.json with the rev seen).
// Phase 4: each unit shows its `progress` ({views_done, views_total, held_reason, converting_step, error}) and, when units are held because the
// set is not in the convert list, a Release button (POST /api/v2/queue {op: 'release', set}: appends the set to convert_sets.txt).
import { api } from './api.js';
import { raceVerb, verbClass, verbLabel } from './actions.js';
import { refresh } from './state.js';
import { showImage } from './modal.js';
import { toast } from './toast.js';
import { badge, blockRepeat, button, h, pretty } from './util.js';

const VIEW_ORDER = ['front', 'front_three_quarter', 'back', 'back_three_quarter'];
const TONE = { reduced: 'ok', converting: 'info', ready: 'info', views: 'info', held: 'warn', failed: 'error' };
const REFRESH_MS = 10000;

function groupByRace(units) {
  const races = new Map();
  for (const unit of units) {
    if (!races.has(unit.race)) races.set(unit.race, []);
    races.get(unit.race).push(unit);
  }
  return races;
}

function viewStrip(views) {
  const names = VIEW_ORDER.filter(name => views?.[name]);
  for (const name of Object.keys(views || {})) if (!names.includes(name)) names.push(name);
  if (!names.length) return h('div', { class: 'no-views', text: 'No views yet' });
  return h('div', { class: 'view-strip' }, names.map(name => h('figure', null,
    h('img', { src: views[name], alt: name, loading: 'lazy', title: 'Click for full size', onclick: () => showImage(views[name]) }),
    h('figcaption', { text: pretty(name) }))));
}

const NOT_LISTED = 'not in convert list';

/** The per-unit progress line: views a/b with a bar, the held reason, the converting step, the error. */
function progressLine(unit) {
  const p = unit.progress;
  if (!p || typeof p !== 'object') return null;
  const total = Number(p.views_total) || 0;
  const done = Math.min(Number(p.views_done) || 0, total);
  const nodes = [];
  if (total) {
    nodes.push(h('span', { class: 'prog-views', title: `${done} of ${total} views made` },
      `views ${done}/${total} `, h('progress', { max: total, value: done })));
  }
  if (p.held_reason) nodes.push(badge(`held: ${p.held_reason}`, 'warn'));
  if (p.converting_step) nodes.push(badge(`converting: ${p.converting_step}`, 'info'));
  const errorText = p.error && p.error !== unit.error ? p.error : null;
  if (!nodes.length && !errorText) return null;
  return h('div', { class: 'unit-progress', dataset: { race: unit.race, module: unit.module } },
    ...nodes, errorText ? h('div', { class: 'unit-error', text: errorText }) : null);
}

function unitRow(unit) {
  const head = h('div', { class: 'unit-head' }, h('strong', { text: pretty(unit.module) }),
    badge(unit.status, TONE[unit.status] || ''));
  if (unit.folder) head.append(h('span', { class: 'unit-folder', text: unit.folder }));
  return h('div', { class: 'unit' }, head,
    progressLine(unit),
    unit.error ? h('div', { class: 'unit-error', text: unit.error }) : null,
    viewStrip(unit.views));
}

export async function showApproved(modal, host) {
  const { card } = host;
  const box = h('div', { class: 'approved-view' }, h('p', { text: 'Loading...' }));
  blockRepeat(box);                 // a held Enter never repeats Release / Drop race
  modal.body.append(box);
  let item = null;

  for (const verb of card.actions || []) {
    modal.actions.append(button(verbLabel(verb), () => host.actAndAdvance(card, verb), verbClass(verb)));
  }
  modal.actions.append(button('Previous', () => host.go(-1)), button('Next', () => host.go(1)));

  async function dropVerb(race, verb) {
    await raceVerb(item, race, verb, { onDone: async () => { refresh().catch(() => {}); await load(); } });
  }

  let releasing = false;
  /** Add the set to convert_sets.txt so the convert worker takes its jobs. */
  async function releaseSet() {
    if (releasing) return;
    releasing = true;
    draw();
    const set = String(item?.name || card.name || '').split('--')[0];
    try {
      const result = await api.queuePost({ op: 'release', set });
      const excluded = Object.keys(result?.excluded || {}).length;
      const added = (result?.added || []).length;
      toast(excluded ? `${pretty(set)} is added, but a ! line in convert_sets.txt still excludes it`
        : added ? `Released ${pretty(set)}: it will be converted` : `${pretty(set)} was already in the convert list`, { ms: 5000 });
    } catch (error) {
      toast(`Release failed: ${error.message}`);
    } finally {
      releasing = false;
    }
    refresh().catch(() => {});
    await load();
  }

  /** The "held, not in the convert list" notice with Release (units whose conversion nobody will start). */
  function heldBlocks() {
    const live = (item.units || []).filter(unit => unit.status !== 'dropped');
    const unlisted = live.filter(unit => unit.progress?.held_reason === NOT_LISTED);
    const excluded = live.filter(unit => unit.progress?.held_reason === 'excluded');
    const nodes = [];
    if (unlisted.length) {
      const release = button(releasing ? 'Releasing...' : 'Release', releaseSet, 'primary', 'Add the set to the convert list (convert_sets.txt)');
      release.disabled = releasing;
      release.dataset.action = 'release';
      nodes.push(h('div', { class: 'held-block', dataset: { held: 'unlisted' } },
        h('span', { text: `Held: this set is not in the convert list, so the conversion worker ignores ${unlisted.length} unit${unlisted.length === 1 ? '' : 's'}.` }),
        release));
    }
    if (excluded.length) {
      nodes.push(h('div', { class: 'held-block', dataset: { held: 'excluded' } },
        `Held: ${excluded.length} unit${excluded.length === 1 ? ' is' : 's are'} excluded by a ! line in convert_sets.txt. Release does not help: that line has to be removed by hand.`));
    }
    return nodes;
  }

  function draw() {
    const dropped = item.decisions?.dropped || {};
    const sections = [];
    for (const [race, units] of groupByRace(item.units || [])) {
      const isDropped = race in dropped;
      const head = h('div', { class: 'race-head' }, h('h3', { text: pretty(race) }));
      if (isDropped) head.append(badge('dropped', 'warn'));
      head.append(button(isDropped ? 'Undrop race' : 'Drop race',
        () => dropVerb(race, isDropped ? 'undrop_race' : 'drop_race'), isDropped ? 'primary' : '',
        isDropped ? 'Require this race again' : 'This set no longer needs this race'));
      sections.push(h('section', { class: 'race-block' + (isDropped ? ' dropped' : '') }, head, units.map(unitRow)));
    }
    const extra = heldBlocks();
    if (item.error) extra.push(h('div', { class: 'item-error', text: `Decisions unreadable, nothing can be changed: ${item.error}` }));
    if (item.not_converted?.length) {
      extra.push(h('p', { class: 'not-converted', text: 'Not converted: ' + item.not_converted.map(n => `${pretty(n.race)} (${n.reason})`).join(', ') }));
    }
    box.replaceChildren(...extra, ...(sections.length ? sections : [h('p', { text: 'No units yet.' })]));
  }

  async function load() {
    try {
      const next = await api.item(card.id);
      if (!host.isCurrent()) return;
      item = next;
      draw();
    } catch (error) {
      if (host.isCurrent() && !item) box.replaceChildren(h('p', { text: `Details not available: ${error.message}` }));
    }
  }

  await load();
  const timer = setInterval(() => { if (!document.hidden) load(); }, REFRESH_MS);
  modal.setTeardown(() => clearInterval(timer));
  if (!host.isCurrent()) clearInterval(timer);
}
