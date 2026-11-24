"""GPU geometry kernels for the fitting code (2026-11-24, user: "write a GPU version ... something that we can use our GPU to get quicker
results"). Profiling a shell-slot fitting job (experiments/shell_slots_20261122) showed ~60 % of its time in body_fit.RayGrid.hits, then the
z-buffer renders (visibility.zbuffer), body_fit.Body.closest and Body.winding.

OpenGL 4.3 through moderngl (works on the AMD cards under Windows with the normal driver; no ROCm / CUDA needed). One context per process,
created on first use (it lands on the RX 9070). Kernels:
  ray_hits      RayGrid.hits: nearest hit on P + t N, t in [t_lo, t_hi], outward (t > eps) and inward (t < -eps), both triangle orientations;
                one invocation per ray, triangles streamed through shared memory in tiles of 64, segment bounding box test, Moller-Trumbore.
  closest_tri   for Body.closest: the index of the triangle nearest to each point (Ericson's closest point, squared distance); body_fit
                recomputes the point and distance on that triangle in float64, so results match the CPU except for exact ties.
  winding       Body.winding: generalised winding number (sum of solid angles, van Oosterom-Strackee).
  raster_ids    visibility.zbuffer: orthographic rasterisation of face ids with a depth test (gl_PrimitiveID into an integer texture).
Positions go to the GPU as float32 relative to a local centre (~0.1 um resolution at body scale). Every dispatch is kept short (well
under the Windows 2 s GPU timeout). Callers switch to these only with BODY_FIT_GPU=1 and fall back to the CPU on any error."""
import hashlib

import numpy as np

_ctx = None
_progs = {}
_error = None
LOCAL = 64
MAX_TESTS_PER_DISPATCH = 4e8          # items x triangles per dispatch

HEAD = """
#version 430
layout(local_size_x = 64) in;
layout(std430, binding = 0) readonly buffer Tris { vec4 tri[]; };     // per triangle: A, B, C (xyz)
uniform int n_tri;
uniform int n_item;
shared vec4 sh[3 * 64];
"""

RAYS = HEAD + """
layout(std430, binding = 1) readonly buffer Rays { vec4 ray[]; };     // per ray: P, N (xyz)
layout(std430, binding = 2) writeonly buffer Res { vec4 res[]; };     // per ray: (t_out, t_in, u_in, v_in)
layout(std430, binding = 3) writeonly buffer Idx { ivec2 idx[]; };    // per ray: (tri_out, tri_in)
uniform float t_lo;
uniform float t_hi;
uniform float eps;
void main() {
    uint i = gl_GlobalInvocationID.x;
    uint li = gl_LocalInvocationID.x;
    bool live = i < uint(n_item);
    vec3 p = vec3(0.0), d = vec3(0.0);
    if (live) { p = ray[2 * i].xyz; d = ray[2 * i + 1].xyz; }
    vec3 q0 = p + t_lo * d, q1 = p + t_hi * d;
    vec3 smin = min(q0, q1) - 1e-5, smax = max(q0, q1) + 1e-5;
    float best_out = 1e30; int tri_out = -1;
    float best_in = -1e30; int tri_in = -1; vec2 uv_in = vec2(0.0);
    for (int base = 0; base < n_tri; base += 64) {
        int k = base + int(li);
        if (k < n_tri) { sh[3 * li] = tri[3 * k]; sh[3 * li + 1] = tri[3 * k + 1]; sh[3 * li + 2] = tri[3 * k + 2]; }
        barrier();
        int m = min(64, n_tri - base);
        if (live) {
            for (int j = 0; j < m; j++) {
                vec3 a = sh[3 * j].xyz, b = sh[3 * j + 1].xyz, c = sh[3 * j + 2].xyz;
                if (any(lessThan(smax, min(min(a, b), c))) || any(greaterThan(smin, max(max(a, b), c)))) continue;
                vec3 e1 = b - a, e2 = c - a;
                vec3 pv = cross(d, e2);
                float det = dot(e1, pv);
                if (abs(det) <= 1e-14) continue;
                float inv = 1.0 / det;
                vec3 tv = p - a;
                float u = dot(tv, pv) * inv;
                vec3 q = cross(tv, e1);
                float v = dot(d, q) * inv;
                float t = dot(e2, q) * inv;
                if (u < -1e-6 || v < -1e-6 || u + v > 1.0 + 1e-6 || t < t_lo || t > t_hi || abs(t) <= eps) continue;
                if (t > 0.0) {
                    if (t < best_out) { best_out = t; tri_out = base + j; }
                } else if (t > best_in) { best_in = t; tri_in = base + j; uv_in = vec2(u, v); }
            }
        }
        barrier();
    }
    if (live) { res[i] = vec4(best_out, best_in, uv_in); idx[i] = ivec2(tri_out, tri_in); }
}
"""

