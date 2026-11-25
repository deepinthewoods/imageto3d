// Mix tab: a free try-on over every published layer build (GET /api/v2/layers?race=R), like the prototype's mix.html.
// Pick a race, then an outfit or an armour body, gloves and boots from any set of that race (or bare); an outfit is the whole body
// below the neck, so gloves and boots are off with one. One Amount slider moves every layer's `fill` morph; it is a preview and is
// never saved. Toggles: red backs, wireframe, colour by layer. Nothing here writes to the server.
import { api } from './api.js';
import { createViewer } from './viewer.js';
import { state } from './state.js';
import { toast } from './toast.js';
import { viewToggles, stepper } from './aligned.js';
import { button, h, pretty, storage } from './util.js';

const view = { race: storage.get('mix.race'), camera: null };       // survives leaving and re-entering the tab

/** Build the page into `container`. Returns {unmount}. */
export function mountMix(container) {
  let data = null;                    // GET /api/v2/layers of `race`
  let race = null;
  let amount = 100;
  let seq = 0;
  let alive = true;
  const pick = { body: 'bare', gloves: 'bare', boots: 'bare' };

  const wrap = h('div', { class: 'viewer-wrap' });
  const message = h('div', { class: 'viewer-message', hidden: true });
  wrap.append(message);
  const viewer = createViewer(wrap);

  // ---- controls --------------------------------------------------------------------------------------------
  const raceSelect = h('select', { class: 'race-select', title: 'Race', onchange: () => selectRace(raceSelect.value) });
  const steppers = {
    body: stepper('Armour / outfit', { onPick: v => { pick.body = v; sync(); show(); }, title: 'An armour body (torso) or a whole-body outfit' }),
    gloves: stepper('Gloves', { onPick: v => { pick.gloves = v; sync(); show(); }, title: 'Both forearms and hands of a set' }),
    boots: stepper('Boots', { onPick: v => { pick.boots = v; sync(); show(); }, title: 'Both shins and feet of a set' }),
  };
  const amountValue = h('span', { class: 'val', text: '100%' });
  const amountInput = h('input', { type: 'range', min: 0, max: 100, step: 1, value: 100, 'aria-label': 'Amount (preview only)' });
  amountInput.oninput = () => {
    amount = +amountInput.value;
    amountValue.textContent = `${amount}%`;
    for (const key of keys) viewer.setLayerMorph(key, amount / 100);
  };
  const toggles = viewToggles(viewer);
  const notes = h('div', { class: 'layer-notes' });
  let keys = [];

  const side = h('div', { class: 'mix-side' },
    h('div', { class: 'hint', text: 'Each layer is cut from its own generated model and joins the game body at its neck / elbow / knee rings, so any armour, gloves and boots of a race fit together. An outfit is the whole body below the neck.' }),
    h('div', { class: 'pick' }, h('div', { class: 'lbl', text: 'Race' }), h('div', { class: 'ctl' }, raceSelect)),
    steppers.body.node, steppers.gloves.node, steppers.boots.node,
    h('div', { class: 'row' }, button('Random', () => random(), '', 'Random armour, gloves and boots'),
      button('Gloves + boots from the armour\'s set', () => sameSet(), '', 'Use the armour\'s own gloves and boots')),
    h('div', { class: 'amount-row' }, h('label', { text: 'Amount' }), amountInput, amountValue),
    h('div', { class: 'hint', text: 'Amount pushes a too-slim garment out to the base body (preview only, not saved).' }),
    h('div', { class: 'toggles' }, ...toggles.nodes), notes);
  container.replaceChildren(h('div', { class: 'mix-page' }, side, h('div', { class: 'mix-main' }, wrap)));

  // ---- data --------------------------------------------------------------------------------------------------
  const layers = () => data?.layers || [];
  const setsFor = slot => [...new Map(layers().filter(l => l.slot === slot).map(l => [l.set, l])).values()];
  const hasLegs = () => 'lower_leg.r' in (data?.bare || {});
  const find = (set, slot) => layers().find(l => l.set === set && l.slot === slot);
  const tag = l => `${l.title} · ${l.stage}`;

  function sync() {
    const outfit = pick.body.startsWith('outfit|');
    steppers.body.set([['bare', 'Bare'], ...setsFor('torso').map(l => [`torso|${l.set}`, `Armour · ${tag(l)}`]),
      ...setsFor('outfit').map(l => [`outfit|${l.set}`, `Outfit · ${tag(l)}`])], pick.body);
    pick.body = steppers.body.select.value || 'bare';
    for (const [id, slot] of [['gloves', 'lower_arm.r'], ['boots', 'lower_leg.r']]) {
      const options = [['bare', 'Bare'], ...setsFor(slot).map(l => [l.set, tag(l)])];
      steppers[id].set(options, pick[id], { disabled: outfit || (id === 'boots' && !hasLegs()) });
      pick[id] = steppers[id].select.value || 'bare';
    }
  }

  function chosen() {
    const bare = data?.bare || {};
    const out = [];
    const add = (slot, source) => {
      if (source) out.push({ key: slot, url: source.url, title: source.title, stage: source.stage, morph: amount / 100 });
      else if (bare[slot]) out.push({ key: slot, url: bare[slot], title: 'bare', stage: '', morph: 1 });
    };
    add('head', null);
    if (pick.body.startsWith('outfit|')) {
      add('outfit', find(pick.body.slice(7), 'outfit'));
      return out;
    }
    add('torso', pick.body.startsWith('torso|') ? find(pick.body.slice(6), 'torso') : null);
    for (const [id, limb] of [['gloves', 'lower_arm'], ['boots', 'lower_leg']]) {
      if (limb === 'lower_leg' && !hasLegs()) continue;
      for (const side of ['l', 'r']) add(`${limb}.${side}`, pick[id] === 'bare' ? null : find(pick[id], `${limb}.${side}`));
    }
    return out;
  }

  function setMessage(text) {
    message.hidden = !text;
    message.textContent = text || '';
  }

  async function show({ relative = false, first = false } = {}) {
    const mine = ++seq;
    const wanted = chosen();
    keys = wanted.map(l => l.key);
    if (!wanted.length) {
      viewer.clear();
      setMessage(data ? `No layers for ${pretty(race)} yet: approve a set in 3D to build them` : 'Loading...');
      notes.replaceChildren();
      return;
    }
    const result = await viewer.compose(wanted, { view: first ? view.camera : null, relative });
    if (!alive || mine !== seq || !result.applied) return;
    setMessage(result.failed.length ? `Not loaded: ${result.failed.map(f => `${f.key} (${f.error})`).join(', ')}` : null);
    notes.replaceChildren(h('h3', { text: 'Layers' }), ...wanted.map(l => h('div', { class: 'layer-row slot' },
      h('b', { text: l.key }), h('span', { text: `${l.title}${l.stage ? ' · ' + l.stage : ''} · ${(result.info[l.key]?.faces ?? 0).toLocaleString('en-US')} faces` }))));
  }

  async function selectRace(next, { first = false } = {}) {
    race = next;
    view.race = race;
    storage.set('mix.race', race);
    raceSelect.value = race;
    setMessage('Loading...');
    try {
      const got = await api.layers(race);
      if (!alive || race !== next) return;
      data = got;
    } catch (error) {
      if (!alive) return;
      data = { race, bare: {}, layers: [] };
      toast(`Layers not available: ${error.message}`);
    }
    for (const id of Object.keys(pick)) pick[id] = 'bare';
    sync();
    await show({ relative: !first, first });
  }

  function random() {
    const arm = setsFor('torso');
    if (arm.length) pick.body = `torso|${arm[Math.floor(Math.random() * arm.length)].set}`;
    for (const [id, slot] of [['gloves', 'lower_arm.r'], ['boots', 'lower_leg.r']]) {
      const sets = setsFor(slot);
      if (sets.length) pick[id] = sets[Math.floor(Math.random() * sets.length)].set;
    }
    sync();
    show();
  }

  function sameSet() {
    if (!pick.body.startsWith('torso|')) return;
    const set = pick.body.slice(6);
    for (const [id, slot] of [['gloves', 'lower_arm.r'], ['boots', 'lower_leg.r']]) pick[id] = find(set, slot) ? set : 'bare';
    sync();
    show();
  }

  // ---- start ---------------------------------------------------------------------------------------------------
  const races = state.races;
  raceSelect.replaceChildren(...races.map(r => h('option', { value: r, text: pretty(r) })));
  const start = races.includes(view.race) ? view.race : races.includes('human_male') ? 'human_male' : races[0];
  if (start) selectRace(start, { first: true }); else setMessage('No races yet');

  return {
    unmount() {
      alive = false;
      view.camera = viewer.hasModel ? viewer.getView() : view.camera;
      viewer.dispose();
      container.replaceChildren();
    },
  };
}
