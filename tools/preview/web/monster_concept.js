// Monster concept dialog (cards `mconcept:<Folder>/<codexN>`): the concept image large, the details from its prompt json (design, notes,
// the per-N intent, colour roles), and the stage's verbs. Same keys as the character concept dialog: Enter = approve (focus on the dialog
// itself, never on a button), P = provisional, Esc = reject (else close), Space = next, arrows = previous / next.
// Approving a monster ALWAYS asks first: it says which variant number the concept gets and offers the "Unattended" tick.
import { api } from './api.js';
import { confirmDialog } from './confirm.js';
import { mutationKey, onDialogBody, showImage } from './modal.js';
import { runAction, verbClass, verbLabel } from './actions.js';
import { badge, button, h, picture, pretty } from './util.js';

const hasText = value => typeof value === 'string' && value.trim() !== '';

/** The verbs of the dialog: the card's own, plus Archive for concepts and provisional (like the character dialog). */
function dialogVerbs(card, tab) {
  const verbs = [...(card.actions || [])];
  if ((tab === 'concepts' || tab === 'provisional') && !verbs.includes('archive')) verbs.push('archive');
  return verbs;
}

/** "variant 2" for a variant name (`main` too), or "the next free variant" when the server did not say. */
const variantText = name => (name == null || name === '' ? 'the next free variant' : `variant ${name}`);

/**
 * The Approve confirmation for a monster concept. Resolves {unattended} when confirmed, null when cancelled.
 * `item` (when loaded) wins over the card for next_variant.
 */
export async function confirmMonsterApprove(card, item) {
  const monster = item?.monster || card.monster || card.title;
  const concept = item?.concept || String(card.id).split('/').pop();
  const next = item?.next_variant ?? card.next_variant;
  const tick = h('input', { type: 'checkbox', id: 'mc-unattended' });
  const content = h('label', { class: 'mc-unattended', title: 'Skip the "Choose views" step: when the three extra views are made, the conversion starts with every view that exists' },
    tick, h('span', null, h('b', { text: 'Unattended: convert straight away with all views' }),
      h('small', { text: 'Off: the card stops at "Choose views" and nothing is spent until you press Use enabled views.' })));
  const ok = await confirmDialog({
    title: `Approve ${monster} ${concept}?`,
    lines: [`It becomes ${variantText(next)} of ${monster}. Its front is copied into the pipeline and the three extra views (left, back, right) are drawn.`],
    content,
    confirm: 'Approve',
  });
  return ok ? { unattended: tick.checked } : null;
}

/**
 * Approve a monster concept (button, key or hover button): confirm, then post `approve` with args.unattended.
 * `host` (the dialog's) advances to the neighbour; without it (hover button) the card just leaves its tab. Resolves the response or null.
 */
export async function approveMonsterConcept(card, { host = null, item = null, tab = host?.tab } = {}) {
  const answer = await confirmMonsterApprove(card, item);
  if (!answer) return null;
  const next = item?.next_variant ?? card.next_variant;
  // the variant the server assigned wins (another approve may have taken `next` meanwhile)
  const subject = result => `${card.title}${(result?.variant ?? next) != null ? ` as variant ${result?.variant ?? next}` : ''}`;
  const options = { args: { unattended: answer.unattended }, subject };
  if (host?.isCurrent?.()) return host.actAndAdvance(card, 'approve', options);
  return runAction(card, 'approve', { tab, ...options });
}

/** The Design paragraph: prompt.design, else "Design: ..." cut out of the full prompt text. */
export function designText(prompt) {
  if (hasText(prompt?.design)) return prompt.design.trim();
  const full = prompt?.prompt;
  if (!hasText(full)) return '';
  const at = full.search(/Design:\s*/);
  if (at < 0) return '';
  return full.slice(at).replace(/^Design:\s*/, '').split(/\n\s*\n/)[0].trim();
}

function intentLines(intent) {
  if (intent == null || intent === '') return [];
  if (typeof intent === 'string') return [intent];
  if (Array.isArray(intent)) return [intent.join(', ')];
  return Object.entries(intent).map(([key, value]) => `${pretty(key)}: ${Array.isArray(value) ? value.join(', ') : typeof value === 'object' && value ? JSON.stringify(value) : value}`);
}

