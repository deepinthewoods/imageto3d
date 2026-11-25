// 3D modal for character sets in the 3D tab: the unit's labelled LOD GLB plus the LOD choice, and Approve (key A) for the whole set.
// Bottom bar: Approve, race, module, LOD with face counts, All, wireframe. A small race x module grid shows the chosen LOD of every
// unit so an odd one stands out. Race, module, wireframe and camera carry over between sets.
//
// Approve opens a confirmation that lists the LOD and conversion of every race x module cell; confirming sends `approve` with
// args.units = {"race/module": {job, lod}} for EVERY cell shown (the server refuses with a 409 when a newer conversion or another LOD
// is current: the dialog then stays open, the item is reloaded and the changed cells are highlighted) and args.lods = the cells whose
// LOD was never saved (the server writes them with the approval). A held key never repeats the approval (modal.js mutationKey). After it the set stays in 3D while its layers are built: a
// progress bar "Building layers n/m", per-unit errors with Retry (`rebuild`), the item is polled every 2 s until the set moves on.
//
// LOD writes (DESIGN.md section 4): every write carries the set's `rev` and the `job` of each unit the user was looking at; a 409
// (stale rev, newer conversion, approved set) reloads the item and never retries. "All" returns the previous picks and the new rev;
// its Undo sends that map ONCE, with that rev, and stops on a conflict. Once the set is approved (Aligned / Completed, `item.locked`)
// the LOD selector is view-only: it changes the displayed mesh and posts nothing.
import { api } from './api.js';
import { createViewer } from './viewer.js';
import { raceVerb, undoAction } from './actions.js';
import { mutationKey } from './modal.js';
import { refresh } from './state.js';
import { toast } from './toast.js';
import { badge, button, faceText, field, fillSelect, h, lodLabel, pretty, storage, toggle } from './util.js';

const MODULE_SHORT = { body_armor: 'armour', right_gauntlet_boot: 'gloves+boots', outfit: 'outfit' };
const moduleShort = module => MODULE_SHORT[module] || pretty(module);
const hasLods = unit => !!unit?.lods && Object.keys(unit.lods).length > 0;
const lodKeys = unit => Object.keys(unit?.lods || {}).sort().reverse(); // 0p4, 0p2, 0p1 (and 0p8 ... for monsters)
const keyOf = unit => `${unit.race}/${unit.module}`;

/** What the next set opens with (race, module, camera); wireframe is kept in localStorage. */
export const carry = {
  race: storage.get('3d.race'),
  module: storage.get('3d.module'),
  view: null,
};

