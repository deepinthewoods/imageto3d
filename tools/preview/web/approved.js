// Approved modal (claimed sets): per race and module the four views at a readable size (click = full size),
// the unit's status and error, and Drop race / Undrop race (set verbs that write pipeline_v2.json with the rev seen).
import { api } from './api.js';
import { raceVerb, verbClass, verbLabel } from './actions.js';
import { refresh } from './state.js';
import { showImage } from './modal.js';
import { badge, button, h, pretty } from './util.js';

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

function unitRow(unit) {
  const head = h('div', { class: 'unit-head' }, h('strong', { text: pretty(unit.module) }),
    badge(unit.status, TONE[unit.status] || ''));
  if (unit.folder) head.append(h('span', { class: 'unit-folder', text: unit.folder }));
  return h('div', { class: 'unit' }, head,
    unit.error ? h('div', { class: 'unit-error', text: unit.error }) : null,
    viewStrip(unit.views));
}

export async function showApproved(modal, host) {
  const { card } = host;
  const box = h('div', { class: 'approved-view' }, h('p', { text: 'Loading...' }));
  modal.body.append(box);
  let item = null;

  for (const verb of card.actions || []) {
    modal.actions.append(button(verbLabel(verb), () => host.actAndAdvance(card, verb), verbClass(verb)));
  }
  modal.actions.append(button('Previous', () => host.go(-1)), button('Next', () => host.go(1)));

  async function dropVerb(race, verb) {
    await raceVerb(item, race, verb, { onDone: async () => { refresh().catch(() => {}); await load(); } });
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
    const extra = [];
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
