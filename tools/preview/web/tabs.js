// Tab bar: two groups, counts after filtering. Mix and Queue are placeholders until later phases.
import { h, $ } from './util.js';

export const GROUPS = [
  [['concepts', 'Concepts'], ['provisional', 'Provisional'], ['approved', 'Approved'],
    ['3d', '3D'], ['aligned', 'Aligned'], ['completed', 'Completed']],
  [['mix', 'Mix'], ['queue', 'Queue'], ['review', 'Review'], ['archive', 'Archive'], ['trash', 'Trash']],
];

export const TAB_NAMES = Object.fromEntries(GROUPS.flat());
export const PLACEHOLDERS = {
  mix: 'Mix (free try-on of any built layers) is coming in a later phase.',
  queue: 'Queue (running, waiting and failed work) is coming in a later phase.',
};
export const isPlaceholder = tab => tab in PLACEHOLDERS;

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
