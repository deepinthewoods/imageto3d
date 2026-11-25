// 3D modal for character sets in the 3D / Aligned / Completed tabs (phase 1: the unit's labelled LOD GLB plus the
// LOD choice; Approve and the real Aligned view arrive in phase 3). Bottom bar: race, module, LOD with face counts,
// All, wireframe. A small race x module grid shows the chosen LOD of every unit so an odd one stands out.
// Race, module, wireframe and camera carry over between sets.
//
// LOD writes (DESIGN.md section 4): every write carries the set's `rev` and the `job` of each unit the user was looking at; a 409
// (stale rev, newer conversion, approved set) reloads the item and never retries. "All" returns the previous picks and the new rev;
// its Undo sends that map ONCE, with that rev, and stops on a conflict. Once the set is approved (Aligned / Completed, `item.locked`)
// the LOD selector is view-only: it changes the displayed mesh and posts nothing.
import { api } from './api.js';
import { createViewer } from './viewer.js';
import { raceVerb } from './actions.js';
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
  wrap.append(message);
  modal.body.append(h('div', { class: 'viewer-shell' }, wrap, gridBox, notes));
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
  // PHASE 3: the Approve button goes into this slot (3D tab: approve the whole set at the chosen LODs).
  const approveSlot = h('span', { class: 'approve-slot', dataset: { slot: 'approve', phase: '3' } });
  const lodNote = h('span', { class: 'lod-note', hidden: true, text: 'LOD view only: approved' });
  const backButton = button('Back to 3D', () => sendBack(), '', 'Clear the 3D approval so the LODs can be changed again');
  backButton.hidden = true;
  const hint = tab === 'aligned' || tab === 'completed'
    ? h('span', { class: 'phase-hint', text: `${tab === 'aligned' ? 'Aligned' : 'Completed'} view: phase 3 (showing the labelled LOD)` })
    : null;
  modal.actions.classList.add('model-actions');
  modal.actions.append(approveSlot, h('span', { class: 'entry-picker' }, prevRace, nextRace, raceSelect), moduleField,
    field('LOD', lodSelect), allButton, facesText, wireToggle.label, lodNote, backButton, ...(hint ? [hint] : []));

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
      if (host.isCurrent()) { pick(); show(); }
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

  // ---- start ----------------------------------------------------------------------------------------------
  modal.setTeardown(() => {
    carry.view = viewer.hasModel ? viewer.getView() : carry.view;
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
  if (!sel.race) {
    drawControls();
    drawNotes();
    setMessage(noModelMessage());
    return;
  }
  show();
}
