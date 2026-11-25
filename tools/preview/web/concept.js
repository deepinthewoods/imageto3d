// Concept modal (port of the old showConcept): the fronts of every race side by side over the race base
// (B cycles under / over / off), a details panel, and the stage's verbs.
// Keys: Enter = approve (only with focus on the dialog itself, never on a button), P = provisional, Esc = reject (else close),
// Space = next, arrows = previous / next. modal.js drops every shortcut whose target is a button / input / select / textarea.
import { api } from './api.js';
import { h, layered, picture, pretty, button } from './util.js';
import { mutationKey, onDialogBody } from './modal.js';
import { verbClass, verbLabel } from './actions.js';

/** Verbs offered in the dialog: the card's own, plus Archive for concepts (the dialog has room for it). */
function dialogVerbs(card, tab) {
  const verbs = [...(card.actions || [])];
  if (tab === 'concepts' && !verbs.includes('archive')) verbs.push('archive');
  return verbs;
}

function detailSections(races) {
  const out = [];
  for (const race of races) {
    for (const [module, meta] of Object.entries(race.info || {})) {
      const text = typeof meta === 'object' && meta
        ? Object.entries(meta).map(([k, v]) => `${pretty(k)}: ${typeof v === 'object' ? JSON.stringify(v) : v}`).join('\n')
        : String(meta ?? '');
      out.push(h('section', null, h('strong', { text: `${pretty(race.name)} · ${pretty(module)}` }),
        h('pre', { text: text })));
    }
  }
  return out;
}

function imageRow(races, fallback) {
  const row = h('div', { class: 'large-images' });
  let count = 0;
  for (const race of races) for (const url of Object.values(race.images || {})) {
    const box = layered(url, race.base, false);
    box.title = race.name;
    row.append(box);
    count++;
  }
  if (!count && fallback) row.append(picture(fallback, { lazy: false }));
  if (!count && !fallback) row.append(h('div', { class: 'no-model', text: 'No images' }));
  return row;
}

export async function showConcept(modal, host) {
  const { card, tab } = host;
  const verbs = dialogVerbs(card, tab);
  const row = h('div', { class: 'large-images' }, h('div', { class: 'no-model', text: 'Loading...' }));
  modal.body.append(row);
  modal.body.onclick = event => { if (event.target === modal.body || event.target === row) modal.close(); };

  const act = verb => host.actAndAdvance(card, verb);
  for (const verb of verbs) modal.actions.append(button(verbLabel(verb), () => act(verb), verbClass(verb)));
  modal.actions.append(button('Previous', () => host.go(-1)), button('Skip', () => host.go(1)));

  modal.setKeyHandler(event => {
    const has = verb => verbs.includes(verb);
    const key = event.key.toLowerCase();
    const run = verb => mutationKey(event, () => act(verb));   // fresh press only: a held key never repeats the verb
    if (event.key === 'Enter' && has('approve') && onDialogBody(event)) return run('approve');   // never while a button has focus
    if (key === 'p' && has('provisional')) return run('provisional');
    if (event.key === 'Escape' && has('reject')) return run('reject');
    if (event.key === ' ') { event.preventDefault(); if (!event.repeat) host.go(1); return true; }
    return false;
  });

  let item = null;
  try {
    item = await api.item(card.id);
  } catch (error) {
    if (!host.isCurrent()) return;
    row.replaceChildren(h('div', { class: 'no-model', text: `Details not available: ${error.message}` }));
    return;
  }
  if (!host.isCurrent()) return;
  const races = item?.detail?.races || item?.races?.filter?.(r => typeof r === 'object') || [];
  const fresh = imageRow(races, card.thumbnail);
  row.replaceChildren(...fresh.childNodes);
  modal.infoBody.replaceChildren(...detailSections(races));
  if (!modal.infoBody.childNodes.length) modal.infoBody.append(h('p', { text: 'No details recorded.' }));
}
