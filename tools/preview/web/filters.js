// Header filters (remembered in localStorage) and the grid size control. The filtered list of the active tab is
// also what prev/next in a modal walk through, and what the tab counts show.
import { h, pretty, storage, $ } from './util.js';

const DEFAULTS = { characters: true, monsters: true, race: '', kind: '', search: '' };
const KINDS = [['', 'All kinds'], ['outfit', 'Outfit'], ['armour', 'Armour'], ['monster', 'Monster'], ['legacy', 'Legacy']];
const GRID_MAX = 14;

export const filters = { ...DEFAULTS, ...(storage.get('filters') || {}) };
let gridCols = Math.min(GRID_MAX, Math.max(0, parseInt(storage.get('gridCols'), 10) || 0));
let onChange = () => {};

export function matches(card, f = filters) {
  const monster = card.type === 'monster' || card.kind === 'monster';
  if (monster ? !f.monsters : !f.characters) return false;
  if (f.kind && card.kind !== f.kind) return false;
  if (f.race && !monster && !(card.races || []).includes(f.race)) return false;
  const words = f.search.trim().toLowerCase().split(/\s+/).filter(Boolean);
  if (words.length) {
    const hay = [card.title, card.name, card.kind, card.stage, ...(card.races || [])].join(' ').toLowerCase()
      .replaceAll('_', ' ');
    if (!words.every(word => hay.includes(word.replaceAll('_', ' ')))) return false;
  }
  return true;
}

export const applyFilters = cards => (cards || []).filter(card => matches(card));

function save() { storage.set('filters', filters); onChange(); }

/** Build the filter row. `getRaces()` is read each time the race list is refreshed. */
export function mountFilters(container, changed) {
  onChange = changed;
  const checkbox = (key, label) => h('label', { class: 'check' },
    h('input', { type: 'checkbox', checked: filters[key], onchange: e => { filters[key] = e.target.checked; syncRace(); save(); } }),
    label);
  const raceSelect = h('select', { title: 'Race (characters only)', onchange: e => { filters.race = e.target.value; save(); } });
  const kindSelect = h('select', { title: 'Kind', onchange: e => { filters.kind = e.target.value; save(); } },
    KINDS.map(([value, label]) => h('option', { value, text: label })));
  const search = h('input', {
    type: 'search', placeholder: 'Search', value: filters.search, class: 'search',
    oninput: e => { filters.search = e.target.value; save(); },
  });
  kindSelect.value = filters.kind;
  const syncRace = () => { raceSelect.disabled = !filters.characters; };
  container.replaceChildren(checkbox('characters', 'Characters'), checkbox('monsters', 'Monsters'),
    raceSelect, kindSelect, search);
  syncRace();
  return {
    setRaces(races) {
      const keep = filters.race;
      // before the first state response the race list is empty: keep the stored race (offered as the only option) instead of clearing it
      const loaded = races.length > 0;
      const list = loaded || !keep ? races : [keep];
      raceSelect.replaceChildren(h('option', { value: '', text: 'All races' }),
        ...list.map(race => h('option', { value: race, text: pretty(race) })));
      raceSelect.value = list.includes(keep) ? keep : '';
      if (loaded && raceSelect.value !== keep) { filters.race = raceSelect.value; storage.set('filters', filters); }
    },
  };
}

/** Grid density: 0 = automatic (responsive CSS), otherwise a fixed number of columns. */
export function applySize() {
  const grid = $('grid');
  if (gridCols) {
    grid.style.gridTemplateColumns = `repeat(${gridCols},minmax(0,1fr))`;
    grid.style.gap = gridCols >= 8 ? '.5rem' : '1rem';
  } else {
    grid.style.gridTemplateColumns = '';
    grid.style.gap = '';
  }
  $('size-count').textContent = gridCols || 'auto';
  storage.set('gridCols', gridCols);
}

export function stepSize(delta) {
  gridCols = Math.min(GRID_MAX, Math.max(0, gridCols + delta));
  applySize();
}
