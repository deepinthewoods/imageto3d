// Review tab modal: a read-only viewer over the entries an agent put in a review card (e.g. "now" against "before").
// DESIGN.md does not fix the item shape, so this accepts what the old review.json cards had and the v2 unit shape:
//   item.entries[]: {label? | race, module, job, lods?: {lv: {url, faces}}, model?: url,
//     variants?: {name: url | {lv: url|{url}}}  or the old review.json form {lv: {name: url}}}
//   or just card.model for a single model.
import { api } from './api.js';
import { createViewer } from './viewer.js';
import { badge, faceText, field, fillSelect, h, lodLabel, pretty, storage, toggle } from './util.js';

const NONE = '-';
const LOD_KEY = /^\d+p\d+$/;

/** entry -> Map(variant name -> Map(lod label -> {url, faces})) */
function variantsOf(entry) {
  const out = new Map();
  const put = (name, lod, value) => {
    const url = typeof value === 'string' ? value : value?.url;
    if (!url) return;
    if (!out.has(name)) out.set(name, new Map());
    out.get(name).set(lod, { url, faces: typeof value === 'object' ? value.faces : undefined });
  };
  for (const [lod, value] of Object.entries(entry.lods || {})) put('full labelled', lod, value);
  for (const [key, spec] of Object.entries(entry.variants || {})) {
    if (typeof spec === 'string') put(key, NONE, spec);
    else if (LOD_KEY.test(key) && spec && typeof spec === 'object') {
      // old review.json shape: variants[lod][variant name] = url
      for (const [name, value] of Object.entries(spec)) put(name, key, value);
    } else for (const [lod, value] of Object.entries(spec || {})) put(key, lod, value);
  }
  if (entry.model) put('model', NONE, entry.model);
  return out;
}

const entryLabel = (entry, i) => entry.label
  || [entry.race, entry.module, entry.job].filter(Boolean).map(pretty).join(' · ') || `Entry ${i + 1}`;

export async function showReview(modal, host) {
  const { card } = host;
  let item = card;
  try { item = { ...card, ...(await api.item(card.id)) }; } catch { /* use the card */ }
  if (!host.isCurrent()) return;
  const entries = item.entries?.length ? item.entries : card.model ? [{ label: card.title || card.name, model: card.model }] : [];
  modal.setBadges([badge('review')]);
  modal.actions.classList.add('model-actions');
  const wrap = h('div', { class: 'viewer-wrap' });
  const message = h('div', { class: 'viewer-message', text: entries.length ? 'Loading...' : 'This review card lists no models' });
  wrap.append(message);
  modal.body.append(h('div', { class: 'viewer-shell' }, wrap));
  if (!entries.length) return;

  const viewer = createViewer(wrap);
  modal.setTeardown(() => viewer.dispose());
  const entrySelect = h('select', { class: 'entry-select', onchange: () => { fillVariants(); show(); } });
  const variantSelect = h('select', { class: 'variant-select', onchange: () => { fillLods(); show(); } });
  const lodSelect = h('select', { onchange: show });
  const facesText = h('span', { class: 'faces-text' });
  let wire = !!storage.get('wire', false);
  const wireToggle = toggle('Wire', wire, on => { wire = on; storage.set('wire', on); viewer.setWireframe(on); });
  modal.actions.append(field('Entry', entrySelect), field('Variant', variantSelect), field('LOD', lodSelect), facesText,
    wireToggle.label, h('span', { class: 'phase-hint', text: 'read-only' }));
  viewer.setWireframe(wire);

  const current = () => variantsOf(entries[+entrySelect.value] || {});
  function fillVariants() {
    fillSelect(variantSelect, [...current().keys()].map(name => [name, name]), variantSelect.value);
    fillLods();
  }
  function fillLods() {
    const lods = current().get(variantSelect.value);
    fillSelect(lodSelect, [...(lods?.keys() || [])].map(lod => [lod, lod === NONE ? 'single' : lodLabel(lod)]), lodSelect.value);
  }
  let shown = 0;
  async function show() {
    const mine = ++shown;
    const found = current().get(variantSelect.value)?.get(lodSelect.value);
    facesText.textContent = found?.faces != null ? faceText(found.faces) : '';
    if (!found) { viewer.clear(); message.hidden = false; message.textContent = 'No model for this selection'; return; }
    message.hidden = false;
    message.textContent = 'Loading...';
    try {
      const result = await viewer.load(found.url, { view: null });
      if (mine === shown && result.applied) message.hidden = true;
    } catch (error) {
      if (mine === shown) message.textContent = `Model not loaded: ${error.message}`;
    }
  }

  fillSelect(entrySelect, entries.map((entry, i) => [String(i), entryLabel(entry, i)]));
  fillVariants();
  show();
}
