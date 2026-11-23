// Parity test of the viewer's symmetry math (fitsym.js) against fit_symmap.py --bake:
//   node tools/preview/test_fitsym.mjs <module_dir> <strategy,amount,sym,scope> <baked.glb>
// Reads the module's aligned GLB, the symmetry-off fit of the strategy and symmap.bin/json, blends them with fitsym.js `blendMeshes` and compares positions and
// normals with the GLB written by `python tools/tripo_pipeline/fit_symmap.py <module_dir> --bake --out <baked.glb> --choice ...`. Exit 1 when they differ by more than 1e-5.
import fs from 'node:fs';
import path from 'node:path';
import {parseSymmap, blendMeshes} from './fitsym.js';

const [dir, choice, bakedPath] = process.argv.slice(2);
if (!dir || !choice || !bakedPath) { console.error('usage: node test_fitsym.mjs <module_dir> <strategy,amount,sym,scope> <baked.glb>'); process.exit(2) }
const [strategy, amount, sym, scope] = choice.split(',');
const NC = {SCALAR: 1, VEC2: 2, VEC3: 3, VEC4: 4}, CT = {5120: Int8Array, 5121: Uint8Array, 5122: Int16Array, 5123: Uint16Array, 5125: Uint32Array, 5126: Float32Array};

function readGlb(file) {
  const b = fs.readFileSync(file), js = JSON.parse(b.subarray(20, 20 + b.readUInt32LE(12)).toString('utf8'));
  const binOff = 20 + b.readUInt32LE(12), bin = b.subarray(binOff + 8, binOff + 8 + b.readUInt32LE(binOff));
  const acc = i => {
    const a = js.accessors[i], v = js.bufferViews[a.bufferView], T = CT[a.componentType], nc = NC[a.type], off = (v.byteOffset || 0) + (a.byteOffset || 0), stride = v.byteStride || T.BYTES_PER_ELEMENT * nc;
    const out = new T(a.count * nc), dv = new DataView(bin.buffer, bin.byteOffset + off);
    const get = {5126: 'getFloat32', 5125: 'getUint32', 5123: 'getUint16', 5122: 'getInt16', 5121: 'getUint8', 5120: 'getInt8'}[a.componentType];
    for (let i2 = 0; i2 < a.count; i2++) for (let c = 0; c < nc; c++) out[i2 * nc + c] = dv[get](i2 * stride + c * T.BYTES_PER_ELEMENT, true);
    return out;
  };
  const meshes = [], visit = i => {
    const n = js.nodes[i];
    if (n.mesh !== undefined) for (const p of js.meshes[n.mesh].primitives) {
      const pos = acc(p.attributes.POSITION);
      meshes.push({pos, nor: p.attributes.NORMAL !== undefined ? acc(p.attributes.NORMAL) : null, index: p.indices !== undefined ? acc(p.indices) : Uint32Array.from({length: pos.length / 3}, (_, k) => k)});
    }
    (n.children || []).forEach(visit);
  };
  (js.scenes ? js.scenes[js.scene || 0].nodes : js.nodes.map((_, i) => i)).forEach(visit);
  return meshes;
}

const fit = path.join(dir, 'fitlab', 'lod_0p4'), base = readGlb(path.join(dir, 'reduced', 'lod_error_0p4__wearable_aligned.glb'));
const fitted = strategy !== 'original' && +amount > 0 ? readGlb(path.join(fit, `${strategy}__off__none.glb`)) : null;
const doc = JSON.parse(fs.readFileSync(path.join(fit, 'symmap.json'), 'utf8')), raw = fs.readFileSync(path.join(fit, 'symmap.bin'));
const map = parseSymmap(doc, raw.buffer.slice(raw.byteOffset, raw.byteOffset + raw.byteLength));
const t0 = performance.now(), res = blendMeshes(base, fitted, map, sym, scope, +amount), ms = performance.now() - t0, ref = readGlb(bakedPath);
let dp = 0, dn = 0;
res.forEach((m, k) => {
  for (let i = 0; i < m.pos.length; i++) dp = Math.max(dp, Math.abs(m.pos[i] - ref[k].pos[i]));
  if (m.nor) for (let i = 0; i < m.nor.length; i++) dn = Math.max(dn, Math.abs(m.nor[i] - ref[k].nor[i]));
});
console.log(`fitsym.js vs bake ${choice}: max position diff ${dp.toExponential(2)} (GLB units), max normal diff ${dn.toExponential(2)}, js ${ms.toFixed(1)} ms`);
process.exit(dp < 1e-5 && dn < 1e-5 ? 0 : 1);