CLOSEST = HEAD + """
layout(std430, binding = 1) readonly buffer Pts { vec4 pt[]; };
layout(std430, binding = 2) writeonly buffer Res { vec4 res[]; };     // per point: (squared distance, triangle as float bits,
                                                                       //  runner-up squared distance (another triangle), 0)
float d2_tri(vec3 p, vec3 a, vec3 b, vec3 c) {
    vec3 ab = b - a, ac = c - a, ap = p - a;
    float d1 = dot(ab, ap), d2 = dot(ac, ap);
    if (d1 <= 0.0 && d2 <= 0.0) return dot(ap, ap);
    vec3 bp = p - b;
    float d3 = dot(ab, bp), d4 = dot(ac, bp);
    if (d3 >= 0.0 && d4 <= d3) return dot(bp, bp);
    float vc = d1 * d4 - d3 * d2;
    if (vc <= 0.0 && d1 >= 0.0 && d3 <= 0.0) { vec3 x = ap - ab * (d1 / (d1 - d3)); return dot(x, x); }
    vec3 cp = p - c;
    float d5 = dot(ab, cp), d6 = dot(ac, cp);
    if (d6 >= 0.0 && d5 <= d6) return dot(cp, cp);
    float vb = d5 * d2 - d1 * d6;
    if (vb <= 0.0 && d2 >= 0.0 && d6 <= 0.0) { vec3 x = ap - ac * (d2 / (d2 - d6)); return dot(x, x); }
    float va = d3 * d6 - d5 * d4;
    if (va <= 0.0 && (d4 - d3) >= 0.0 && (d5 - d6) >= 0.0) { vec3 x = bp - (c - b) * ((d4 - d3) / ((d4 - d3) + (d5 - d6))); return dot(x, x); }
    float den = 1.0 / (va + vb + vc);
    vec3 x = ap - ab * (vb * den) - ac * (vc * den);
    return dot(x, x);
}
void main() {
    uint i = gl_GlobalInvocationID.x;
    uint li = gl_LocalInvocationID.x;
    bool live = i < uint(n_item);
    vec3 p = live ? pt[i].xyz : vec3(0.0);
    float best = 1e30, second = 1e30; int bt = -1;
    for (int base = 0; base < n_tri; base += 64) {
        int k = base + int(li);
        if (k < n_tri) { sh[3 * li] = tri[3 * k]; sh[3 * li + 1] = tri[3 * k + 1]; sh[3 * li + 2] = tri[3 * k + 2]; }
        barrier();
        int m = min(64, n_tri - base);
        if (live) {
            for (int j = 0; j < m; j++) {
                float d2 = d2_tri(p, sh[3 * j].xyz, sh[3 * j + 1].xyz, sh[3 * j + 2].xyz);
                if (d2 < best) { second = best; best = d2; bt = base + j; }
                else if (d2 < second) second = d2;
            }
        }
        barrier();
    }
    if (live) res[i] = vec4(best, intBitsToFloat(bt), second, 0.0);
}
"""

WINDING = HEAD + """
layout(std430, binding = 1) readonly buffer Pts { vec4 pt[]; };
layout(std430, binding = 2) writeonly buffer Res { vec2 res[]; };    // per point: (winding number, 1 = too close to a triangle's
uniform float unsure_rel;                                              //  branch cut for float32: recompute in float64 on the CPU)
void main() {
    uint i = gl_GlobalInvocationID.x;
    uint li = gl_LocalInvocationID.x;
    bool live = i < uint(n_item);
    vec3 p = live ? pt[i].xyz : vec3(0.0);
    float s = 0.0;
    float unsure = 0.0;
    for (int base = 0; base < n_tri; base += 64) {
        int k = base + int(li);
        if (k < n_tri) { sh[3 * li] = tri[3 * k]; sh[3 * li + 1] = tri[3 * k + 1]; sh[3 * li + 2] = tri[3 * k + 2]; }
        barrier();
        int m = min(64, n_tri - base);
        if (live) {
            for (int j = 0; j < m; j++) {
                vec3 a = sh[3 * j].xyz - p, b = sh[3 * j + 1].xyz - p, c = sh[3 * j + 2].xyz - p;
                float la = length(a), lb = length(b), lc = length(c);
                float num = dot(a, cross(b, c));
                float den = la * lb * lc + dot(a, b) * lc + dot(b, c) * la + dot(c, a) * lb;
                if (den <= 0.0 && abs(num) <= unsure_rel * la * lb * lc) unsure = 1.0;   // on / next to the triangle: num's sign picks +-pi
                if (num != 0.0 || den != 0.0) s += atan(num, den);   // atan(0, 0) is undefined in GLSL (AMD: +-pi), numpy gives 0
            }
        }
        barrier();
    }
    if (live) res[i] = vec2(s * 2.0 / (4.0 * 3.14159265358979), unsure);
}
"""

