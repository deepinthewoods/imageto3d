// Run a verb on a card: optimistic removal from its tab, POST /api/v2/action, then an 8 s toast with Undo built from
// the response's `undo` ({verb, id, args?}). A refused or failed action puts the card back.
import { api } from './api.js';
import { optimistic, refresh } from './state.js';
import { toast } from './toast.js';
import { pretty } from './util.js';

const PAST = {
  provisional: 'Moved to Provisional',
  approve: 'Approved',
  reject: 'Rejected',
  unapprove: 'Unapproved',
  archive: 'Archived',
  unarchive: 'Unarchived',
  restore: 'Restored',
  drop_race: 'Dropped race of',
  undrop_race: 'Restored race of',
  complete: 'Completed',
  rollback: 'Rolled back',
};
const inflight = new Set();

export const verbLabel = verb => ({ provisional: 'Provisional', rollback: 'Roll back', complete: 'Complete' }[verb]
  || pretty(verb).replace(/^./, c => c.toUpperCase()));
export const verbClass = verb => (verb === 'approve' || verb === 'provisional' || verb === 'restore' ? 'primary'
  : verb === 'reject' ? 'danger' : '');

export async function undoAction(undo) {
  try {
    // the descriptor carries the rev (and prior drop metadata) of the action it reverses: never re-fetched, so an undo can never
    // silently reverse a LATER decision (the server answers 409 instead)
    await api.action(undo.id, undo.verb, { args: undo.args, rev: undo.rev });
    toast('Undone');
  } catch (error) {
    if (!error.stale) toast(`Undo failed: ${error.message}`);
    else toast('Undo not applied: the item changed since');
  }
  refresh().catch(() => {});
}

/**
 * Drop / undrop one race of a set (set verbs, `rev` = the revision the user saw). The toast's Undo is the descriptor the server
 * returned: it carries the new rev and the earlier drop metadata, so it is never rebuilt from a fresh fetch. `onDone` runs after
 * the call and after an Undo (the caller reloads its item).
 */
export async function raceVerb(item, race, verb, { onDone } = {}) {
  let result = null;
  try {
    result = await api.action(item.id, verb, { rev: item.rev, args: { race } });
    toast(`${verb === 'drop_race' ? 'Dropped' : 'Restored'} ${pretty(race)}`, {
      undo: result?.undo ? async () => { await undoAction(result.undo); await onDone?.(); } : null,
    });
  } catch (error) {
    if (!error.conflict) toast(`${verbLabel(verb)} failed: ${error.message}`); // a 409 is reported by the conflict handler
  }
  await onDone?.();
  return result;
}

/**
 * runAction(card, 'approve', {tab: 'concepts'})
 *   removes (default true): the card leaves `tab` at once (set verbs that only edit a decision pass false)
 *   rev, args: forwarded to the server
 *   onError(error): runs first when the server refuses (the caller can handle a 409 itself, e.g. keep its dialog open)
 *   quiet: a 409 is not reported by the global conflict handler (onError words it)
 *   subject: the toast's name for the card, or a function (response) -> name
 * Resolves the response, or null when it failed or another action on this card is still running.
 */
export async function runAction(card, verb, { tab, rev, args, removes = true, subject, onError, quiet = false } = {}) {
  if (inflight.has(card.id)) return null;
  inflight.add(card.id);
  const edit = removes ? optimistic.remove(tab, card.id) : null;
  let result;
  try {
    result = await api.action(card.id, verb, { rev, args, quiet });
  } catch (error) {
    edit?.revert();
    onError?.(error);
    if (!error.conflict) toast(`${verbLabel(verb)} failed: ${error.message}`); // a 409 is reported by the conflict handler
    return null;
  } finally {
    inflight.delete(card.id);
  }
  try { await refresh(); } catch { /* the poll will catch up */ }
  edit?.commit();
  const warning = result?.warning ? ` · Warning: ${result.warning}` : '';
  const named = typeof subject === 'function' ? subject(result || {}) : subject;     // a function: the text may use the response (phase 5)
  toast(`${PAST[verb] || verbLabel(verb)} ${named || card.title || pretty(card.name)}${warning}`, {
    undo: result?.undo ? () => undoAction(result.undo) : null,
  });
  return result || {};
}
