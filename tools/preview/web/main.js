// Entry point: header, tabs, grid, filters, and the router that opens the right modal view for a card.
//   #<tab>             selects a tab;  #<tab>/<card id>  opens that card's modal (back button closes it).
import { onConflict } from './api.js';
import { state, refresh, startPolling, subscribe } from './state.js';
import { TAB_NAMES, isPlaceholder, PLACEHOLDERS, renderTabs, GROUPS } from './tabs.js';
import { applyFilters, applySize, mountFilters, stepSize } from './filters.js';
import { renderGrid } from './cards.js';
import { closeModal, initModal, isModalOpen, openModal } from './modal.js';
import { runAction } from './actions.js';
import { toast } from './toast.js';
import { $, badge, pretty, shortcutKey, storage } from './util.js';
import { showConcept } from './concept.js';
import { showApproved } from './approved.js';
import { showModel3d } from './model3d.js';
import { showMonster3d } from './monster3d.js';
import { showReview } from './review.js';

const TABS = GROUPS.flat().map(([key]) => key);
const MODEL_TABS = ['3d', 'aligned', 'completed'];
const BASE_MODES = ['under', 'over', 'off'];

let active = 'concepts';
let token = 0;               // identifies the modal view currently shown
let shownId = null;          // id of the card in the modal
let lastIndex = 0;           // its position, for when it leaves the list
let pushed = false;          // a history entry belongs to the open modal
let replaceNext = false;     // the next openCard reuses the current history entry (deep link on load)
let filterUi = null;

const isMonster = card => card.type === 'monster' || card.kind === 'monster';
const visible = tab => applyFilters(state.tabs[tab] || []);

// ---- base toggle (concept fronts over the race base) -------------------------------------------------------------
let baseMode = storage.get('baseMode', 'under');
function applyBase() {
  if (!BASE_MODES.includes(baseMode)) baseMode = 'under';
  document.body.dataset.base = baseMode;
  $('base-toggle').textContent = 'Base: ' + baseMode;
  storage.set('baseMode', baseMode);
}
function cycleBase() {
  baseMode = BASE_MODES[(BASE_MODES.indexOf(baseMode) + 1) % BASE_MODES.length];
  applyBase();
}

// ---- tab bar and grid ---------------------------------------------------------------------------------------------
function statusLine() {
  const parts = [];
  if (state.error) parts.push(`Server not reachable: ${state.error}`);
  if (state.credits != null) {
    parts.push(typeof state.credits === 'object' ? `Credits: ${JSON.stringify(state.credits)}` : `Credits: ${state.credits}`);
  }
  if (state.paused) parts.push('Paused');
  const banner = $('banner');
  const notes = state.server.notes || [];
  banner.hidden = !(state.server.read_only || notes.length);
  banner.textContent = state.server.read_only ? (state.server.message || 'Read-only: this server cannot change anything')
    : notes.length ? `Recovered at start: ${notes.join('; ')}` : '';
  banner.classList.toggle('read-only', !!state.server.read_only);
  $('status-line').textContent = parts.join(' · ') || 'Concept and model review';
  $('status-line').classList.toggle('error', !!state.error);
}

function render() {
  const counts = Object.fromEntries(TABS.filter(t => !isPlaceholder(t)).map(t => [t, visible(t).length]));
  renderTabs(active, counts, selectTab);
  filterUi?.setRaces(state.races);
  statusLine();
  const grid = $('grid');
  const placeholder = $('placeholder');
  const empty = $('empty');
  if (isPlaceholder(active)) {
    grid.hidden = true;
    empty.hidden = true;
    placeholder.hidden = false;
    placeholder.textContent = PLACEHOLDERS[active];
    return;
  }
  placeholder.hidden = true;
  grid.hidden = false;
  const cards = visible(active);
  const total = (state.tabs[active] || []).length;
  empty.hidden = cards.length > 0 || !state.loaded;
  empty.textContent = total ? `No ${TAB_NAMES[active]} cards match the filters (${total} hidden).`
    : `No ${TAB_NAMES[active]} cards.`;
  renderGrid(grid, cards, active, { onOpen: card => openCard(card), onAction: (card, verb) => runAction(card, verb, { tab: active }) });
  applySize();
}

function selectTab(tab) {
  active = tab;
  storage.set('tab', tab);
  if (isModalOpen()) closeModal({ silent: true });
  history.replaceState(null, '', '#' + tab);
  render();
}

