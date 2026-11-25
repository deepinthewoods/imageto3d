// Aligned and Completed modals (ported from experiments/shell_slots_20261122/out/layers/mix.html, on the shared viewer.js).
//
// Composition of the shown race: the bare head, then either the outfit layer, or the torso plus both lower arms and both lower legs
// (a gnome has no legs); a slot with no usable layer falls back to the race's bare layer. Every layer has the morph target `fill`
// (the Amount): the sliders drive it live and are saved on release (POST /api/v2/amount, one write at a time, each with the set's rev).
// Armour sets can try other sets' gloves and boots (both sides of a slot are taken from the donor, with the donor's own saved Amount).
// Hold Space = every layer at Amount 0. Completed (`readOnly`) shows the same viewer with the approved Amount as text.
//
// Approve (phase 3 review fixes): it is only possible when every unit of every race has a build AND the composition on screen holds
// every required layer (the bare head and the set's own slots) with no load failure; it sends exactly what the reviewer saw (the build
// id of every unit, the bare build id of every race, the amounts) and a 409 (changed / stale / rev) reloads, lists what changed and
// stays on the set. Sliders belong to the race and set they were drawn for (disabled synchronously when the race changes).
// The item is polled while ANY build or rebuild task of the set is queued / running / stalled.
// Approve (phase 3 re-check R1): the click captures the composition generation (bumped on every compose / item replacement / race change)
// and the exact displayed ids; after the pending Amount saves it ABORTS (no POST, "the view changed - check it and approve again", a fresh
// gesture needed) when the generation or an id changed or the composition is not fully loaded.
//
// Keyboard (same rules as the other modals): the shortcuts only fire when no control has focus; after a slider / select / checkbox
// changed, focus goes back to the dialog so Space and the arrows keep working.
import { api } from './api.js';
import { createViewer } from './viewer.js';
import { focusDialog, mutationKey } from './modal.js';
import { refresh } from './state.js';
import { toast } from './toast.js';
import { badge, button, h, pretty, storage } from './util.js';

/** Race and camera carried from one set to the next (the camera relative to the model's size). */
export const carry = { race: storage.get('aligned.race'), view: null };

