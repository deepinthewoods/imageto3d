// Client copy of GET /api/v2/state. Polled every 10 s and after every action. Optimistic edits (a card removed or
// patched before the server confirmed) are laid over each fresh response until they are committed or reverted, so a
// poll that lands mid-action does not flash the card back.
import { api } from './api.js';

export const TAB_KEYS = ['concepts', 'provisional', 'approved', '3d', 'aligned', 'completed', 'review', 'archive', 'trash'];
const POLL_MS = 10000;
const OPTIMISTIC_TTL_MS = 30000; // a stuck edit never hides a card for longer than this

export const state = {
  loaded: false,
  races: [],
  /** tab key -> cards, with optimistic edits applied */
  tabs: Object.fromEntries(TAB_KEYS.map(key => [key, []])),
  queue: null,
  credits: null,
  paused: null,
  /** GET /api/v2/state `status` (phase 4): {mode, paused, services, queue_health ...}, drawn as the header chip (status.js) */
  status: null,
  /** {read_only, message, notes}: this server may be a read-only second instance (another one holds the writer lock) */
  server: { read_only: false, message: null, notes: [] },
  /** GET /api/v2/state `queue_health` as the server sent it, and its error texts (see queueHealthErrors) */
  queueHealth: null,
  queueErrors: [],
  error: null,
};

/** The error texts of the state's `queue_health`: a string, a list of strings / {message|error}, or {ok, errors|error}. */
export function queueHealthErrors(health) {
  if (health == null || health === true) return [];
  const text = entry => (typeof entry === 'string' ? entry : entry?.message || entry?.error || JSON.stringify(entry));
  if (typeof health === 'string') return [health];
  if (Array.isArray(health)) return health.map(text);
  const list = [].concat(health.errors ?? health.error ?? []).filter(Boolean).map(text);
  if (!list.length && health.ok === false) list.push(health.message || 'queue unhealthy');
  return list;
}

let raw = null;          // last server response
let rawText = '';        // its JSON, to skip identical polls
let sequence = 0;
let timer = null;
const listeners = new Set();
const edits = new Set(); // {tab, id, remove?, patch?, born}

export function subscribe(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

function notify() {
  for (const fn of listeners) fn(state);
}

function derive() {
  const now = Date.now();
  for (const edit of edits) if (now - edit.born > OPTIMISTIC_TTL_MS) edits.delete(edit);
  const tabs = {};
  for (const key of TAB_KEYS) {
    let cards = raw?.tabs?.[key] || [];
    for (const edit of edits) {
      if (edit.tab !== key) continue;
      if (edit.remove) cards = cards.filter(card => card.id !== edit.id);
      else if (edit.patch) cards = cards.map(card => (card.id === edit.id ? { ...card, ...edit.patch } : card));
    }
    tabs[key] = cards;
  }
  state.tabs = tabs;
  state.races = raw?.races || [];
  state.queue = raw?.queue ?? null;
  state.credits = raw?.credits ?? null;
  state.paused = raw?.paused ?? null;
  state.status = raw?.status ?? null;
  state.server = raw?.server ?? { read_only: false, message: null, notes: [] };
  state.queueHealth = raw?.queue_health ?? null;
  state.queueErrors = queueHealthErrors(state.queueHealth);
}

/** Fetch the state; resolves true when something changed. Responses overtaken by a newer request are dropped. */
export async function refresh() {
  const mine = ++sequence;
  let next;
  try {
    next = await api.state();
  } catch (error) {
    state.error = error.message;
    notify();
    throw error;
  }
  if (mine !== sequence) return false;
  const hadError = state.error !== null;
  state.error = null;
  const text = JSON.stringify(next);
  const changed = text !== rawText || !state.loaded;
  raw = next;
  rawText = text;
  state.loaded = true;
  derive();
  if (changed || hadError) notify();
  return changed;
}

export function startPolling() {
  clearInterval(timer);
  timer = setInterval(() => { if (!document.hidden) refresh().catch(() => {}); }, POLL_MS);
}

function addEdit(edit) {
  edit.born = Date.now();
  edits.add(edit);
  derive();
  notify();
  return {
    /** The server confirmed (and the state was refreshed): drop the overlay. */
    commit() { edits.delete(edit); derive(); notify(); },
    /** The server refused: put the card back. */
    revert() { edits.delete(edit); derive(); notify(); },
  };
}

export const optimistic = {
  remove: (tab, id) => addEdit({ tab, id, remove: true }),
  patch: (tab, id, patch) => addEdit({ tab, id, patch }),
};

export const findCard = (tab, id) => state.tabs[tab]?.find(card => card.id === id) ?? null;
