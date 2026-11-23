// Fit lab runtime symmetry (2026-11-19): pure math, no DOM, no three.js (browser module imported by app.js; also runs under node, see test_fitsym.mjs).
// The map (symmap.json + symmap.bin) comes from tools/tripo_pipeline/fit_symmap.py, whose SymMap.mirror / blend are the same math (used by its --bake).
// A mesh is a Float32Array of local GLB positions per primitive (glTF traversal order = viewer mesh order); the map works per welded vertex in "scene space"
// (node matrix applied to the GLB position). A target-side vertex with weight w > 0 becomes lerp(p, R(sum b_i q_i), w); every other vertex keeps its exact value.
const DT = {i32: Int32Array, f32: Float32Array, i8: Int8Array};

export function parseSymmap(doc, buf) {
  const a = {};
  for (const [name, d] of Object.entries(doc.arrays)) a[name] = new DT[d.dtype](buf, d.offset, d.count);
  const n = a.wid.length, K = doc.welded_vertices, first = new Int32Array(K).fill(-1), primOf = new Uint16Array(n);
  for (let v = n - 1; v >= 0; v--) first[a.wid[v]] = v;
  doc.prims.forEach((p, k) => primOf.fill(k, p.offset, p.offset + p.count));
  return {doc, a, n, K, first, primOf, prims: doc.prims, R: doc.reflection};
}

// New Float32Array per mesh: `pos` mirrored for direction sym ('l2r' | 'r2l') and scope ('limbs' | 'whole'); sym 'off' = plain copies.
export function mirror(map, pos, sym, scope) {
  const out = pos.map(p => Float32Array.from(p));
  if (sym === 'off') return out;
  const {a, K, first, primOf, prims, R} = map, idx = a[sym + '_idx'], tri = a[sym + '_tri'], bary = a[sym + '_bary'], w = a[`${sym}_w_${scope}`];
  const W = new Float64Array(K * 3);
  for (let t = 0; t < K; t++) {
    const v = first[t], k = primOf[v], p = prims[k], M = p.to_scene, P = pos[k], l = (v - p.offset) * 3, x = P[l], y = P[l + 1], z = P[l + 2];
    W[3 * t] = M[0][0] * x + M[0][1] * y + M[0][2] * z + M[0][3];
    W[3 * t + 1] = M[1][0] * x + M[1][1] * y + M[1][2] * z + M[1][3];
    W[3 * t + 2] = M[2][0] * x + M[2][1] * y + M[2][2] * z + M[2][3];
  }
  const Wn = Float64Array.from(W), moved = new Uint8Array(K);
  for (let j = 0; j < idx.length; j++) {
    const wj = w[j];
    if (!(wj > 0)) continue;
    const t = idx[j], i0 = 3 * tri[3 * j], i1 = 3 * tri[3 * j + 1], i2 = 3 * tri[3 * j + 2], b0 = bary[3 * j], b1 = bary[3 * j + 1], b2 = bary[3 * j + 2];
    const qx = b0 * W[i0] + b1 * W[i1] + b2 * W[i2], qy = b0 * W[i0 + 1] + b1 * W[i1 + 1] + b2 * W[i2 + 1], qz = b0 * W[i0 + 2] + b1 * W[i1 + 2] + b2 * W[i2 + 2];
    const rx = R[0][0] * qx + R[0][1] * qy + R[0][2] * qz + R[0][3], ry = R[1][0] * qx + R[1][1] * qy + R[1][2] * qz + R[1][3], rz = R[2][0] * qx + R[2][1] * qy + R[2][2] * qz + R[2][3];
    const o = 3 * t;
    Wn[o] = W[o] + wj * (rx - W[o]);
    Wn[o + 1] = W[o + 1] + wj * (ry - W[o + 1]);
    Wn[o + 2] = W[o + 2] + wj * (rz - W[o + 2]);
    moved[t] = 1;
  }
  const wid = a.wid;
  for (let v = 0; v < wid.length; v++) {
    const t = wid[v];
    if (!moved[t]) continue;
    const k = primOf[v], p = prims[k], M = p.from_scene, l = (v - p.offset) * 3, o = 3 * t, x = Wn[o], y = Wn[o + 1], z = Wn[o + 2], O = out[k];
    O[l] = M[0][0] * x + M[0][1] * y + M[0][2] * z + M[0][3];
    O[l + 1] = M[1][0] * x + M[1][1] * y + M[1][2] * z + M[1][3];
    O[l + 2] = M[2][0] * x + M[2][1] * y + M[2][2] * z + M[2][3];
  }
  return out;
}