RASTER_VS = """
#version 430
in vec3 pos;                 // x, y in pixels, z = depth along the view direction (smaller = nearer)
uniform vec2 size;
uniform vec2 zr;             // (z near, z far)
void main() {
    gl_Position = vec4(2.0 * pos.x / size.x - 1.0, 2.0 * pos.y / size.y - 1.0, 2.0 * (pos.z - zr.x) / (zr.y - zr.x) - 1.0, 1.0);
}
"""
RASTER_FS = """
#version 430
out int fid;
void main() { fid = gl_PrimitiveID + 1; }
"""


def context():
    """The process's GPU context (created once), or None when no GPU is usable (see `info`)."""
    global _ctx, _error
    if _ctx is not None or _error is not None:
        return _ctx
    try:
        import moderngl
        _ctx = moderngl.create_context(standalone=True, require=430)
        _ctx.gc_mode = 'auto'
        for k, src in (('rays', RAYS), ('closest', CLOSEST), ('winding', WINDING)):
            _progs[k] = _ctx.compute_shader(src)
        _progs['raster'] = _ctx.program(vertex_shader=RASTER_VS, fragment_shader=RASTER_FS)
    except Exception as e:  # noqa: BLE001
        _error = f'{type(e).__name__}: {e}'
        _ctx = None
    return _ctx


def info():
    c = context()
    return {'renderer': c.info['GL_RENDERER'], 'version': c.info['GL_VERSION']} if c else {'error': _error}


def _need():
    if context() is None:
        raise RuntimeError(f'no GPU: {_error}')


def _fingerprint(A, B, C):
    h = hashlib.blake2b(digest_size=16)
    for X in (A, B, C):
        h.update(np.ascontiguousarray(X, np.float64).tobytes())
    return h.digest()


def _tris(owner, A, B, C):
    """Triangles on the GPU, cached on `owner` (one upload per grid / body), relative to their centre. The cache is keyed by a hash of the
    triangle data: the fitting code moves some meshes in place, and a stale upload gave wrong winding numbers."""
    key = _fingerprint(A, B, C)
    cache = getattr(owner, '_gpu_tris', None)
    if cache is None or cache[3] != key:
        if cache is not None:
            cache[0].release()
        o = (np.minimum(np.minimum(A, B), C).min(0) + np.maximum(np.maximum(A, B), C).max(0)) / 2
        T = np.zeros((len(A), 3, 4), np.float32)
        T[:, 0, :3], T[:, 1, :3], T[:, 2, :3] = A - o, B - o, C - o
        cache = (_ctx.buffer(T.tobytes()), o, len(A), key)
        try:
            owner._gpu_tris = cache
        except AttributeError:
            pass
    return cache[:3]