export async function showModel3d(modal, host) {
  const { card, tab } = host;
  let item = null;
  const sel = { race: null, module: null };
  let wire = !!storage.get('wire', false);
  let writes = Promise.resolve();
  let shown = 0; // newest show() call, for the stale guard on messages
  const viewLods = new Map(); // "race/module" -> level displayed while the picks are locked (never saved)

  // ---- viewer ---------------------------------------------------------------------------------------------
  const wrap = h('div', { class: 'viewer-wrap' });
  const message = h('div', { class: 'viewer-message', hidden: true });
  const gridBox = h('details', { class: 'lod-grid', open: !matchMedia('(max-width:650px)').matches });
  const notes = h('div', { class: 'viewer-notes' });
  const buildPanel = h('div', { class: 'build-panel', hidden: true });
  wrap.append(message);
  modal.body.append(h('div', { class: 'viewer-shell' }, wrap, buildPanel, gridBox, notes));
  const viewer = createViewer(wrap);
  viewer.setWireframe(wire);

  // ---- bottom bar -----------------------------------------------------------------------------------------
  const raceSelect = h('select', { class: 'race-select', title: 'Race', onchange: () => select(raceSelect.value, sel.module) });
  const moduleSelect = h('select', { title: 'Module', onchange: () => select(sel.race, moduleSelect.value) });
  const lodSelect = h('select', { title: 'Level of detail kept by the Blender reduction', onchange: () => setLod(lodSelect.value) });
  const facesText = h('span', { class: 'faces-text' });
  const moduleField = field('', moduleSelect);
  const allButton = button('All', () => setAll(lodSelect.value), 'lod-all', 'Use this LOD for every unit of this set');
  const wireToggle = toggle('Wire', wire, on => {
    wire = on;
    storage.set('wire', on);
    viewer.setWireframe(on);
  });
  const stepRace = d => {
    const races = raceList();
    const i = races.indexOf(sel.race) + d;
    if (i >= 0 && i < races.length) select(races[i], sel.module);
  };
  const prevRace = button('‹', () => stepRace(-1), '', 'Previous race');
  const nextRace = button('›', () => stepRace(1), '', 'Next race');
  const approveButton = button('Approve', () => askApprove(), 'primary', 'Approve the whole set at the chosen LODs (A)');
  const approveSlot = h('span', { class: 'approve-slot' }, approveButton);
  const lodNote = h('span', { class: 'lod-note', hidden: true, text: 'LOD view only: approved' });
  const backButton = button('Back to 3D', () => sendBack(), '', 'Clear the 3D approval so the LODs can be changed again');
  backButton.hidden = true;
  modal.actions.classList.add('model-actions');
  modal.actions.append(approveSlot, h('span', { class: 'entry-picker' }, prevRace, nextRace, raceSelect), moduleField,
    field('LOD', lodSelect), allButton, facesText, wireToggle.label, lodNote, backButton);

  // ---- data -----------------------------------------------------------------------------------------------
  const units = () => item?.units || [];
  const unitOf = (race, module) => units().find(u => u.race === race && u.module === module);
  const raceList = () => [...new Set(units().filter(hasLods).map(u => u.race))];
  const modulesOf = race => units().filter(u => u.race === race && hasLods(u)).map(u => u.module);
  const allModules = () => [...new Set(units().map(u => u.module))];
  const current = () => unitOf(sel.race, sel.module);
  /** Approved sets (Aligned / Completed): the persisted picks are frozen; the selector only changes what is displayed. */
  const isLocked = () => !!item?.locked || ['aligned', 'completed'].includes(item?.stage ?? tab);
  const shownLod = unit => (unit && isLocked() ? (viewLods.get(keyOf(unit)) ?? unit.lod) : unit?.lod);

  function pick() {
    const races = raceList();
    const race = races.includes(sel.race) ? sel.race : races.includes(carry.race) ? carry.race
      : races.includes('human_male') ? 'human_male' : races[0];
    const modules = modulesOf(race);
    const module = modules.includes(sel.module) ? sel.module : modules.includes(carry.module) ? carry.module : modules[0];
    sel.race = race ?? null;
    sel.module = module ?? null;
  }

  // ---- drawing --------------------------------------------------------------------------------------------
  function drawControls() {
    const races = raceList();
    const locked = isLocked();
    fillSelect(raceSelect, races.map(r => [r, pretty(r)]), sel.race);
    prevRace.disabled = races.indexOf(sel.race) <= 0;
    nextRace.disabled = races.indexOf(sel.race) >= races.length - 1;
    fillSelect(moduleSelect, modulesOf(sel.race).map(m => [m, pretty(m)]), sel.module);
    moduleField.hidden = allModules().length <= 1;
    const unit = current();
    fillSelect(lodSelect, lodKeys(unit).map(l => [l, `${lodLabel(l)} · ${faceText(unit.lods[l].faces).replace(' faces', '')}`]), shownLod(unit));
    lodSelect.disabled = !hasLods(unit);
    allButton.disabled = locked || !hasLods(unit);
    allButton.hidden = locked;
    lodNote.hidden = !locked;
    lodNote.title = 'The LOD of an approved set cannot change: use Back to 3D first. The selector only switches the displayed mesh.';
    backButton.hidden = !locked;
    const block = approveBlock();
    approveButton.hidden = locked;
    approveButton.disabled = !!block;
    approveButton.title = block || 'Approve the whole set at the chosen LODs (A)';
    const faces = unit?.lods?.[lodSelect.value]?.faces;
    facesText.textContent = faces != null ? faceText(faces) + (unit.lod_saved || locked ? '' : ' · not saved') : '';
    facesText.classList.toggle('unsaved', !!unit && !unit.lod_saved && !locked);
  }

  function drawGrid() {
    const races = [...new Set(units().map(u => u.race))];
    const modules = allModules();
    const counts = new Map();
    for (const u of units().filter(hasLods)) counts.set(u.lod, (counts.get(u.lod) || 0) + 1);
    const common = [...counts].sort((a, b) => b[1] - a[1])[0]?.[0];
    const head = h('tr', null, h('th'), modules.map(m => h('th', { text: moduleShort(m), title: pretty(m) })));
    const rows = races.map(race => h('tr', null, h('th', { text: pretty(race), class: 'race-name' }), modules.map(module => {
      const unit = unitOf(race, module);
      const ready = hasLods(unit);
      const cls = ['cell', !ready && 'missing', ready && unit.lod !== common && 'odd', ready && !unit.lod_saved && 'unsaved',
        race === sel.race && module === sel.module && 'sel'].filter(Boolean).join(' ');
      const text = ready ? lodLabel(unit.lod).replace('%', '') : unit ? unit.status.replace('_', ' ') : '';
      return h('td', { class: cls, text, title: `${pretty(race)} ${pretty(module)}${unit?.error ? ': ' + unit.error : ''}`,
        onclick: ready ? () => select(race, module) : null });
    })));
    gridBox.replaceChildren(h('summary', { text: 'LODs' }), h('table', null, h('thead', null, head), h('tbody', null, ...rows)));
  }

  /** Warnings the job record keeps for the shown unit (doubled shell, manual review ...), dropped / unconverted races. */
  function drawNotes() {
    const unit = current();
    const nodes = [];
    if (item?.error) nodes.push(h('div', { class: 'warn error', text: `Decisions unreadable, nothing can be changed: ${item.error}` }));
    for (const warning of unit?.warnings || []) nodes.push(h('div', { class: 'warn', text: `${pretty(unit.module)}: ${warning}` }));
    const levels = unit?.review_levels || [];
    if (levels.length) {
      const here = levels.includes(lodSelect.value);
      nodes.push(h('div', { class: 'warn' + (here ? ' error' : ''), text: `Needs review at ${levels.map(lodLabel).join(', ')}${here ? ' (this level)' : ''}` }));
    }
    for (const race of Object.keys(item?.decisions?.dropped || {})) {
      nodes.push(h('div', { class: 'drop-line' }, `Dropped: ${pretty(race)}`, button('Undrop', () => undrop(race), 'primary', 'Require this race again')));
    }
    if (item?.not_converted?.length) {
      nodes.push(h('div', { class: 'drop-line', text: 'Not converted: ' + item.not_converted.map(n => `${pretty(n.race)} (${n.reason})`).join(', ') }));
    }
    notes.replaceChildren(...nodes);
  }

  /** Why Approve is not possible, or null. The server checks the same (and answers 400 with the missing units). */
  function approveBlock() {
    if (item?.error) return `Decisions unreadable: ${item.error}`;
    if (!item) return 'Loading...';
    const live = units().filter(u => u.status !== 'dropped');
    if (!live.length) return 'No unit to approve';
    const bad = live.filter(u => u.status !== 'reduced' || !hasLods(u));
    if (bad.length) return `Not ready: ${bad.map(u => `${pretty(u.race)} ${moduleShort(u.module)} (${u.status})`).join(', ')}`;
    return null;
  }

  function setMessage(text) {
    message.hidden = !text;
    message.textContent = text || '';
  }

  function noModelMessage() {
    const stuck = units().find(u => u.status === 'pin_missing');
    if (stuck) return `${pretty(stuck.race)} · ${pretty(stuck.module)}: ${stuck.error}. Use Back to 3D to clear the approval.`;
    return 'No converted model in this set yet';
  }

  async function show() {
    const mine = ++shown;
    drawControls();
    drawGrid();
    drawNotes();
    const unit = current();
    const lod = lodSelect.value;
    const url = unit?.lods?.[lod]?.url;
    if (!url) {
      viewer.clear();
      setMessage(unit ? `${pretty(unit.race)} · ${pretty(unit.module)}: ${unit.status}${unit.error ? ' - ' + unit.error : ''}` : noModelMessage());
      return;
    }
    setMessage('Loading...');
    try {
      const result = await viewer.load(url, { view: viewer.hasModel ? null : carry.view });
      if (mine === shown && result.applied) setMessage(null);
    } catch (error) {
      if (mine === shown) setMessage(`Model not loaded: ${error.message}`);
    }
  }

  function select(race, module) {
    sel.race = race;
    sel.module = module;
    pick();
    carry.race = sel.race;
    carry.module = sel.module;
    storage.set('3d.race', sel.race);
    storage.set('3d.module', sel.module);
    show();
  }

  // ---- saving ---------------------------------------------------------------------------------------------
  const enqueue = fn => { writes = writes.catch(() => {}).then(fn); return writes; };

  /** The server bumps the set's rev on every LOD write and returns it. */
  function syncRev(result) {
    if (typeof result?.rev === 'number') item.rev = result.rev;
  }

  /** A refusal: reload the item (the global 409 handler already told the user why); anything else is reported here. */
  async function failed(error, what) {
    if (error.conflict) {
      await reload();
    } else {
      toast(`${what} not saved: ${error.message}`);
    }
  }

  /** Optimistic: the unit shows the new LOD at once; the write is queued so a quick second click cannot 409 the first. */
  async function saveUnit(unit, lod) {
    const before = { lod: unit.lod, saved: unit.lod_saved };
    unit.lod = lod;
    unit.lod_saved = true;
    try {
      await enqueue(async () => {
        const result = await api.lod({ id: item.id, race: unit.race, module: unit.module, lod, job: unit.job, rev: item.rev });
        syncRev(result);
      });
      return true;
    } catch (error) {
      unit.lod = before.lod;
      unit.lod_saved = before.saved;
      await failed(error, 'LOD');
      return false;
    }
  }

  async function setLod(lod) {
    const unit = current();
    if (!unit) return;
    if (isLocked()) { // approved: view only, nothing is posted
      viewLods.set(keyOf(unit), lod);
      show();
      return;
    }
    const saving = saveUnit(unit, lod); // sets unit.lod at once, so the viewer can switch before the write is done
    show();
    if (!(await saving) && host.isCurrent()) show(); // refused: back to the saved LOD
  }

  async function setAll(lod) {
    if (isLocked()) return;
    const targets = units().filter(hasLods);
    const lacking = targets.filter(u => !u.lods[lod]);
    if (lacking.length) {
      toast(`No ${lodLabel(lod)} level for ${lacking.map(u => `${pretty(u.race)} ${pretty(u.module)}`).join(', ')}`);
      return;
    }
    const jobs = Object.fromEntries(targets.map(u => [keyOf(u), u.job])); // the conversions the user is looking at
    const before = targets.map(unit => ({ unit, lod: unit.lod, saved: unit.lod_saved }));
    targets.forEach(unit => { unit.lod = lod; unit.lod_saved = true; });
    show();
    let result;
    try {
      result = await enqueue(async () => {
        const written = await api.lod({ id: item.id, lod, all: true, jobs, rev: item.rev });
        syncRev(written);
        return written;
      });
    } catch (error) {
      before.forEach(b => { b.unit.lod = b.lod; b.unit.lod_saved = b.saved; });
      await failed(error, 'LOD');
      if (host.isCurrent()) show();
      return;
    }
    toast(`LOD ${lodLabel(lod)} set on all ${targets.length} units`, { undo: () => undoAll(result, jobs) });
  }

  /**
   * Undo of "All": the server's `previous` map goes back in ONE request (null removes a pick that did not exist), at the rev "All"
   * returned. Anything written since gives a 409: nothing is undone, the item reloads and the user decides.
   */
  async function undoAll(result, jobs) {
    try {
      const undone = await enqueue(() => api.lod({ id: item.id, units: result.previous, jobs, rev: result.rev }));
      syncRev(undone);
      toast('Undone');
    } catch (error) {
      toast(error.conflict ? 'Undo not applied: the set changed since' : `Undo failed: ${error.message}`);
    }
    await reload();
  }

  async function reload() {
    try {
      item = await api.item(card.id);
      if (host.isCurrent()) { pick(); show(); drawBadges(); drawBuild(); watch(); }
    } catch (error) {
      toast(`Reload failed: ${error.message}`);
    }
  }

  // ---- decisions on the set ---------------------------------------------------------------------------------
  async function undrop(race) {
    await raceVerb(item, race, 'undrop_race', { onDone: async () => { refresh().catch(() => {}); await reload(); } });
  }

  async function sendBack() {
    try {
      const result = await api.action(item.id, 'unapprove', { rev: item.rev, args: { to: '3d' } });
      toast(`Sent back to 3D (now in ${result?.stage || '3D'})`);
    } catch (error) {
      if (!error.conflict) toast(`Back to 3D failed: ${error.message}`);
    }
    refresh().catch(() => {});
    await reload();
  }

  // ---- approval and layer builds ---------------------------------------------------------------------------
  let confirm = null;     // the open confirmation: {node, shown: {"race/module": {job, lod, saved}}, busy}
  let timer = null;

  const liveUnits = () => units().filter(u => u.status !== 'dropped' && hasLods(u));
  const buildOf = unit => item?.builds?.[keyOf(unit)];
  const built = unit => ['published', 'stale'].includes(buildOf(unit)?.status);
  const shortJob = job => String(job || '').slice(0, 8);

  function closeConfirm() {
    confirm?.node.remove();
    confirm = null;
  }

  /** What the confirmation shows (and what Approve sends back): the conversion (job) and LOD of every live unit. */
  const shownUnits = () => Object.fromEntries(liveUnits().map(u => [keyOf(u), { job: u.job, lod: u.lod, saved: !!u.lod_saved }]));

  /** Differences between two shown maps: [{key, text}] (a unit's LOD, its conversion, a unit that appeared or went). */
  function diffShown(before, now) {
    const out = [];
    for (const key of new Set([...Object.keys(before), ...Object.keys(now)])) {
      const a = before[key];
      const b = now[key];
      if (!a || !b) { out.push({ key, text: `${key}: ${a ? 'no longer a unit of this set' : 'new unit'}` }); continue; }
      const parts = [];
      if (a.lod !== b.lod) parts.push(`LOD ${lodLabel(a.lod)} -> ${lodLabel(b.lod)}`);
      if (a.job !== b.job) parts.push(`conversion ${shortJob(a.job)} -> ${shortJob(b.job)}`);
      if (parts.length) out.push({ key, text: `${key}: ${parts.join(', ')}` });
    }
    return out;
  }

  /**
   * (Re)draw the confirmation from the units as they are NOW: the LOD and conversion of every race x module cell, the cells that
   * differ from what the reviewer saw before highlighted, and a fresh confirm required (the same dialog stays open after a 409).
   */
  function renderConfirm(changes = []) {
    const live = liveUnits();
    const races = [...new Set(live.map(u => u.race))];
    const modules = allModules();
    const unsaved = live.filter(u => !u.lod_saved);
    const changed = new Set(changes.map(c => c.key));
    const head = h('tr', null, h('th'), modules.map(m => h('th', { text: moduleShort(m), title: pretty(m) })));
    const rows = races.map(race => h('tr', null, h('th', { class: 'race-name', text: pretty(race) }), modules.map(module => {
      const unit = unitOf(race, module);
      const ready = unit && hasLods(unit);
      const cls = [unit && !unit.lod_saved && 'unsaved', changed.has(keyOf({ race, module })) && 'changed'].filter(Boolean).join(' ');
      return h('td', { class: cls, title: ready ? `conversion ${unit.job}${unit.lod_saved ? '' : ' - LOD not saved yet: it is saved with the approval'}` : '' },
        ready ? [lodLabel(unit.lod) + (unit.lod_saved ? '' : ' *'), h('small', { class: 'job', text: shortJob(unit.job) })] : '-');
    })));
    const box = h('div', { class: 'confirm-box' },
      h('h3', { text: `Approve ${card.title || pretty(card.name)}?` }),
      changes.length ? h('div', { class: 'confirm-changes' }, h('b', { text: 'The set changed since you looked at it. Check the highlighted cells and confirm again:' }),
        ...changes.map(c => h('div', { text: c.text }))) : null,
      h('p', { text: `The ${live.length} unit${live.length === 1 ? '' : 's'} below get their layers built at these LODs (conversion id under each LOD). `
        + 'The LODs are frozen until you use Back to 3D.' }),
      h('table', null, h('thead', null, head), h('tbody', null, ...rows)),
      unsaved.length ? h('p', { class: 'note', text: `* ${unsaved.length} LOD${unsaved.length === 1 ? ' is' : 's are'} not saved yet (the default is shown); approving saves ${unsaved.length === 1 ? 'it' : 'them'}.` }) : null,
      h('div', { class: 'confirm-buttons' }, button('Cancel', closeConfirm, '', 'Esc'), button('Approve', doApprove, 'primary', 'Enter or A')));
    const node = h('div', { class: 'confirm-overlay', tabIndex: -1 }, box);
    if (confirm) confirm.node.replaceWith(node); else modal.body.querySelector('.viewer-shell').append(node);
    confirm = { node, shown: shownUnits(), busy: false };
    node.focus();
  }

  /** Approve: show the LOD and conversion of every race x module cell first (the one place a wrong "All" would show up). */
  function askApprove() {
    const block = approveBlock();
    if (block) { toast(block); return; }
    if (confirm || isLocked()) return;
    renderConfirm();
  }

  /**
   * Confirmed. Sends `args.units` = {"race/module": {job, lod}} for EVERY unit the confirmation shows (so the server refuses when a
   * newer conversion or another LOD is current) and `args.lods` for the cells whose LOD was never saved (the server writes those
   * with the approval). Nothing else about the units is sent. A 409 keeps the dialog open: the item is reloaded and the table
   * redrawn with the differences highlighted; the reviewer has to confirm again.
   */
  async function doApprove() {
    if (!confirm || confirm.busy) return;
    const mine = confirm;
    mine.busy = true;
    mine.node.querySelectorAll('button').forEach(b => { b.disabled = true; });
    let done = false;
    try {
      // the table on screen (mine.shown) must be what the units are NOW, at the click and again after the pending saves: otherwise
      // nothing is sent, the table is redrawn with the differences and a fresh confirm is required
      const early = diffShown(mine.shown, shownUnits());
      if (early.length) { renderConfirm(early); return; }
      await writes.catch(() => {});             // pending LOD saves first, so the rev is the newest
      if (confirm !== mine) return;
      const drift = diffShown(mine.shown, shownUnits());
      if (drift.length) { renderConfirm(drift); return; }    // what the cells show changed behind the dialog: look again
      const sent = Object.fromEntries(Object.entries(mine.shown).map(([key, v]) => [key, { job: v.job, lod: v.lod }]));
      const lods = Object.fromEntries(Object.entries(mine.shown).filter(([, v]) => !v.saved).map(([key, v]) => [key, v.lod]));
      const result = await api.action(item.id, 'approve', { rev: item.rev, args: { lods, units: sent } });
      done = true;
      closeConfirm();
      // the server's undo descriptor has no rev: ask for the current one (the approval just bumped it)
      const undoable = result?.undo ? async () => {
        const rev = typeof result.rev === 'number' ? result.rev : (await api.item(item.id)).rev;
        await undoAction({ ...result.undo, rev });
        refresh().catch(() => {});
      } : null;
      toast('Approved: building layers', { undo: undoable });
    } catch (error) {
      if (error.conflict && confirm === mine && [null, 'rev', 'job', 'lod', 'changed'].includes(error.code)) {
        await changedUnderDialog(mine, error);
        return;
      }
      if (confirm === mine) closeConfirm();
      if (!error.conflict) toast(`Approve failed: ${error.message}`);
    } finally {
      if (confirm === mine && !done) { mine.busy = false; mine.node.querySelectorAll('button').forEach(b => { b.disabled = false; }); }
    }
    refresh().catch(() => {});
    await reload();
  }

  /** The server says the set changed since the confirmation was drawn: reload it and show the confirmation again with the differences. */
  async function changedUnderDialog(old, error) {
    try {
      item = await api.item(card.id);
    } catch (reloadError) {
      closeConfirm();
      toast(`Reload failed: ${reloadError.message}`);
      return;
    }
    if (!host.isCurrent()) return;
    pick();
    show();
    drawBadges();
    drawBuild();
    watch();
    if (isLocked() || approveBlock()) { closeConfirm(); toast(isLocked() ? 'Not approved: the set was approved elsewhere' : `Not approved: ${approveBlock()}`); return; }
    const server = [].concat(error.body?.changes ?? []).map(c => ({ key: '', text: typeof c === 'string' ? c : c?.message || JSON.stringify(c) }));
    let changes = diffShown(old.shown, shownUnits());
    if (!changes.length) changes = server.length ? server : [{ key: '', text: 'The set was changed elsewhere (revision). The cells are as shown now.' }];
    renderConfirm(changes);
  }

  async function retry(unit) {
    try {
      await api.action(item.id, 'rebuild', { rev: item.rev, args: { race: unit.race, module: unit.module } });
      toast(`Rebuilding ${pretty(unit.race)} ${moduleShort(unit.module)}`);
    } catch (error) {
      if (!error.conflict) toast(`Retry failed: ${error.message}`);
    }
    await pollItem();
  }

  /** "Building layers n/m", the units still working and the failed ones with their error and Retry. */
  function drawBuild() {
    if (!item?.decisions?.lod_approval) { buildPanel.hidden = true; return; }
    const live = liveUnits();
    const done = live.filter(built).length;
    const failed = live.filter(u => buildOf(u)?.status === 'failed');
    const working = live.filter(u => ['queued', 'building'].includes(buildOf(u)?.status) || ['queued', 'running', 'stalled'].includes(buildOf(u)?.task?.state));
    const moved = item.stage && item.stage !== '3d';
    const nodes = [h('div', { class: 'build-line' },
      h('strong', { text: moved ? `Layers built ${done}/${live.length}` : `Building layers ${done}/${live.length}` }),
      h('progress', { max: Math.max(live.length, 1), value: done }))];
    if (moved) {
      nodes.push(h('div', { class: 'build-line' }, `The set is now in ${pretty(item.stage)}.`,
        button(`Open in ${pretty(item.stage)}`, () => host.openIn?.(item.stage), 'primary')));
    }
    for (const unit of failed) {
      nodes.push(h('div', { class: 'build-error' }, h('b', { text: `${pretty(unit.race)} ${moduleShort(unit.module)}: build failed` }),
        h('pre', { text: buildOf(unit).error || 'no error text' }), button('Retry', () => retry(unit), '', 'Build this unit again')));
    }
    if (working.length) {
      nodes.push(h('div', { class: 'build-working', text: working.map(u => {
        const b = buildOf(u);
        return `${pretty(u.race)} ${moduleShort(u.module)}: ${b.task?.state === 'stalled' ? 'stalled' : b.status}${b.task?.elapsed != null ? ` ${Math.round(b.task.elapsed)}s` : ''}`;
      }).join(' · ') }));
    }
    buildPanel.replaceChildren(...nodes);
    buildPanel.hidden = false;
  }

  const drawBadges = () => modal.setBadges([badge(item?.stage || card.stage || tab), ...((item?.card || card).badges || []).map(b => badge(b.text, b.tone))]);

  /** Re-read the item while layers are being built (no viewer reload): progress, errors, and the move to Aligned. */
  async function pollItem() {
    try {
      const fresh = await api.item(card.id);
      if (!host.isCurrent()) return;
      item = fresh;
      drawBadges();
      drawControls();
      drawGrid();
      drawBuild();
    } catch { /* the next poll tries again */ }
    watch();
  }

  function watch() {
    const busy = !!item?.decisions?.lod_approval && item?.stage === '3d' && liveUnits().some(u => !built(u));
    if (busy && !timer) timer = setInterval(pollItem, 2000);
    if (!busy && timer) { clearInterval(timer); timer = null; }
  }

  modal.setKeyHandler(event => {
    if (confirm) {
      if (event.key === 'Escape') closeConfirm();
      else if (event.key === 'Enter' || event.key.toLowerCase() === 'a') return mutationKey(event, doApprove);   // a fresh press, never a repeat
      return true;                          // nothing else (arrows, other shortcuts) reaches the dialog while the confirmation is open
    }
    if (event.key.toLowerCase() === 'a' && !isLocked()) return mutationKey(event, askApprove);   // opens the confirmation once per press
    return false;
  });

  // ---- start ----------------------------------------------------------------------------------------------
  modal.setTeardown(() => {
    carry.view = viewer.hasModel ? viewer.getView() : carry.view;
    clearInterval(timer);
    timer = null;
    viewer.dispose();
  });
  modal.setBadges([badge(card.stage || tab), ...(card.badges || []).map(b => badge(b.text, b.tone))]);
  setMessage('Loading...');
  try {
    item = await api.item(card.id);
  } catch (error) {
    if (host.isCurrent()) setMessage(`Details not available: ${error.message}`);
    return;
  }
  if (!host.isCurrent()) return;
  pick();
  drawGrid();
  drawBuild();
  watch();
  if (!sel.race) {
    drawControls();
    drawNotes();
    setMessage(noModelMessage());
    return;
  }
  show();
}
