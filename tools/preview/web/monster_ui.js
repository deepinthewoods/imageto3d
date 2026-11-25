// Phase 5: the one registration hook for the monster UI. main.js asks `monsterView(tab, card)` which dialog to show and
// `monsterAction(...)` before it runs a hover verb; everything else about monsters lives in monster_concept.js, monster_views.js
// and (3D modal) monster3d.js. Loads monster.css itself so index.html and style.css stay untouched.
import { showMonsterConcept, approveMonsterConcept } from './monster_concept.js';
import { showMonsterViews } from './monster_views.js';

if (!document.querySelector('link[data-monster-css]')) {
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = 'monster.css';
  link.dataset.monsterCss = '';
  document.head.append(link);
}

export const isMonsterConcept = card => String(card?.id || '').startsWith('mconcept:');
export const isMonsterVariant = card => String(card?.id || '').startsWith('monster:');

/** {show, info} for the dialog of this card, or null when it is not one of the monster dialogs handled here. */
export function monsterView(tab, card) {
  if (isMonsterConcept(card)) return { show: showMonsterConcept, info: false };
  if (isMonsterVariant(card) && tab === 'approved') return { show: showMonsterViews, info: false };
  return null;
}

/**
 * A hover button on a monster card. Returns true when handled here:
 *  - Approve on a monster concept asks (variant number, "unattended" tick) before it posts;
 *  - Approve on a 3D monster card only opens its modal (the confirmation lives there);
 *  - Import on a flat pick (Approved) only opens its modal (Import asks about "unattended" there).
 * Everything else returns false and runs as any other verb.
 */
export function monsterAction(card, verb, tab, openCard) {
  if (isMonsterConcept(card) && verb === 'approve') {
    approveMonsterConcept(card, { tab });
    return true;
  }
  if (isMonsterVariant(card) && ((verb === 'approve' && tab === '3d') || verb === 'import')) {
    openCard(card);
    return true;
  }
  return false;
}