export const LIMBS = [
  { id: 'gloves', label: 'Gloves', limb: 'lower_arm' },
  { id: 'boots', label: 'Boots', limb: 'lower_leg' },
];
const usable = build => !!build && ['published', 'stale'].includes(build.status) && !!build.slots;
const keyOf = (race, module) => `${race}/${module}`;
const TASK_ACTIVE = ['queued', 'running', 'stalled'];
/** A build (or bare build) with a task that is still going: the status alone says "published" while a rebuild runs. */
const taskActive = build => TASK_ACTIVE.includes(build?.task?.state);
const buildBusy = build => ['queued', 'building'].includes(build?.status) || taskActive(build);
/** `/files/tools/preview/cache/layers/<race>/<bare build id>/<slot>.glb` -> the build id (the item does not carry it separately). */
const idFromUrl = url => /\/layers\/[^/]+\/([^/?#]+)\/[^/?#]+(?:[?#].*)?$/.exec(String(url || ''))?.[1] ?? null;

/** Amount groups of a set kind: [[group, label]]. Outfit sets have one, armour sets a body and a gloves-and-boots slider. */
export const groupsFor = kind => (kind === 'outfit' ? [['outfit', 'Amount']] : [['body', 'Body'], ['limbs', 'Gloves & boots']]);
/** Which Amount group a layer slot belongs to. */
export const groupOfSlot = slot => (slot === 'outfit' ? 'outfit' : slot === 'torso' ? 'body' : slot.startsWith('lower_') ? 'limbs' : null);

/** "wall crossings left 74 · boundary edges 0" from a build's metrics. */
export function metricsText(metrics) {
  const parts = [];
  const sum = value => (value && typeof value === 'object' ? Object.values(value).reduce((a, b) => a + (Number(b) || 0), 0) : value);
  if (metrics?.wall_crossings_left != null) parts.push(`wall crossings left ${metrics.wall_crossings_left}`);
  if (metrics?.boundary_edges != null) parts.push(`boundary edges ${sum(metrics.boundary_edges)}`);
  return parts.join(' · ');
}

/** The three display toggles, remembered in localStorage and shared with the Mix tab. */
export function viewToggles(viewer, { onChange } = {}) {
  const state = { backs: !!storage.get('view.backs', true), wire: !!storage.get('wire', false), tint: !!storage.get('view.tint', false) };
  const apply = () => {
    viewer.setBackRed(state.backs);
    viewer.setWireframe(state.wire);
    viewer.setColourByLayer(state.tint);
  };
  const box = (key, label, storeKey, title) => {
    const input = h('input', {
      type: 'checkbox', checked: state[key], onchange: () => {
        state[key] = input.checked;
        storage.set(storeKey, input.checked);
        apply();
        onChange?.();
        focusDialog();
      },
    });
    return h('label', { class: 'check', title }, input, label);
  };
  apply();
  const nodes = [
    box('backs', 'Red backs', 'view.backs', 'Paint the inside of faces red: shows faces that point the wrong way'),
    box('wire', 'Wireframe', 'wire', 'Wireframe'),
    box('tint', 'Colour by layer', 'view.tint', 'One colour per layer, to see where one piece ends and the next begins'),
  ];
  return { state, nodes, apply };
}

/** A labelled dropdown with previous / next buttons ("1 / 5"). `options` is read each time: () => [[value, text], ...]. */
export function stepper(label, { onPick, title = '' }) {
  const select = h('select', { title });
  const count = h('span', { class: 'n' });
  let options = [];
  const step = d => {
    if (!options.length) return;
    const i = Math.max(0, options.findIndex(([v]) => v === select.value));
    onPick(options[(i + d + options.length) % options.length][0]);
  };
  select.onchange = () => { onPick(select.value); focusDialog(); };
  const prev = button('◀', () => step(-1), '', 'Previous');
  const next = button('▶', () => step(1), '', 'Next');
  const node = h('div', { class: 'pick' }, h('div', { class: 'lbl' }, h('span', { text: label }), count), h('div', { class: 'ctl' }, prev, select, next));
  return {
    node,
    select,
    set(newOptions, value, { disabled = false } = {}) {
      options = newOptions;
      select.replaceChildren(...options.map(([v, text]) => h('option', { value: v, text })));
      if (options.some(([v]) => v === value)) select.value = value;
      count.textContent = options.length ? `${Math.max(0, options.findIndex(([v]) => v === select.value)) + 1} / ${options.length}` : '';
      select.disabled = disabled;
      prev.disabled = next.disabled = disabled || options.length < 2;
      node.classList.toggle('off', disabled);
    },
  };
}

export const showAligned = (modal, host) => showComposite(modal, host, { readOnly: false });
export const showCompleted = (modal, host) => showComposite(modal, host, { readOnly: true });

async function showComposite(modal, host, { readOnly }) {
  const { card } = host;
  let item = null;
  let race = null;
  const donors = { gloves: 'own', boots: 'own' };   // 'own' | 'bare' | 'set:<name>'
  const amounts = {};                               // race -> {group: 0..100}, the local copy of the saved Amount
  const layerLists = new Map();                     // race -> GET /api/v2/layers response
  let writes = Promise.resolve();
  let renderSeq = 0;
  let composed = [];                                // the layers of the last composition: [{key, set, title, group, own, fallback}]
  let timer = null;
  let infoByKey = {};
  let sliderEpoch = 0;                              // bumped when the race changes: older sliders are dead
  let dragging = false;                             // a slider is being dragged (the poll must not rebuild it)
  let composition = { ok: false, reason: 'Loading layers...' };   // can the composition on screen be approved?
  let failures = [];                                // layers of the last composition that did not load: [{key, url, error}]
  let lastExtra = [];
  let notice = [];                                  // what changed when the server refused an approve
  let rebuilt = [];                                 // published build ids that changed under the viewer
  let approving = false;
  let generation = 0;                               // bumped on every compose / item replacement / race change: what Approve was clicked on

  // ---- layout ---------------------------------------------------------------------------------------------
  const wrap = h('div', { class: 'viewer-wrap' });
  const message = h('div', { class: 'viewer-message', hidden: true });
  wrap.append(message);
  const side = h('div', { class: 'aligned-side' });
  const raceSelect = h('select', { class: 'race-select', title: 'Race', onchange: () => { selectRace(raceSelect.value); focusDialog(); } });
  const setButtons = [button('‹', () => host.go(-1), '', 'Previous set'), button('›', () => host.go(1), '', 'Next set')];
  setButtons[0].disabled = document.getElementById('prev-set').disabled;
  setButtons[1].disabled = document.getElementById('next-set').disabled;
  const raceStep = d => {
    const races = raceList();
    const i = races.indexOf(race) + d;
    if (i >= 0 && i < races.length) selectRace(races[i]);
  };
  const top = h('div', { class: 'aligned-top' },
    h('span', { class: 'entry-picker' }, ...setButtons), h('strong', { class: 'set-title', text: card.title || pretty(card.name) }),
    h('span', { class: 'entry-picker' }, button('‹', () => raceStep(-1), '', 'Previous race'), raceSelect, button('›', () => raceStep(1), '', 'Next race')));
  modal.body.append(h('div', { class: 'aligned-view' }, top, h('div', { class: 'aligned-main' }, wrap, side)));
  const viewer = createViewer(wrap);

  // ---- side panel ------------------------------------------------------------------------------------------
  const amountBox = h('div', { class: 'amount-box' });
  const pickers = Object.fromEntries(LIMBS.map(limb => [limb.id, stepper(limb.label, {
    title: `${limb.label} of this set, of another set's published layers, or bare`,
    onPick: value => { donors[limb.id] = value; render(); },
  })]));
  const ownButton = button('Own pieces', () => { donors.gloves = donors.boots = 'own'; render(); }, '', 'Back to this set\'s own gloves and boots');
  const pickBox = h('div', { class: 'pick-box' }, h('h3', { text: 'Gloves and boots' }), pickers.gloves.node, pickers.boots.node, ownButton);
  const toggles = viewToggles(viewer);
  const notes = h('div', { class: 'layer-notes' });
  side.append(...[amountBox, readOnly ? null : pickBox, h('div', { class: 'toggles' }, ...toggles.nodes),
    h('div', { class: 'hint', text: 'Hold Space: every layer at Amount 0.' }), notes].filter(Boolean));

  // ---- bottom bar ------------------------------------------------------------------------------------------
  modal.actions.classList.add('model-actions');
  const rebuildButton = button('Rebuild', () => rebuild(), '', 'Build this race again (a current build stays as it is)');
  const backButton = button('Back to 3D', () => sendBack(), '', 'Clear the 3D approval so the LODs can be changed again');
  const approveButton = button('Approve', () => approve(), 'primary', 'Approve the Amounts of every race (A)');
  const unapproveButton = button('Unapprove', () => unapprove(), 'danger', 'Send the set back to Aligned');
  const status = h('span', { class: 'phase-hint' });
  if (readOnly) modal.actions.append(unapproveButton, status);
  else modal.actions.append(approveButton, rebuildButton, backButton, status);

  // ---- data ------------------------------------------------------------------------------------------------
  const kind = () => item?.kind === 'outfit' ? 'outfit' : 'armour';
  const dropped = () => Object.keys(item?.decisions?.dropped || {});
  const raceList = () => (item?.required_races || item?.races || []).filter(r => !dropped().includes(r));
  const build = module => item?.builds?.[keyOf(race, module)];
  const modulesOf = () => (kind() === 'outfit' ? ['outfit'] : ['body_armor', 'right_gauntlet_boot']);
  const bareOf = r => item?.bare?.[r] || layerLists.get(r)?.bare || {};
  const savedAmount = (r, group) => item?.decisions?.amount?.[r]?.[group] ?? 100;
  const approvedAmount = (r, group) => item?.decisions?.aligned_approval?.amount?.[r]?.[group];
  const amountOf = (r, group) => (readOnly ? (approvedAmount(r, group) ?? savedAmount(r, group)) : (amounts[r]?.[group] ?? savedAmount(r, group)));
  const setAmount = (r, group, value) => { (amounts[r] ??= {})[group] = value; };
  const donorLayers = limb => (layerLists.get(race)?.layers || []).filter(l => l.slot === `${limb}.r` && l.set !== item.name);
  const hasLimb = limb => `${limb}.r` in bareOf(race);
  const bareInfo = r => item?.bare_builds?.[r] || null;
  const bareBuildId = r => bareInfo(r)?.build_id ?? idFromUrl(Object.values(bareOf(r))[0]);
  /** The slots of THIS set's own build that the composition must show to be approved (besides the bare head). */
  const ownSlots = () => (kind() === 'outfit' ? ['outfit']
    : ['torso', ...LIMBS.filter(l => hasLimb(l.limb)).flatMap(l => [`${l.limb}.l`, `${l.limb}.r`])]);
  const idOf = module => build(module)?.build_id ?? null;
  /** Every unit of every race of the set, as the approval needs them: [[key, build]]. */
  const allUnitBuilds = () => raceList().flatMap(r => modulesOf().map(m => [keyOf(r, m), item?.builds?.[keyOf(r, m)]]));
  /** The build ids the reviewer is looking at: {builds: {"race/module": id}, bare: {race: id}}. */
  const seenIds = () => ({
    builds: Object.fromEntries(allUnitBuilds().map(([key, b]) => [key, b?.build_id ?? null])),
    bare: Object.fromEntries(raceList().map(r => [r, bareBuildId(r)])),
  });

  /** The layers of the current race and choices: [{key, url, set, title, group, morph}]. */
  function computeLayers() {
    const bare = bareOf(race);
    const out = [];
    const notes = [];
    const own = (title = card.title || pretty(card.name)) => ({ set: item.name, title });
    const bareLayer = (slot, fallback = false) => {
      if (bare[slot]) out.push({ key: slot, url: bare[slot], set: null, title: fallback ? 'bare (fallback)' : 'bare', group: null, morph: 1, fallback });
    };
    if (bare.head) bareLayer('head');
    else notes.push('No bare layers for this race: build them first.');
    if (kind() === 'outfit') {
      const b = build('outfit');
      if (usable(b) && b.slots.outfit) {
        out.push({ key: 'outfit', url: b.slots.outfit, ...own(), group: 'outfit', morph: amountOf(race, 'outfit') / 100, own: true });
      } else {
        notes.push(`No published outfit build for ${pretty(race)} (${b?.status || 'none'}): showing the bare body as a fallback.`);
        for (const slot of ['torso', 'lower_arm.l', 'lower_arm.r', 'lower_leg.l', 'lower_leg.r']) bareLayer(slot, true);
      }
      return { layers: out, notes };
    }
    const armour = build('body_armor');
    if (usable(armour) && armour.slots.torso) {
      out.push({ key: 'torso', url: armour.slots.torso, ...own(), group: 'body', morph: amountOf(race, 'body') / 100, own: true });
    } else {
      bareLayer('torso', true);
      notes.push(`No published body armour build for ${pretty(race)} (${armour?.status || 'none'}): bare torso as a fallback.`);
    }
    const gear = build('right_gauntlet_boot');
    for (const { id, limb } of LIMBS) {
      if (!hasLimb(limb)) continue;
      const choice = donors[id];
      const donor = choice.startsWith('set:') ? choice.slice(4) : null;
      for (const side of ['l', 'r']) {
        const slot = `${limb}.${side}`;
        if (choice === 'own' && usable(gear) && gear.slots[slot]) {
          out.push({ key: slot, url: gear.slots[slot], ...own(), group: 'limbs', morph: amountOf(race, 'limbs') / 100, own: true });
          continue;
        }
        const found = donor && donorLayers(limb).length ? (layerLists.get(race).layers.find(l => l.set === donor && l.slot === slot)) : null;
        if (found) {
          out.push({ key: slot, url: found.url, set: found.set, title: found.title, group: null, morph: (found.amount ?? 100) / 100, donor: true });
        } else {
          bareLayer(slot, choice === 'own');
          if (choice === 'own') notes.push(`No published ${limb === 'lower_arm' ? 'gloves' : 'boots'} for ${pretty(race)}: bare fallback.`);
        }
      }
    }
    return { layers: out, notes };
  }

  // ---- drawing ---------------------------------------------------------------------------------------------
  function setMessage(text) {
    message.hidden = !text;
    message.textContent = text || '';
  }

  function drawControls() {
    const races = raceList();
    raceSelect.replaceChildren(...races.map(r => {
      const modules = modulesOf();
      const built = modules.every(m => usable(item?.builds?.[keyOf(r, m)]));
      return h('option', { value: r, text: pretty(r) + (built ? '' : ' (not fully built)') });
    }));
    if (races.includes(race)) raceSelect.value = race;
    // sliders: bound to the set, race and epoch they were drawn for; the poll keeps them while one is being dragged
    const sliderKey = `${item?.id}|${race}|${kind()}|${sliderEpoch}`;
    if (!readOnly && amountBox.dataset.key === sliderKey) {
      if (!dragging) {
        for (const input of amountBox.querySelectorAll('input[type=range]')) {
          input.value = amountOf(race, input.dataset.group);
          input.nextElementSibling.textContent = `${input.value}%`;
        }
      }
    } else {
      amountBox.dataset.key = readOnly ? '' : sliderKey;
      const owner = { setId: item?.id, race, epoch: sliderEpoch };
      const live = () => !!item && owner.setId === item.id && owner.race === race && owner.epoch === sliderEpoch && host.isCurrent();
      const rows = groupsFor(kind()).map(([group, label]) => {
        const value = amountOf(race, group);
        const out = h('span', { class: 'val', text: `${value}%` });
        if (readOnly) {
          const approved = approvedAmount(race, group);
          const saved = savedAmount(race, group);
          const changed = approved != null && approved !== saved;
          return h('div', { class: 'amount-row' }, h('label', { text: label }), out, changed ? h('span', { class: 'changed', title: 'The Amount was changed after the approval: approve it again from Aligned', text: `amount changed: now ${saved}%` }) : null);
        }
        const input = h('input', { type: 'range', min: 0, max: 100, step: 1, value, 'aria-label': `${label} amount`, dataset: { group, race } });
        input.onpointerdown = () => { dragging = true; };
        input.oninput = () => {
          if (input.disabled || !live()) return;
          setAmount(owner.race, group, +input.value);
          out.textContent = `${input.value}%`;
          applyAmounts();
        };
        input.onchange = () => {
          dragging = false;
          // a release on a slider of another race / set (or one disabled while the race changed) never writes
          if (input.disabled || !live()) return;
          saveAmount(owner.setId, owner.race, group, +input.value);
          focusDialog();
        };
        return h('div', { class: 'amount-row' }, h('label', { text: label }), input, out);
      });
      amountBox.replaceChildren(h('h3', { text: readOnly ? 'Approved Amount' : 'Amount' }), ...rows);
    }
    // pickers
    for (const { id, limb } of LIMBS) {
      const present = hasLimb(limb) && kind() === 'armour';
      pickers[id].node.hidden = !present;
      if (!present) continue;
      const options = [['own', 'Own (this set)'], ...[...new Map(donorLayers(limb).map(l => [l.set, l])).values()]
        .map(l => [`set:${l.set}`, `${l.title} · ${l.stage}`]), ['bare', 'Bare']];
      if (!options.some(([v]) => v === donors[id])) donors[id] = 'own';
      pickers[id].set(options, donors[id]);
    }
    pickBox.hidden = kind() === 'outfit';
    ownButton.disabled = donors.gloves === 'own' && donors.boots === 'own';
    // buttons
    updateApprove();
    rebuildButton.disabled = !!item?.error;
  }

  /** Why Approve is not possible right now, or null: decisions, stage, every unit of every race built, and the composition on screen complete. */
  function approveBlock({ ignoreApproving = false } = {}) {
    if (!item) return 'Loading...';
    if (item.error) return `Decisions unreadable: ${item.error}`;
    if ((item.stage ?? card.stage) !== 'aligned') return 'The set is not in Aligned';
    const unbuilt = allUnitBuilds().filter(([, b]) => !usable(b) || !b.build_id);
    if (unbuilt.length) return `Every layer build must be published first: ${unbuilt.map(([key, b]) => `${key} (${b?.status || 'none'})`).join(', ')}`;
    const noBare = raceList().filter(r => !bareBuildId(r));
    if (noBare.length) return `No bare layers for ${noBare.map(pretty).join(', ')}: build them first`;
    if (approving && !ignoreApproving) return 'Approving...';
    if (!composition.ok) return composition.reason;
    return null;
  }

  function updateApprove() {
    if (readOnly) return;
    const block = approveBlock();
    approveButton.disabled = !!block;
    approveButton.title = block || 'Approve the Amounts of every race (A)';
  }

  function drawNotes(extra = lastExtra) {
    lastExtra = extra;
    const nodes = notice.map(text => h('div', { class: 'warn error', text }));
    nodes.push(...rebuilt.map(text => h('div', { class: 'warn', text })));
    if (!composition.ok && !composition.loading && !readOnly) nodes.push(h('div', { class: 'warn error', text: `Approve is off: ${composition.reason}` }));
    nodes.push(...extra.map(text => h('div', { class: 'warn', text })));
    for (const f of failures) nodes.push(h('div', { class: 'warn error', text: `Not loaded: ${f.key} (${f.url}): ${f.error}` }));
    for (const entry of composed) {
      const info = infoByKey[entry.key];
      const failed = failures.some(f => f.key === entry.key);
      nodes.push(h('div', { class: 'layer-row slot' + (failed ? ' failed' : '') },
        h('b', { text: entry.key }), h('span', { text: `${entry.title} · ${failed ? 'NOT LOADED' : info ? `${info.faces.toLocaleString('en-US')} faces` : '...'}${entry.donor ? ' · donor' : ''}` })));
    }
    for (const module of modulesOf()) {
      const b = build(module);
      const lines = [];
      if (!b) { nodes.push(h('div', { class: 'warn', text: `${pretty(module)}: no build` })); continue; }
      lines.push(`${pretty(module)}: ${b.status}${b.build_id ? ' · ' + b.build_id : ''}`);
      if (b.status === 'stale') nodes.push(h('div', { class: 'warn', text: `${pretty(module)}: built with older layer code. Use Rebuild.` }));
      if (b.status === 'failed') nodes.push(h('div', { class: 'warn error', text: `${pretty(module)}: build failed${b.error ? ' - ' + b.error : ''}` }));
      if (b.task && b.task.state !== 'failed') lines.push(`rebuild ${b.task.state}${b.task.elapsed != null ? ` ${Math.round(b.task.elapsed)}s` : ''}`);
      if (b.task?.state === 'stalled') nodes.push(h('div', { class: 'warn error', text: `${pretty(module)}: the build is stalled (Queue tab: Kill & retry)` }));
      if (b.task?.state === 'failed' && b.status !== 'failed') {
        nodes.push(h('div', { class: 'warn error', text: `${pretty(module)}: rebuild failed${b.task.error || b.error ? ' - ' + (b.task.error || b.error) : ''}; the older build is still shown` }));
      }
      nodes.push(h('div', { class: 'layer-row' }, lines.join(' · ')));
      for (const warning of b.warnings || []) nodes.push(h('div', { class: 'warn', text: `${pretty(module)}: ${warning}` }));
      const metrics = metricsText(b.metrics);
      if (metrics) nodes.push(h('div', { class: 'layer-row' + (b.metrics?.wall_crossings_left > 0 ? ' warn-text' : ''), text: metrics }));
    }
    const bareState = bareInfo(race);
    if (bareState) {
      const text = `Bare layers of ${pretty(race)}: ${bareState.status}${bareState.build_id ? ' · ' + bareState.build_id : ''}${bareState.task ? ` · ${bareState.task.state}` : ''}`;
      if (bareState.status === 'stale') nodes.push(h('div', { class: 'warn', text: `${text}: built with older layer code. Use Rebuild.` }));
      else if (bareState.status === 'failed' || bareState.task?.state === 'failed') nodes.push(h('div', { class: 'warn error', text: `${text}: rebuild failed${bareState.error ? ' - ' + bareState.error : ''}` }));
      else nodes.push(h('div', { class: 'layer-row', text }));
    }
    for (const r of raceList().filter(r => r !== race && bareInfo(r)?.status === 'stale')) {
      nodes.push(h('div', { class: 'warn', text: `Bare layers of ${pretty(r)}: stale: built with older layer code. Select that race and use Rebuild, or Rebuild stale in the Queue tab.` }));
    }
    if (item?.not_converted?.length) nodes.push(h('div', { class: 'layer-row', text: 'Not converted: ' + item.not_converted.map(n => pretty(n.race)).join(', ') }));
    notes.replaceChildren(h('h3', { text: 'Layers' }), ...nodes);
  }

  function applyAmounts() {
    for (const entry of composed) {
      if (entry.group) viewer.setLayerMorph(entry.key, amountOf(race, entry.group) / 100);
    }
  }

  async function ensureLayers(r) {
    // only armour sets pick other sets' pieces; the item already carries the bare layers of its races
    if ((readOnly || kind() === 'outfit') && item.bare?.[r]) return;
    try {
      layerLists.set(r, await api.layers(r));
    } catch (error) {
      toast(`Layers not available: ${error.message}`);
      if (!layerLists.has(r)) layerLists.set(r, { race: r, bare: {}, layers: [] });
    }
  }

  /** The composition is approvable when nothing failed to load and the bare head plus every own slot of the set is on screen. */
  function judge(layers, result) {
    if (result.failed.length) return { ok: false, reason: `a layer did not load (${result.failed.map(f => f.key).join(', ')})` };
    const missing = ['head', ...ownSlots()].filter(slot => {
      const entry = layers.find(l => l.key === slot);
      return !entry || (slot !== 'head' && !entry.own) || result.info[slot]?.url !== entry.url;
    });
    return missing.length ? { ok: false, reason: `required layers not shown: ${missing.join(', ')} (own pieces of this set and the bare head)` } : { ok: true };
  }

  async function render({ relative = false, first = false } = {}) {
    const mine = ++renderSeq;
    generation++;
    composition = { ok: false, loading: true, reason: 'Loading layers...' };
    failures = [];
    drawControls();
    if (!race) { composition = { ok: false, reason: 'No race to show' }; updateApprove(); setMessage('No race to show'); return; }
    const { layers, notes: extra } = computeLayers();
    composed = layers;
    if (!layers.length) {
      viewer.clear();
      infoByKey = {};
      composition = { ok: false, reason: `no layers for ${pretty(race)}` };
      updateApprove();
      setMessage(`No layers for ${pretty(race)}`);
      drawNotes(extra);
      return;
    }
    if (!viewer.hasModel) setMessage('Loading...');
    const result = await viewer.compose(layers, { view: first ? carry.view : null, relative });
    if (mine !== renderSeq || !host.isCurrent() || !result.applied) return;
    infoByKey = result.info;
    failures = result.failed;
    composition = judge(layers, result);
    setMessage(result.failed.length ? `Not loaded: ${result.failed.map(f => `${f.key} (${f.error})`).join(', ')}` : null);
    applyAmounts();
    drawNotes(extra);
    updateApprove();
  }

  async function selectRace(next) {
    if (!next || next === race) return;
    race = next;
    generation++;
    // synchronously, before any await: the sliders on screen belong to the old race and must not save into the new one
    sliderEpoch++;
    for (const input of amountBox.querySelectorAll('input')) input.disabled = true;
    composition = { ok: false, loading: true, reason: 'Loading layers...' };
    updateApprove();
    carry.race = race;
    storage.set('aligned.race', race);
    donors.gloves = donors.boots = 'own';
    await ensureLayers(race);
    if (host.isCurrent() && race === next) render({ relative: true });
  }

  // ---- saving ----------------------------------------------------------------------------------------------
  const enqueue = fn => { writes = writes.catch(() => {}).then(fn); return writes; };

  function saveAmount(setId, r, group, value) {
    enqueue(async () => {
      const result = await api.amount({ id: setId, race: r, group, value, rev: item.rev });
      if (typeof result?.rev === 'number') item.rev = result.rev;
      item.decisions.amount = { ...(item.decisions.amount || {}), [r]: { ...(item.decisions.amount?.[r] || {}), [group]: value } };
    }).catch(async error => {
      if (!error.conflict) toast(`Amount not saved: ${error.message}`);
      await reload();
    });
  }

  async function reload({ quiet = false } = {}) {
    try {
      const fresh = await api.item(card.id);
      if (!host.isCurrent()) return;
      noteRebuilt(item, fresh);
      item = fresh;
      generation++;
      for (const key of Object.keys(amounts)) delete amounts[key];   // the saved values are the truth again
      if (!raceList().includes(race)) race = raceList().includes(carry.race) ? carry.race : raceList()[0] ?? null;
      await render();
      watch();
    } catch (error) {
      if (!quiet) toast(`Reload failed: ${error.message}`);
    }
  }

  /** A published build id that differs from the one on screen (a rebuild finished): say so, the composition re-reads the new files. */
  function noteRebuilt(before, after) {
    if (!before) return;
    const changes = [];
    const compare = (label, old, now) => {
      if (old?.build_id && now?.build_id && old.build_id !== now.build_id && ['published', 'stale'].includes(now.status ?? 'published')) {
        changes.push(`${label}: rebuilt ${old.build_id} -> ${now.build_id}`);
      }
    };
    for (const [key, b] of Object.entries(after?.builds || {})) compare(key, before.builds?.[key], b);
    for (const [r, b] of Object.entries(after?.bare_builds || {})) compare(`bare ${r}`, before.bare_builds?.[r], b);
    if (changes.length) { rebuilt = changes; toast('Layers rebuilt: the viewer shows the new build', { ms: 2500 }); }
  }

  /**
   * Poll the item while ANY build of this set (or its bare layers) has a task that is queued, running or stalled, or a status of
   * queued / building. The top-level status alone stays "published" while a rebuild of an older build runs.
   */
  function watch() {
    const all = [...Object.values(item?.builds || {}), ...Object.values(item?.bare_builds || {})];
    const busy = all.some(buildBusy);
    const stalled = all.some(b => b?.task?.state === 'stalled');
    if (!holding) status.textContent = busy ? (stalled ? 'Layer build stalled...' : 'Building layers...') : '';
    if (busy && !timer) timer = setInterval(() => reload({ quiet: true }), 2500);
    if (!busy && timer) { clearInterval(timer); timer = null; }
  }

  // ---- verbs -------------------------------------------------------------------------------------------------
  async function rebuild() {
    notice = [];
    try {
      await api.action(item.id, 'rebuild', { rev: item.rev, args: { race } });
      toast(`Rebuilding ${pretty(race)}`);
    } catch (error) {
      if (!error.conflict) toast(`Rebuild failed: ${error.message}`);
    }
    refresh().catch(() => {});
    await reload();
  }

  async function sendBack() {
    try {
      await api.action(item.id, 'unapprove', { rev: item.rev, args: { to: '3d' } });
      toast('Sent back to 3D');
      await refresh().catch(() => {});
      host.openIn?.('3d');
      return;
    } catch (error) {
      if (!error.conflict) toast(`Back to 3D failed: ${error.message}`);
    }
    refresh().catch(() => {});
    await reload();
  }

  /** The Amount of every race the set needs, as the server stores it: {race: {outfit} | {body, limbs}}. */
  function amountMap() {
    return Object.fromEntries(raceList().map(r => [r, Object.fromEntries(groupsFor(kind()).map(([group]) => [group, amountOf(r, group)]))]));
  }

  /** What changed between the ids sent and the ones the reloaded item shows (build per unit, bare build per race). */
  function describeChanges(sent, now) {
    const lines = [];
    for (const [key, id] of Object.entries(sent.builds)) {
      if (now.builds[key] !== id) lines.push(`${key}: build ${id ?? 'none'} -> ${now.builds[key] ?? 'none'}`);
    }
    for (const [r, id] of Object.entries(sent.bare)) {
      if (now.bare[r] !== id) lines.push(`bare ${pretty(r)}: build ${id ?? 'none'} -> ${now.bare[r] ?? 'none'}`);
    }
    return lines;
  }

  /** The server refused with a 409 (changed / stale / rev): nothing was approved. Reload, say what changed, stay on the set. */
  async function explainRefusal(sent, error) {
    await writes.catch(() => {});
    await reload();
    if (!host.isCurrent()) return;
    const lines = describeChanges(sent, seenIds());
    const server = [].concat(error.body?.changes ?? []).map(c => (typeof c === 'string' ? c : c?.message || JSON.stringify(c)));
    const stage = item?.stage ?? card.stage;
    const moved = stage !== 'aligned';
    const why = moved ? `Not approved: the set is now in ${stage === '3d' ? '3D' : pretty(stage)}, not Aligned (it was moved elsewhere). Nothing was approved here.`
      : error.code === 'stale' ? 'Not approved: some layers are stale (built with older layer code). Rebuild them first.'
      : lines.length || server.length ? 'Not approved: the layers changed since you looked at them. Review them and approve again.'
        : 'Not approved: the set changed (revision) since you looked at it. Review it and approve again.';
    rebuilt = [];                                          // the refusal lists the changes itself
    notice = [why, ...(moved ? [] : lines.length ? lines : server)];
    drawNotes();
    updateApprove();
  }

  /** The composition or the ids changed between the click and the moment Approve would send: nothing is posted, a fresh gesture is needed. */
  function viewChanged(block) {
    rebuilt = [];
    notice = ['Not approved: the view changed - check it and approve again.', ...(block ? [`(${block})`] : [])];
    toast('The view changed - check it and approve again', { ms: 3000 });
    drawNotes();
    updateApprove();
  }

  async function approve() {
    if (readOnly || approving) return;
    const early = approveBlock();
    if (early) { toast(early); return; }
    // what the reviewer looked at when the gesture happened: the composition generation and the exact build ids (units and bare)
    const clicked = { generation, ids: JSON.stringify(seenIds()) };
    approving = true;
    updateApprove();
    try {
      await writes.catch(() => {});                       // saved Amounts first, so the rev is the newest
      if (!host.isCurrent()) return;
      // after the wait: the same composition, fully loaded, with the same ids - otherwise abort and ask for a fresh gesture
      const sent = seenIds();
      const block = approveBlock({ ignoreApproving: true });
      if (generation !== clicked.generation || JSON.stringify(sent) !== clicked.ids || block) { viewChanged(block); return; }
      notice = [];
      let refused = null;
      const result = await host.actThenAdvance(card, 'approve', {   // no await between the check above and the request
        rev: item.rev,
        args: { amount: amountMap(), builds: sent.builds, bare: sent.bare },
        onError: error => { refused = error; },
      });
      if (!result && refused?.conflict && host.isCurrent()) await explainRefusal(sent, refused);
    } finally {
      approving = false;
      if (host.isCurrent()) updateApprove();
    }
  }

  async function unapprove() {
    host.actAndAdvance(card, 'unapprove', { rev: item.rev, args: { to: 'aligned' } });
  }

  // ---- keyboard: hold Space = Amount 0 -------------------------------------------------------------------------
  let holding = false;
  const hold = on => {
    if (holding === on) return;
    holding = on;
    viewer.setMorphHold(on ? 0 : null);
    status.textContent = on ? 'Amount 0 (holding Space)' : (timer ? 'Building layers...' : '');
  };
  const onKeyUp = event => { if (event.key === ' ') hold(false); };
  const onBlur = () => hold(false);
  const onPointerUp = () => { dragging = false; };
  document.addEventListener('keyup', onKeyUp);
  document.addEventListener('pointerup', onPointerUp);
  window.addEventListener('blur', onBlur);
  modal.setKeyHandler(event => {
    if (event.key === ' ') {
      event.preventDefault();
      if (!event.repeat) hold(true);
      return true;
    }
    if (!readOnly && event.key.toLowerCase() === 'a') return mutationKey(event, approve);   // one fresh press, never a held key
    return false;
  });
  modal.setTeardown(() => {
    carry.view = viewer.hasModel ? viewer.getView() : carry.view;
    clearInterval(timer);
    timer = null;
    sliderEpoch++;
    document.removeEventListener('keyup', onKeyUp);
    document.removeEventListener('pointerup', onPointerUp);
    window.removeEventListener('blur', onBlur);
    viewer.dispose();
  });

  // ---- start ---------------------------------------------------------------------------------------------------
  modal.setBadges([badge(card.stage || (readOnly ? 'completed' : 'aligned')), ...(card.badges || []).map(b => badge(b.text, b.tone))]);
  setMessage('Loading...');
  try {
    item = await api.item(card.id);
  } catch (error) {
    if (host.isCurrent()) setMessage(`Details not available: ${error.message}`);
    return;
  }
  if (!host.isCurrent()) return;
  const races = raceList();
  race = races.includes(carry.race) ? carry.race : races.includes('human_male') ? 'human_male' : races[0] ?? null;
  await ensureLayers(race);
  if (!host.isCurrent()) return;
  await render({ first: true });
  watch();
}
