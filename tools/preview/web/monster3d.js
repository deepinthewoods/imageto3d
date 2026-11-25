// 3D modal for monster variants: viewer of the chosen reduced level (five levels, 0.8% to 0.1%), the conversion
// image and colour legend on the side. Changing the LOD saves it (POST /api/v2/lod with the monster id, which writes
// chosen_lod.json through monsters.write_choice). Approve arrives in a later phase.
import { api } from './api.js';
import { createViewer } from './viewer.js';
import { refresh } from './state.js';
import { toast } from './toast.js';
import { badge, button, faceText, field, fillSelect, h, lodLabel, picture, pretty, storage, toggle } from './util.js';
import { showImage } from './modal.js';
import { carry } from './model3d.js';

const LODS = ['0p8', '0p6', '0p4', '0p2', '0p1'];

function sidePanel(item) {
  const side = h('div', { class: 'monster-side' });
  for (const [title, url] of [['Conversion image', item.image], ['Source image', item.source_image]]) {
    if (!url) continue;
    const img = picture(url, { lazy: false });
    img.title = 'Click for full size';
    img.onclick = () => showImage(url);
    side.append(h('section', null, h('h3', { text: title }), img));
  }
  if (item.roles?.length) {
    const grid = h('div', { class: 'monster-roles' });
    for (const role of item.roles) {
      grid.append(h('span', { class: 'swatch', style: { background: role.hex || '#888' } }),
        h('div', null, h('b', { text: role.role }), h('span', { text: (role.parts || []).join(', ') })));
    }
    side.append(h('section', null, h('h3', { text: 'Colour coding' }), grid));
  }
  return side;
}

export async function showMonster3d(modal, host) {
  const { card } = host;
  let item = card;
  try { item = { ...card, ...(await api.item(card.id)) }; } catch { /* the card from the state is enough */ }
  if (!host.isCurrent()) return;

  const lods = item.lods || {};
  const levels = LODS.filter(level => lods[level]);
  let wire = !!storage.get('wire', false);
  let shown = 0;

  const wrap = h('div', { class: 'viewer-wrap' });
  const message = h('div', { class: 'viewer-message', hidden: true });
  wrap.append(message);
  const layout = h('div', { class: 'monster-layout' }, h('div', { class: 'viewer-shell' }, wrap));
  if (item.image || item.source_image || item.roles?.length) layout.append(sidePanel(item));
  modal.body.append(layout);
  modal.setBadges([badge('Monster', 'info'), badge(item.stage || card.stage || ''), ...(item.approved ? [badge('approved', 'ok')] : [])]);

  if (!levels.length) {
    message.hidden = false;
    message.textContent = 'No reduced model yet';
    modal.actions.classList.add('model-actions');
    return;
  }
  const viewer = createViewer(wrap);
  viewer.setWireframe(wire);
  modal.setTeardown(() => {
    carry.view = viewer.hasModel ? viewer.getView() : carry.view;
    viewer.dispose();
  });

  // an approved monster keeps its level: the selector then only switches the displayed mesh (the server refuses the write anyway)
  const locked = item.approved === true || (item.stage || card.stage) === 'completed';
  const lodSelect = h('select', { title: 'Level of detail', onchange: () => choose(lodSelect.value) });
  const facesText = h('span', { class: 'faces-text' });
  const saveButton = button('Save', () => choose(lodSelect.value, { alreadyShown: true }), 'primary', 'Record this level as the chosen one');
  const wireToggle = toggle('Wire', wire, on => { wire = on; storage.set('wire', on); viewer.setWireframe(on); });
  // LATER PHASE: the Approve button goes into this slot (a monster goes straight to Completed on approval).
  const approveSlot = h('span', { class: 'approve-slot', dataset: { slot: 'approve', phase: 'later' } });
  modal.actions.classList.add('model-actions');
  modal.actions.append(approveSlot, field('LOD', lodSelect), saveButton, facesText, wireToggle.label,
    ...(locked ? [h('span', { class: 'lod-note', text: 'LOD view only: approved', title: 'An approved model keeps its LOD. The selector only switches the displayed mesh.' })] : []));
  fillSelect(lodSelect, levels.map(level => [level, `${lodLabel(level)} · ${faceText(lods[level].faces).replace(' faces', '')}`]),
    item.lod && lods[item.lod] ? item.lod : (lods['0p4'] ? '0p4' : levels[0]));
  let saved = item.lod && lods[item.lod] ? item.lod : null;

  function sync() {
    const faces = lods[lodSelect.value]?.faces;
    facesText.textContent = [faces != null ? faceText(faces) : '', saved === lodSelect.value ? 'chosen' : 'not chosen yet']
      .filter(Boolean).join(' · ');
    facesText.classList.toggle('unsaved', saved !== lodSelect.value);
    saveButton.hidden = locked || saved === lodSelect.value;
    if (locked) { facesText.textContent = faces != null ? faceText(faces) : ''; facesText.classList.remove('unsaved'); }
  }

  async function show() {
    const mine = ++shown;
    sync();
    message.hidden = false;
    message.textContent = 'Loading...';
    try {
      const result = await viewer.load(lods[lodSelect.value].url, { view: viewer.hasModel ? null : carry.view });
      if (mine === shown && result.applied) message.hidden = true;
    } catch (error) {
      if (mine === shown) message.textContent = `Model not loaded: ${error.message}`;
    }
  }

  async function choose(level, { alreadyShown = false } = {}) {
    if (!alreadyShown) show();
    if (locked) return;       // view only: no write
    try {
      await api.lod({ id: card.id, lod: level });
      saved = level;
      toast(`LOD ${lodLabel(level)} chosen for ${card.title || pretty(card.name)}`);
      refresh().catch(() => {});
    } catch (error) {
      toast(`LOD not saved: ${error.message}`);
    }
    if (host.isCurrent()) sync();
  }

  show();
}