const section = (title, ...children) => h('section', { class: 'mc-section' }, h('h3', { text: title }), ...children);

function detailNodes(item, card) {
  const prompt = item.prompt || {};
  const nodes = [];
  const design = designText(prompt);
  nodes.push(section('Design', design ? h('p', { class: 'mc-design', text: design }) : h('p', { class: 'mc-none', text: 'No design text recorded.' })));
  const notes = [].concat(prompt.notes ?? []).filter(hasText);
  if (notes.length) nodes.push(section('Notes', h('ul', { class: 'mc-notes' }, notes.map(text => h('li', { text })))));
  const intent = intentLines(item.intent);
  if (intent.length) nodes.push(section('Intent', h('div', { class: 'mc-intent' }, intent.map(text => h('p', { text })))));
  if (item.roles?.length) {
    const grid = h('div', { class: 'monster-roles' });
    for (const role of item.roles) {
      grid.append(h('span', { class: 'swatch', style: { background: role.hex || '#888' } }),
        h('div', null, h('b', { text: role.role }), h('span', { text: (role.parts || []).join(', ') })));
    }
    nodes.push(section('Colour coding', grid));
  }
  const size = Array.isArray(prompt.size) ? prompt.size.join(' × ') : null;
  const facts = [['Monster', item.monster || card.monster], ['Concept', item.concept], ['Variant it gets', item.variant ?? item.next_variant ?? card.next_variant],
    ['Colour variant', prompt.colour_variant], ['Date', prompt.date], ['Attempts', prompt.attempts], ['Size', size ? `${size} ${prompt.mode || ''}`.trim() : null],
    ['Job', prompt.job]].filter(([, value]) => value != null && value !== '');
  nodes.push(section('Facts', h('dl', { class: 'mc-facts' }, facts.flatMap(([key, value]) => [h('dt', { text: key }), h('dd', { text: String(value) })]))));
  return nodes;
}

export async function showMonsterConcept(modal, host) {
  const { card, tab } = host;
  const verbs = dialogVerbs(card, tab);
  let item = null;
  const image = h('div', { class: 'mc-image' });
  const details = h('div', { class: 'mc-details' }, h('p', { class: 'mc-none', text: 'Loading...' }));
  const layout = h('div', { class: 'mc-layout' }, image, details);
  modal.body.append(layout);
  const showPicture = url => {
    if (!url) { image.replaceChildren(h('div', { class: 'no-model', text: 'No image' })); return; }
    const img = picture(url, { lazy: false });
    img.title = 'Click for full size';
    img.onclick = () => showImage(url);
    image.replaceChildren(img);
  };
  showPicture(card.thumbnail);

  const approve = () => approveMonsterConcept(card, { host, item });
  const act = verb => (verb === 'approve' ? approve() : host.actAndAdvance(card, verb));
  for (const verb of verbs) modal.actions.append(button(verbLabel(verb), () => act(verb), verbClass(verb)));
  modal.actions.append(button('Previous', () => host.go(-1)), button('Skip', () => host.go(1)));

  modal.setKeyHandler(event => {
    const has = verb => verbs.includes(verb);
    const key = event.key.toLowerCase();
    const run = verb => mutationKey(event, () => act(verb));   // fresh press only: a held key never repeats the verb
    if (event.key === 'Enter' && has('approve') && onDialogBody(event)) return run('approve');
    if (key === 'p' && has('provisional')) return run('provisional');
    if (event.key === 'Escape' && has('reject')) return run('reject');
    if (event.key === ' ') { event.preventDefault(); if (!event.repeat) host.go(1); return true; }
    return false;
  });

  try {
    item = await api.item(card.id);
  } catch (error) {
    if (host.isCurrent()) details.replaceChildren(h('p', { class: 'mc-none', text: `Details not available: ${error.message}` }));
    return;
  }
  if (!host.isCurrent()) return;
  if (item.image && item.image !== card.thumbnail) showPicture(item.image);
  const hint = item.library_hint || card.library_hint;
  if (hint && !(card.badges || []).some(b => b.text === hint)) {
    modal.setBadges([badge('Monster', 'info'), ...(card.badges || []).map(b => badge(b.text, b.tone)), badge(hint, 'warn')]);
  }
  details.replaceChildren(...detailNodes(item, card));
}
