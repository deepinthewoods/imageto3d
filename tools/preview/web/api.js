// Thin fetch wrapper for /api/v2/*. Writes carry the per-session token (X-Token); a 409 means the item changed
// under us (another tab, the pipeline): the conflict handler refreshes state and tells the user, and the caller
// gets an ApiError with status 409 (and the server's current item, when it sent one) so it can reload its view.

const token = document.querySelector('meta[name="v2-token"]')?.content ?? '';
const BASE = '/api/v2';

export class ApiError extends Error {
  constructor(message, status, body) {
    super(message);
    this.status = status;
    this.body = body;
    /** The server's current copy of the item on a 409, when it sent one. */
    this.item = body?.item ?? null;
    /** The server's reason for a 409 / 503: rev, job, locked, unreadable, busy, read_only. */
    this.code = body?.code ?? null;
  }
  get conflict() { return this.status === 409; }
  /** A 409 that only means "somebody else changed it" (reload and retry), as opposed to a refusal that needs the user's attention. */
  get stale() { return this.status === 409 && (this.code === 'rev' || this.code === 'job' || this.code == null); }
}

let conflictHandler = null;
/** main.js registers what to do on a 409 (refresh state + toast). */
export function onConflict(fn) { conflictHandler = fn; }

function url(path, query) {
  const out = new URL(BASE + path, location.origin);
  for (const [key, value] of Object.entries(query || {})) {
    if (value != null) out.searchParams.set(key, value);
  }
  return out.pathname + out.search;
}

async function request(path, { method = 'GET', query, json, blob } = {}) {
  const headers = {};
  let body;
  if (method !== 'GET') headers['X-Token'] = token;
  if (json !== undefined) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(json);
  } else if (blob) {
    headers['Content-Type'] = blob.type || 'application/octet-stream';
    body = blob;
  }
  let response;
  try {
    response = await fetch(url(path, query), { method, headers, body, cache: 'no-store' });
  } catch (error) {
    throw new ApiError(`Server not reachable (${error.message})`, 0, null);
  }
  const text = await response.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { /* not JSON */ }
  if (!response.ok) {
    const error = new ApiError(data?.error || `HTTP ${response.status}`, response.status, data);
    if (error.conflict) conflictHandler?.(error);
    throw error;
  }
  return data;
}

export const api = {
  state: () => request('/state'),
  item: id => request('/item', { query: { id } }),
  /** Concept-stage and set verbs. `rev` is the item revision the user saw (set verbs). */
  action: (id, verb, { rev, args } = {}) =>
    request('/action', { method: 'POST', json: { id, verb, ...(rev != null ? { rev } : {}), ...(args ? { args } : {}) } }),
  /** One unit: {id, race, module, lod, rev}; every unit: {id, lod, all: true, rev}; monsters: {id, lod}. */
  lod: body => request('/lod', { method: 'POST', json: body }),
  /** Set an Amount: {id, race, group: outfit|body|limbs, value: 0..100, rev}. */
  amount: body => request('/amount', { method: 'POST', json: body }),
  /** Published layer builds of a race (and the race's bare layers) for the donor dropdowns and the Mix tab. */
  layers: race => request('/layers', { query: { race } }),
  queue: () => request('/queue'),
  /** op: retry | cancel */
  queueOp: (op, id) => request('/queue', { method: 'POST', json: { op, id } }),
  thumbnail: (glbUrl, blob) => request('/thumbnail', { method: 'POST', query: { url: glbUrl }, blob }),
};