def _run(prog, n_tri, items, outs):
    """Dispatch `prog` over `items` (list of (binding, float32 array (m, 4))) in chunks; outs = [(binding, bytes per item, dtype, cols)].
    Returns the output arrays."""
    m_all = len(items[0][1])
    chunk = max(LOCAL, int(MAX_TESTS_PER_DISPATCH / max(n_tri, 1)) // LOCAL * LOCAL)
    res = [[] for _ in outs]
    prog['n_tri'].value = int(n_tri)
    for s in range(0, m_all, chunk):
        bufs = []
        for bind, arr in items:
            b = _ctx.buffer(np.ascontiguousarray(arr[s:s + chunk], np.float32).tobytes())
            b.bind_to_storage_buffer(bind)
            bufs.append(b)
        m = min(chunk, m_all - s)
        obufs = []
        for bind, nbytes, _, _ in outs:
            b = _ctx.buffer(reserve=m * nbytes)
            b.bind_to_storage_buffer(bind)
            obufs.append(b)
        prog['n_item'].value = m
        prog.run(group_x=(m + LOCAL - 1) // LOCAL)
        for k, ((_, _, dt, cols), b) in enumerate(zip(outs, obufs)):
            res[k].append(np.frombuffer(b.read(), dt).reshape(m, cols))
        for b in bufs + obufs:
            b.release()
    return [np.concatenate(r) if r else np.zeros((0, o[3]), o[2]) for r, o in zip(res, outs)]


def ray_hits(grid, P, N, t_lo, t_hi, eps):
    """body_fit.RayGrid.hits on the GPU (same dict)."""
    _need()
    n = len(P)
    out = {'t_out': np.full(n, np.inf), 'tri_out': np.full(n, -1, np.int64), 't_in': np.full(n, -np.inf),
           'tri_in': np.full(n, -1, np.int64), 'uv_in': np.zeros((n, 2))}
    tbuf, o, nt = _tris(grid, grid.A, grid.B, grid.C)
    tbuf.bind_to_storage_buffer(0)
    prog = _progs['rays']
    prog['t_lo'].value, prog['t_hi'].value, prog['eps'].value = float(t_lo), float(t_hi), float(eps)
    R = np.zeros((n, 2, 4), np.float32)
    R[:, 0, :3], R[:, 1, :3] = P - o, N
    res, idx = _run(prog, nt, [(1, R.reshape(n, 8))], [(2, 16, np.float32, 4), (3, 8, np.int32, 2)])
    res, idx = res.astype(np.float64), idx.astype(np.int64)
    hit_out, hit_in = idx[:, 0] >= 0, idx[:, 1] >= 0
    out['t_out'] = np.where(hit_out, res[:, 0], np.inf)
    out['tri_out'] = idx[:, 0]
    out['t_in'] = np.where(hit_in, res[:, 1], -np.inf)
    out['tri_in'] = idx[:, 1]
    out['uv_in'] = np.where(hit_in[:, None], res[:, 2:4], 0.)
    return out


CLOSEST_TIE_REL = 1e-4        # best and runner-up squared distances this close (relative) ...
CLOSEST_TIE_ABS = 1e-12       # ... or absolute (m^2): a tie (shared edge / vertex, or float32 noise) the caller settles on the CPU


def closest_tri(owner, A, B, C, P):
    """Index of the triangle nearest to each point, and a flag for (near) ties between two triangles: which of them wins decides the
    part / normal downstream, and the CPU's tie-break follows its search order, so the caller recomputes flagged points on the CPU."""
    _need()
    tbuf, o, nt = _tris(owner, A, B, C)
    tbuf.bind_to_storage_buffer(0)
    X = np.zeros((len(P), 4), np.float32)
    X[:, :3] = P - o
    (res,) = _run(_progs['closest'], nt, [(1, X)], [(2, 16, np.float32, 4)])
    tri = res[:, 1].copy().view(np.int32).astype(np.int64)
    best, second = res[:, 0].astype(np.float64), res[:, 2].astype(np.float64)
    tie = (second - best) <= CLOSEST_TIE_REL * best + CLOSEST_TIE_ABS
    return tri, tie


WINDING_UNSURE_REL = 1e-3


def winding(owner, A, B, C, P):
    """Generalised winding number of the triangles at each point, and a flag for points so close to a triangle (or behind it in its plane)
    that float32 can put them on the wrong side of the atan2 branch cut: the caller recomputes those in float64."""
    _need()
    tbuf, o, nt = _tris(owner, A, B, C)
    tbuf.bind_to_storage_buffer(0)
    X = np.zeros((len(P), 4), np.float32)
    X[:, :3] = P - o
    _progs['winding']['unsure_rel'].value = WINDING_UNSURE_REL
    (res,) = _run(_progs['winding'], nt, [(1, X)], [(2, 8, np.float32, 2)])
    return res[:, 0].astype(np.float64), res[:, 1] > .5


def raster_ids(x, y, z, F, W, H, znear=None):
    """Face id per pixel (H, W; -1 = background) of the triangles F over vertices at pixel coordinates (x, y) with depth z (smaller =
    nearer); pixel (i, j) covers x in [j, j+1), y in [i, i+1) like visibility.zbuffer. Fragments nearer than `znear` are clipped."""
    _need()
    import moderngl
    zlo = float(z.min()) if znear is None else float(znear)
    zhi = float(z.max())
    if zhi <= zlo:
        zhi = zlo + 1e-6
    pad = (zhi - zlo) * 1e-3 + 1e-9
    zr = (zlo - (0 if znear is not None else pad), zhi + pad)
    prog = _progs['raster']
    prog['size'].value = (float(W), float(H))
    prog['zr'].value = zr
    vb = _ctx.buffer(np.stack([x, y, z], 1).astype(np.float32).tobytes())
    ib = _ctx.buffer(np.ascontiguousarray(F, np.int32).tobytes())
    vao = _ctx.vertex_array(prog, [(vb, '3f', 'pos')], index_buffer=ib, index_element_size=4)
    col = _ctx.texture((W, H), 1, dtype='i4')
    dep = _ctx.depth_texture((W, H))
    fbo = _ctx.framebuffer(color_attachments=[col], depth_attachment=dep)
    fbo.use()
    fbo.clear(0.0, 0.0, 0.0, 0.0, depth=1.0)
    _ctx.enable(moderngl.DEPTH_TEST)
    _ctx.disable(moderngl.CULL_FACE)
    _ctx.depth_func = '<'
    vao.render(moderngl.TRIANGLES)
    img = np.frombuffer(col.read(), np.int32).reshape(H, W).astype(np.int64) - 1
    for obj in (vao, vb, ib, fbo, col, dep):
        obj.release()
    return img
