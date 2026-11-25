// The card grid: thumbnail, title, kind tag (Monster is highlighted), badges, hover buttons from card.actions.
import { h, pretty, badge } from './util.js';
import { attachThumbnail, rendered } from './thumbs.js';
import { isModalOpen } from './modal.js';
import { verbClass, verbLabel } from './actions.js';

const TONE_OF_STATUS = {
  reduced: 'ok', converting: 'info', ready: 'info', views: 'info', held: 'warn', failed: 'error', missing: '', dropped: '',
};

/**
 * Approved tab. `progress` is {race: {module: X}} where X is either the unit's status text (older servers: "how many units are in each
 * state") or its progress record {views_done, views_total, held_reason, converting_step, error} (phase 4: "views 3/11", "converting 2/11",
 * "held", "not in convert list", "failed"). The per-race detail is the tooltip. A badge the server already put into `card.badges` is not
 * drawn twice.
 */
function progressBadges(progress, existing = []) {
  const counts = {};
  const lines = [];
  const sum = { done: 0, total: 0, converting: 0, held: 0, unlisted: 0, failed: 0, units: 0 };
  let record = false;
  for (const [race, modules] of Object.entries(progress || {})) {
    const parts = [];
    for (const [module, value] of Object.entries(modules || {})) {
      if (value && typeof value === 'object') {
        record = true;
        sum.units += 1;
        sum.done += Number(value.views_done) || 0;
        sum.total += Number(value.views_total) || 0;
        if (value.converting_step) sum.converting += 1;
        if (value.held_reason === 'not in convert list') sum.unlisted += 1;
        else if (value.held_reason) sum.held += 1;
        if (value.error) sum.failed += 1;
        const bits = [value.views_total ? `views ${value.views_done ?? 0}/${value.views_total}` : null,
          value.converting_step ? `converting ${value.converting_step}` : null, value.held_reason ? `held: ${value.held_reason}` : null,
          value.error ? `failed: ${value.error}` : null].filter(Boolean);
        parts.push(`${pretty(module)}: ${bits.join(', ') || value.status || 'ok'}`);
      } else {
        parts.push(`${pretty(module)}: ${value}`);
        counts[value] = (counts[value] || 0) + 1;
      }
    }
    lines.push(`${pretty(race)}: ${parts.join(', ')}`);
  }
  const make = (text, tone) => {
    const node = badge(text, tone);
    node.title = lines.join('\n');
    return node;
  };
  const out = Object.entries(counts).map(([status, n]) => make(`${status} ${n}`, TONE_OF_STATUS[status] ?? ''));
  if (record) {
    const have = kind => existing.some(b => String(b.text || '').toLowerCase().startsWith(kind));
    const count = (n, of) => `${n}${of ? `/${of}` : ''}`;
    if (sum.total && sum.done < sum.total && !have('views')) out.push(make(`views ${count(sum.done, sum.total)}`, 'info'));
    if (sum.converting && !have('converting')) out.push(make(`converting ${count(sum.converting, sum.units)}`, 'info'));
    if (sum.held && !have('held')) out.push(make(sum.held > 1 ? `held ${sum.held}` : 'held', 'warn'));
    if (sum.unlisted && !have('not in convert list')) out.push(make('not in convert list', 'warn'));
    if (sum.failed && !have('failed')) out.push(make(sum.failed > 1 ? `failed ${sum.failed}` : 'failed', 'error'));
  }
  return out;
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
    ...(tab === 'approved' ? progressBadges(card.progress, card.badges || []) : []));
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