// Normals of a moved mesh (body_fit.recomputed_normals): the original normal n0 shifted by the mean change of the face normals around the vertex (p0 -> p1).
// `index` = triangle corner indices (Uint16/32Array); result written into `out` (Float32Array) when given.
export function recomputeNormals(index, p0, n0, p1, out = new Float32Array(n0.length)) {
  const nv = n0.length / 3, acc = new Float64Array(n0.length), cnt = new Float64Array(nv), f = [0, 0, 0], g = [0, 0, 0];
  const face = (p, a, b, c, o) => {
    const ux = p[3 * b] - p[3 * a], uy = p[3 * b + 1] - p[3 * a + 1], uz = p[3 * b + 2] - p[3 * a + 2], vx = p[3 * c] - p[3 * a], vy = p[3 * c + 1] - p[3 * a + 1], vz = p[3 * c + 2] - p[3 * a + 2];
    const x = uy * vz - uz * vy, y = uz * vx - ux * vz, z = ux * vy - uy * vx, l = Math.hypot(x, y, z);
    if (l > 1e-30) { o[0] = x / l; o[1] = y / l; o[2] = z / l } else { o[0] = o[1] = o[2] = 0 }
  };
  for (let t = 0; t + 2 < index.length; t += 3) {
    const a = index[t], b = index[t + 1], c = index[t + 2];
    face(p0, a, b, c, f);
    face(p1, a, b, c, g);
    const dx = g[0] - f[0], dy = g[1] - f[1], dz = g[2] - f[2];
    for (const i of [a, b, c]) { acc[3 * i] += dx; acc[3 * i + 1] += dy; acc[3 * i + 2] += dz; cnt[i] += 1 }
  }
  for (let i = 0; i < nv; i++) {
    const c = Math.max(cnt[i], 1), x = n0[3 * i] + acc[3 * i] / c, y = n0[3 * i + 1] + acc[3 * i + 1] / c, z = n0[3 * i + 2] + acc[3 * i + 2] / c, l = Math.hypot(x, y, z);
    if (l > 1e-9) { out[3 * i] = x / l; out[3 * i + 1] = y / l; out[3 * i + 2] = z / l } else { out[3 * i] = n0[3 * i]; out[3 * i + 1] = n0[3 * i + 1]; out[3 * i + 2] = n0[3 * i + 2] }
  }
  return out;
}

// out = a + (b - a) * t (t = 0: a)
export function lerpPositions(out, a, b, t) {
  if (!t) out.set(a);
  else for (let i = 0; i < a.length; i++) out[i] = a[i] + (b[i] - a[i]) * t;
  return out;
}

// The whole result for a choice, for tests / export parity with fit_symmap.blend. base: [{pos, nor, index}] per mesh (the aligned GLB); fitted: [{pos, nor}] of the
// symmetry-off strategy GLB or null (strategy original); sym 'off' blends positions and (file) normals, otherwise both endpoints are mirrored, positions blended by
// amount and the normals recomputed. Returns [{pos, nor}].
export function blendMeshes(base, fitted, map, sym, scope, amount) {
  const t = fitted ? amount / 100 : 0;
  if (sym === 'off') {
    return base.map((m, i) => {
      const f = fitted ? fitted[i] : m, pos = lerpPositions(new Float32Array(m.pos.length), m.pos, f.pos, t);
      let nor = null;
      if (m.nor) {
        nor = new Float32Array(m.nor.length);
        for (let k = 0; k < nor.length; k += 3) {
          let x = m.nor[k], y = m.nor[k + 1], z = m.nor[k + 2];
          if (t && f.nor) { x += (f.nor[k] - x) * t; y += (f.nor[k + 1] - y) * t; z += (f.nor[k + 2] - z) * t }
          const l = Math.hypot(x, y, z) || 1;
          nor[k] = x / l; nor[k + 1] = y / l; nor[k + 2] = z / l;
        }
      }
      return {pos, nor};
    });
  }
  const A = mirror(map, base.map(m => m.pos), sym, scope), B = fitted ? mirror(map, fitted.map(m => m.pos), sym, scope) : A;
  return base.map((m, i) => {
    const pos = lerpPositions(new Float32Array(m.pos.length), A[i], B[i], t);
    return {pos, nor: m.nor ? recomputeNormals(m.index, m.pos, m.nor, pos) : null};
  });
}