// ---- modal router -------------------------------------------------------------------------------------------------
function viewFor(tab, card) {
  if (tab === 'review') return { show: showReview, info: false };
  if (MODEL_TABS.includes(tab)) {
    if (isMonster(card)) return { show: showMonster3d, info: false };
    if (card.id.startsWith('set:')) return { show: showModel3d, info: false };
  }
  if (tab === 'approved' && card.id.startsWith('set:')) return { show: showApproved, info: false };
  return { show: showConcept, info: true };
}

function neighbor(id, d) {
  const list = visible(active);
  const index = list.findIndex(card => card.id === id);
  const from = index >= 0 ? index : Math.min(lastIndex, list.length) - (d > 0 ? 1 : 0);
  return list[from + d] ?? null;
}

function updateHash(card) {
  const hash = `#${active}/${encodeURIComponent(card.id)}`;
  if (!pushed && !replaceNext) { history.pushState({ modal: true }, '', hash); pushed = true; }
  else history.replaceState({ modal: true }, '', hash);
  replaceNext = false;
}

function openCard(card) {
  const mine = ++token;
  const tab = active;
  shownId = card.id;
  const list = visible(tab);
  const index = list.findIndex(c => c.id === card.id);
  lastIndex = Math.max(index, 0);
  const view = viewFor(tab, card);
  const go = d => { const next = neighbor(card.id, d); if (next) openCard(next); };
  const modal = openModal({
    title: card.title || pretty(card.name),
    badges: [badge(isMonster(card) ? 'Monster' : pretty(card.kind || ''), isMonster(card) ? 'info' : ''),
      ...(card.badges || []).map(b => badge(b.text, b.tone))],
    info: view.info,
    onPrev: () => go(-1),
    onNext: () => go(1),
    onDismiss: dismissed,
  });
  modal.setNav({ index, total: list.length, hasPrev: index > 0, hasNext: index >= 0 && index < list.length - 1 });
  updateHash(card);
  const host = {
    tab,
    card,
    modal,
    isCurrent: () => token === mine && isModalOpen(),
    go,
    /** Run a verb for the card in the modal and move on to the neighbour at once (the action runs in the background). */
    actAndAdvance(target, verb, options = {}) {
      const next = neighbor(target.id, 1) || neighbor(target.id, -1);
      runAction(target, verb, { tab, ...options });
      if (host.isCurrent()) { if (next) openCard(next); else closeModal(); }
    },
  };
  view.show(modal, host).catch(error => {
    console.error(error);
    if (host.isCurrent()) toast(`View failed: ${error.message}`);
  });
}

/** The user closed the modal (X, shade, Esc). */
function dismissed() {
  token++;
  shownId = null;
  if (pushed) { pushed = false; history.back(); } else history.replaceState(null, '', '#' + active);
}

/** Back / forward / edited hash: follow the URL (close the modal, switch tab, reopen a card). */
window.addEventListener('popstate', () => {
  if (isModalOpen()) { pushed = false; token++; closeModal({ silent: true }); }
  const hash = parseHash();
  if (hash.tab && hash.tab !== active) { active = hash.tab; render(); }
  const card = hash.id && (state.tabs[active] || []).find(c => c.id === hash.id);
  if (card) { pushed = true; openCard(card); }
});

function parseHash() {
  const [tab, ...rest] = decodeURIComponent(location.hash.slice(1)).split('/');
  return { tab: TABS.includes(tab) ? tab : null, id: rest.length ? rest.join('/') : null };
}

// ---- start ------------------------------------------------------------------------------------------------------
function init() {
  applyBase();
  initModal();
  filterUi = mountFilters($('filters'), render);
  $('size-down').onclick = () => stepSize(-1);
  $('size-up').onclick = () => stepSize(1);
  $('base-toggle').onclick = cycleBase;
  $('refresh').onclick = () => refresh().then(() => toast('Refreshed', { ms: 1500 })).catch(error => toast(error.message));
  document.addEventListener('keydown', event => {
    if (event.key.toLowerCase() === 'b' && shortcutKey(event)) cycleBase();
  });
  onConflict(error => {
    refresh().catch(() => {});
    // a stale rev / job just reloads; a refusal (approved, being claimed, unreadable decisions) says what is wrong
    toast(error.stale ? 'Changed elsewhere; reloaded' : error.message);
  });
  const hash = parseHash();
  active = hash.tab || storage.get('tab', 'concepts');
  if (!TABS.includes(active)) active = 'concepts';
  subscribe(render);
  render();
  refresh().then(() => {
    if (!hash.id) return;
    const card = (state.tabs[active] || []).find(c => c.id === hash.id);
    if (card) { replaceNext = true; openCard(card); }
  }).catch(error => toast(error.message)).finally(startPolling);
}

init();
