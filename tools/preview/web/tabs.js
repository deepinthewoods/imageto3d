// Tab bar: two groups, counts after filtering. Mix and Queue are full pages (no cards): main.js mounts mix.js / queue.js into #page.
import { h, $ } from './util.js';

export const GROUPS = [
  [['concepts', 'Concepts'], ['provisional', 'Provisional'], ['approved', 'Approved'],
    ['3d', '3D'], ['aligned', 'Aligned'], ['completed', 'Completed']],
  [['mix', 'Mix'], ['queue', 'Queue'], ['review', 'Review'], ['archive', 'Archive'], ['trash', 'Trash']],
];

export const TAB_NAMES = Object.fromEntries(GROUPS.flat());
export const PLACEHOLDERS = {};
export const isPlaceholder = tab => tab in PLACEHOLDERS;
/** Tabs that are a page of their own instead of a grid of cards. */
export const PAGES = ['mix', 'queue'];
export const isPage = tab => PAGES.includes(tab);

/** counts: tab -> number (placeholders have none). */
export function renderTabs(active, counts, onSelect) {
  const group = tabs => h('span', { class: 'tab-group' }, tabs.map(([key, label]) => {
    const button = h('button', {
      type: 'button',
      class: 'tab' + (key === active ? ' active' : ''),
      onclick: () => onSelect(key),
      dataset: { tab: key },
    }, label);
    if (counts[key] != null) button.append(h('span', { class: 'count', text: String(counts[key]) }));
    return button;
  }));
  $('tabs').replaceChildren(group(GROUPS[0]), h('span', { class: 'tab-sep' }), group(GROUPS[1]));
}
