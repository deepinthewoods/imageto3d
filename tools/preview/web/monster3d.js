// 3D modal for monster variants: viewer of the chosen reduced level (five levels, 0.8% to 0.1%), the conversion
// image and colour legend on the side. Changing the LOD saves it (POST /api/v2/lod with the monster id, which writes
// chosen_lod.json through monsters.write_choice).
// PHASE 5 (marked below): Approve (button + key A, confirmation) in 3D, Unapprove in Completed, Reconvert (paid, confirmation with the view
// ticks; sends args.confirm), and the PIPELINE_LIBRARY hint badge. Item fields: tools/preview/v2/DESIGN_PHASE5.md section H.
// Phase 5 review fixes: Approve sends the LOD AND the sha256 of the model as displayed ({lod, glb_sha256}; 409 `changed` = the model
// changed since the modal loaded it: it is loaded again); Reconvert sends the exact ticked files and the views revision the user saw
// ({files, views_rev, confirm}); a `damaged` reduction is read-only (the error, no buttons); a `legacy` one carries a badge.
// Recheck before cut-over (finding 10): Approve is enabled only for the level the viewer SUCCESSFULLY loaded and applied; the digest sent
// is the one bound to that loaded URL (`applied`). While a level loads, after a failed load (the stale mesh is cleared) and when the
// selector shows another level than the applied one, Approve is disabled.
// Final verification (row 10): the digest is bound to the BYTES ON SCREEN. The modal fetches the GLB itself (no HTTP cache), hashes those
// bytes (SubtleCrypto SHA-256) and hands exactly them to the viewer (a blob URL); Approve is enabled only when that hash equals the
// item's `lods[level].sha256` (hashed by the server from the file), and the hash the modal COMPUTED is what Approve sends. A model
// whose bytes are not the ones the item describes shows `model changed` and offers a reload; it can never be approved by metadata.
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

/** The bytes behind a model URL, fetched past every cache, and their SHA-256 (lower-case hex). */
async function fetchModel(url) {
  const response = await fetch(url, { cache: 'no-store' });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const bytes = await response.arrayBuffer();
  if (!globalThis.crypto?.subtle) throw new Error('this browser cannot hash the model (no SubtleCrypto)');
  const digest = new Uint8Array(await globalThis.crypto.subtle.digest('SHA-256', bytes));
  return { bytes, sha: Array.from(digest, byte => byte.toString(16).padStart(2, '0')).join('') };
}

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
  // what is ON SCREEN: {level, url, sha, expected} of the last load the viewer applied; null while loading and after a failed load.
  // `sha` is the SHA-256 this modal computed from the bytes it gave the viewer, `expected` the item's digest of that level. Approve
  // vouches for exactly these bytes, never for a level that was merely selected or for a digest read from metadata.
  let applied = null;
  let rereadOnce = false;            // a digest mismatch re-reads the item once, never in a loop
  let approveButton = null;

  /** Why Approve cannot be pressed now (null = it can). */
  function approveBlock() {
    if (!applied) return 'The model is not loaded';
    if (applied.level !== lodSelect.value) return 'The level on screen is not the selected one';
    if (!applied.sha) return 'This level has no digest; reload';
    if (!applied.expected) return 'The server gave no digest for this level; reload';
    if (applied.sha !== applied.expected) return 'The model on screen is not the file the server describes (it changed); reload it';
    return null;
  }

  function syncApprove() {
    if (!approveButton) return;
    const block = approveBlock();
    approveButton.disabled = !!block;
    approveButton.title = block || 'Approve this level (A): the monster moves to Completed';
    approveButton.dataset.loaded = applied ? applied.level : '';
    approveButton.dataset.sha = applied?.sha || '';            // the digest of the bytes on screen (what Approve sends)
  }

  function sync() {
    const faces = lods[lodSelect.value]?.faces;
    facesText.textContent = [faces != null ? faceText(faces) : '', saved === lodSelect.value ? 'chosen' : 'not chosen yet']
      .filter(Boolean).join(' · ');
    facesText.classList.toggle('unsaved', saved !== lodSelect.value);
    saveButton.hidden = locked || saved === lodSelect.value;
    if (locked) { facesText.textContent = faces != null ? faceText(faces) : ''; facesText.classList.remove('unsaved'); }
    syncApprove();
  }

  async function show() {
    const mine = ++shown;
    const level = lodSelect.value;
    // `sha` is filled from the bytes this modal fetches; `expected` is the server's digest of the level's file
    const target = { level, url: lods[level].url, sha: null, expected: lods[level]?.sha256 || null };
    applied = null;                  // nothing is vouched for while another level loads
    sync();
    message.hidden = false;
    message.dataset.state = 'loading';
    message.textContent = 'Loading...';
    try {
      const model = await fetchModel(target.url);          // the bytes are fetched and hashed HERE, then shown from memory
      if (mine !== shown) return;
      target.sha = model.sha;
      const blobUrl = URL.createObjectURL(new Blob([model.bytes], { type: 'model/gltf-binary' }));
      let result;
      try {
        result = await viewer.load(blobUrl, { view: viewer.hasModel ? null : carry.view });
      } finally {
        URL.revokeObjectURL(blobUrl);
      }
      if (mine !== shown) return;
      if (result.applied) {
        applied = target;
        const differs = !!target.expected && target.sha !== target.expected;
        message.hidden = !differs;
        message.dataset.state = differs ? 'changed' : '';
        if (differs) message.textContent = 'This model changed on disk after the page read it. Approve is off until the model on screen is the one the server describes.';
        if (differs && !rereadOnce) {      // read the item again ONCE (its digest is hashed from the file now) and load the level again
          rereadOnce = true;
          reloadModel();
          return;
        }
        if (!differs) rereadOnce = false;
      }
    } catch (error) {
      if (mine !== shown) return;
      applied = null;
      try { viewer.clear?.(); } catch { /* the message says it */ }      // never leave another level's mesh under this selection
      message.dataset.state = 'failed';
      message.textContent = `Model not loaded: ${error.message}. Approve is off until this level is on screen.`;
    }
    if (host.isCurrent()) sync();
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
    applied = null;
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
    const block = approveBlock();
    if (block) { toast(`Not approved: ${block}`); return; }
    asking = true;
    try {
      // the level AND the digest of the model the viewer applied, captured now: what the user confirms is what the server is asked to approve
      const { level, sha } = applied;
      const ok = await confirmDialog({
        title: `Approve ${name}?`,
        lines: [`LOD ${lodLabel(level)} (${faceText(lods[level].faces)}) becomes the approved level and ${name} moves to Completed.`,
          saved === level ? 'This level is already the chosen one.' : 'This level is saved as the chosen one by the same step.',
          'The level stays locked until you Unapprove it in Completed.'],
        confirm: 'Approve',
      });
      if (!ok || !host.isCurrent()) return;
      await host.actThenAdvance(card, 'approve', {
        args: { lod: level, glb_sha256: sha }, subject: `${name} (LOD ${lodLabel(level)})`, quiet: true,
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
    approveButton = button('Approve', askApprove, 'primary', 'Approve this level (A): the monster moves to Completed');
    approveButton.dataset.action = 'approve';
    approveSlot.append(approveButton);
    syncApprove();
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
