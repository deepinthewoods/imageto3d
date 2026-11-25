// 3D modal for monster variants: viewer of the chosen reduced level (five levels, 0.8% to 0.1%), the conversion
// image and colour legend on the side. Changing the LOD saves it (POST /api/v2/lod with the monster id, which writes
// chosen_lod.json through monsters.write_choice).
// PHASE 5 (marked below): Approve (button + key A, confirmation) in 3D, Unapprove in Completed, Reconvert (paid, confirmation with the view
// ticks; sends args.confirm), and the PIPELINE_LIBRARY hint badge. Item fields: tools/preview/v2/DESIGN_PHASE5.md section H.
// Phase 5 review fixes: Approve sends the LOD AND the sha256 of the model as displayed ({lod, glb_sha256}; 409 `changed` = the model
// changed since the modal loaded it: it is loaded again); Reconvert sends the exact ticked files and the views revision the user saw
// ({files, views_rev, confirm}); a `damaged` reduction is read-only (the error, no buttons); a `legacy` one carries a badge.
import { api } from './api.js';
import { createViewer } from './viewer.js';
import { refresh } from './state.js';
import { toast } from './toast.js';
import { conflictMessage, damageText, isDamaged, viewFile } from './monster_common.js';
import { badge, button, faceText, field, fillSelect, h, lodLabel, picture, pretty, storage, toggle } from './util.js';
import { mutationKey, showImage } from './modal.js';
import { confirmDialog } from './confirm.js';   // PHASE 5
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

  let lods = item.lods || {};
  let levels = LODS.filter(level => lods[level]);
  const damaged = isDamaged(item);
  let wire = !!storage.get('wire', false);
  let shown = 0;

  const wrap = h('div', { class: 'viewer-wrap' });
  const message = h('div', { class: 'viewer-message', hidden: true });
  wrap.append(message);
  const layout = h('div', { class: 'monster-layout' }, h('div', { class: 'viewer-shell' }, wrap));
  if (item.image || item.source_image || item.roles?.length) layout.append(sidePanel(item));
  modal.body.append(layout);
  const libraryHint = item.library_hint || card.library_hint;   // PHASE 5: "no library folder mapping (add to PIPELINE_LIBRARY)"
  const outdated = (item.card?.badges || card.badges || []).filter(b => b.text === 'sections out of date');   // PHASE 5: Reconvert kept sections/
  modal.setBadges([badge('Monster', 'info'), badge(item.stage || card.stage || ''), ...(item.approved ? [badge('approved', 'ok')] : []),
    ...(libraryHint ? [badge(libraryHint, 'warn')] : []), ...(item.legacy === true ? [badge('legacy reduction', 'warn')] : []),
    ...(damaged ? [badge('damaged', 'error')] : []), ...outdated.map(b => badge(b.text, b.tone))]);

  if (damaged || !levels.length) {
    message.hidden = false;
    message.dataset.state = damaged ? 'damaged' : 'empty';
    message.textContent = damaged ? damageText(item) : 'No reduced model yet';     // a damaged variant: the error only, no action buttons
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
  // PHASE 5: Approve (3D) / Unapprove (Completed) / Reconvert live in this slot; a monster goes straight to Completed on approval.
  const approveSlot = h('span', { class: 'approve-slot', dataset: { slot: 'approve' } });
  modal.actions.classList.add('model-actions');
  modal.actions.append(approveSlot, field('LOD', lodSelect), saveButton, facesText, wireToggle.label,
    ...(locked ? [h('span', { class: 'lod-note', text: 'LOD view only: approved', title: 'An approved model keeps its LOD. The selector only switches the displayed mesh.' })] : []));
  fillSelect(lodSelect, levels.map(level => [level, `${lodLabel(level)} · ${faceText(lods[level].faces).replace(' faces', '')}`]),
    item.lod && lods[item.lod] ? item.lod : (lods['0p4'] ? '0p4' : levels[0]));
  let saved = item.lod && lods[item.lod] ? item.lod : null;
  const shownSha = {};              // level -> the sha256 of the model this modal asked the viewer to show (what Approve vouches for)

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
    shownSha[lodSelect.value] = lods[lodSelect.value]?.sha256 || null;
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

  /** The model changed under this modal (409 `changed`): the item is read again and the shown level is loaded again. */
  async function reloadModel() {
    try {
      item = { ...card, ...(await api.item(card.id)) };
    } catch (error) {
      toast(`Not reloaded: ${error.message}`);
      return;
    }
    if (!host.isCurrent()) return;
    lods = item.lods || {};
    levels = LODS.filter(level => lods[level]);
    if (!levels.length) { refresh().catch(() => {}); return; }
    const keep = lods[lodSelect.value] ? lodSelect.value : (item.lod && lods[item.lod] ? item.lod : levels[0]);
    fillSelect(lodSelect, levels.map(level => [level, `${lodLabel(level)} · ${faceText(lods[level].faces).replace(' faces', '')}`]), keep);
    saved = item.lod && lods[item.lod] ? item.lod : null;
    for (const key of Object.keys(shownSha)) delete shownSha[key];
    show();
    refresh().catch(() => {});
  }

  // ---- PHASE 5: Approve / Unapprove / Reconvert ------------------------------------------------------------------------
  const stage = item.stage || card.stage;
  const name = card.title || pretty(card.name);
  const canApprove = !locked && stage === '3d';
  let asking = false;

  async function askApprove() {
    if (!canApprove || asking || !host.isCurrent()) return;
    asking = true;
    try {
      // the level AND the digest of the model on screen, captured now: what the user confirms is what the server is asked to approve
      const level = lodSelect.value;
      const sha = shownSha[level] || lods[level]?.sha256 || null;
      const ok = await confirmDialog({
        title: `Approve ${name}?`,
        lines: [`LOD ${lodLabel(level)} (${faceText(lods[level].faces)}) becomes the approved level and ${name} moves to Completed.`,
          saved === level ? 'This level is already the chosen one.' : 'This level is saved as the chosen one by the same step.',
          'The level stays locked until you Unapprove it in Completed.'],
        confirm: 'Approve',
      });
      if (!ok || !host.isCurrent()) return;
      await host.actThenAdvance(card, 'approve', {
        args: { lod: level, ...(sha ? { glb_sha256: sha } : {}) }, subject: `${name} (LOD ${lodLabel(level)})`, quiet: true,
        onError: error => {
          if (!error.conflict) return;
          toast(conflictMessage(error, 'model'), { ms: 6000 });
          if (error.code === 'changed') reloadModel(); else refresh().catch(() => {});
        },
      });
    } finally {
      asking = false;
    }
  }

  async function askReconvert() {
    if (asking || !host.isCurrent()) return;
    asking = true;
    try {
      const made = (item.views || []).filter(view => view.url);
      const viewsRev = item.views_rev;                    // the views revision the user is looking at
      const ticks = made.map(view => h('input', { type: 'checkbox', checked: view.enabled !== false, dataset: { view: view.name },
        onchange: () => listFiles() }));
      const filesLine = h('p', { class: 'mv-reconvert-files', dataset: { files: '' } });
      const ticked = () => made.filter((view, i) => ticks[i].checked).map(viewFile);
      function listFiles() { filesLine.textContent = `Files used: ${ticked().join(', ') || 'none'}`; }
      listFiles();
      const content = h('div', { class: 'mv-reconvert' }, made.map((view, i) => h('label', { class: 'mv-reconvert-view' },
        h('img', { src: view.url, alt: view.name }), h('span', null, ticks[i], pretty(view.name)))), filesLine);
      const ok = await confirmDialog({
        title: `Reconvert ${name}?`,
        lines: ['This is PAID: it starts a new Hyper3D conversion that costs 0.5 Hyper3D credits.',
          'The current Hyper3D result and reduced models are moved aside (kept as *_prev_<time>), not deleted. The variant goes back to Approved while it converts.',
          'Exactly the views ticked below are used:'],
        content,
        confirm: 'Reconvert (0.5 credits)',
        danger: true,
      });
      if (!ok || !host.isCurrent()) return;
      const files = ticked();                             // read the moment the user confirmed
      if (!files.length) { toast('Not reconverted: tick at least one view'); return; }
      await host.actThenAdvance(card, 'reconvert', {
        args: { files, views_rev: viewsRev, confirm: true }, subject: `${name} (reconvert)`, quiet: true,   // confirm: the server refuses a paid reconvert without it
        onError: error => {
          if (!error.conflict) return;
          toast(conflictMessage(error, 'views'), { ms: 6000 });
          refresh().catch(() => {});
        },
      });
    } finally {
      asking = false;
    }
  }

  if (canApprove) {
    const approve = button('Approve', askApprove, 'primary', 'Approve this level (A): the monster moves to Completed');
    approve.dataset.action = 'approve';
    approveSlot.append(approve);
  }
  if (stage === 'completed' || item.approved === true) {
    const unapprove = button('Unapprove', () => host.actThenAdvance(card, 'unapprove', { subject: name }), '', 'Clear the approval: the monster goes back to 3D');
    unapprove.dataset.action = 'unapprove';
    approveSlot.append(unapprove);
  }
  if ((item.views || []).some(view => view.url)) {
    const reconvert = button('Reconvert', askReconvert, '', item.approved === true ? 'Unapprove first: an approved model keeps its files' : 'Convert again (paid, 0.5 credits)');
    reconvert.dataset.action = 'reconvert';
    reconvert.disabled = item.approved === true;
    approveSlot.append(reconvert);
  }
  modal.setKeyHandler(event => (canApprove && event.key.toLowerCase() === 'a' ? mutationKey(event, askApprove) : false));

  show();
}
