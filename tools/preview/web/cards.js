// The card grid: thumbnail, title, kind tag (Monster is highlighted), badges, hover buttons from card.actions.
import { h, pretty, badge } from './util.js';
import { attachThumbnail, rendered } from './thumbs.js';
import { isModalOpen } from './modal.js';
import { verbClass, verbLabel } from './actions.js';

const TONE_OF_STATUS = {
  reduced: 'ok', converting: 'info', ready: 'info', views: 'info', held: 'warn', failed: 'error', missing: '', dropped: '',
};

/** Approved tab: how many units are in each state, with the per-race detail as tooltip. */
function progressBadges(progress) {
  const counts = {};
  const lines = [];
  for (const [race, modules] of Object.entries(progress || {})) {
    const parts = Object.entries(modules).map(([module, status]) => `${pretty(module)}: ${status}`);
    lines.push(`${pretty(race)}: ${parts.join(', ')}`);
    for (const status of Object.values(modules)) counts[status] = (counts[status] || 0) + 1;
  }
  return Object.entries(counts).map(([status, n]) => {
    const node = badge(`${status} ${n}`, TONE_OF_STATUS[status] ?? '');
    node.title = lines.join('\n');
    return node;
  });
}

function thumbnail(card) {
  const thumbs = h('div', { class: 'thumbs' });
  const model = card.model;
  const fallback = card.thumbnail_kind === 'fallback' && !!model;     // concept / view art shown until the model's own render exists
  const made = model ? rendered.get(model) : null;
  const url = fallback ? (made || card.thumbnail) : (card.thumbnail || made || null);
  if (url) {
    const img = h('img', { src: url, alt: '', loading: 'lazy' });
    if (fallback && !made) attachThumbnail(img, model, { keepSrc: true });   // render the visible model once, then switch to it
    thumbs.append(img);
  } else if (model) {
    const img = h('img', { alt: '', class: 'pending' });
    attachThumbnail(img, model);
    thumbs.append(img);
  } else {
    thumbs.append(h('div', { class: 'no-model', text: 'No preview' }));
  }
  return thumbs;
}

function hoverButtons(card, tab, onAction) {
  const actions = card.actions || [];
  if (!actions.length) return null;
  return h('div', { class: 'card-actions' }, actions.map(verb => h('button', {
    type: 'button',
    class: verbClass(verb),
    text: verbLabel(verb),
    title: `${verbLabel(verb)} ${card.title || pretty(card.name)}`,
    onclick: event => { event.stopPropagation(); onAction(card, verb); },
    onkeydown: event => event.stopPropagation(),
  })));
}

export function makeCard(card, tab, { onOpen, onAction }) {
  const monster = card.type === 'monster' || card.kind === 'monster';
  const tags = h('div', { class: 'badges' },
    h('span', { class: 'tag' + (monster ? ' tag-monster' : ''), text: monster ? 'Monster' : pretty(card.kind || '') }),
    ...(card.badges || []).map(b => badge(b.text, b.tone)),
    ...(tab === 'approved' ? progressBadges(card.progress) : []));
  const node = h('div', { class: 'card', tabIndex: 0, dataset: { id: card.id },
    onclick: () => onOpen(card),
    onkeydown: event => {
      // Enter on the focused card only OPENS the modal: the event is consumed so the dialog's shortcuts never see it
      if (event.key === 'Enter' && event.target === node && !isModalOpen() && !event.ctrlKey && !event.altKey && !event.metaKey) {
        event.preventDefault();
        event.stopPropagation();
        onOpen(card);
      }
    } },
  thumbnail(card),
  h('div', { class: 'card-text' }, h('strong', { text: card.title || pretty(card.name) }), tags));
  const buttons = hoverButtons(card, tab, onAction);
  if (buttons) node.append(buttons);
  return node;
}

export function renderGrid(grid, cards, tab, handlers) {
  const scroll = grid.scrollTop;
  grid.replaceChildren(...cards.map(card => makeCard(card, tab, handlers)));
  grid.scrollTop = scroll;
}
