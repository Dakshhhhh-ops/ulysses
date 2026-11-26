// The front view of the instrument, the JS twin of service/scene.py elevation().
// Pure functions: scene JSON in, world-space numbers out. No statistics.
import C from "../../src/ulysses/service/elevation_constants.json" with { type: "json" };

export const K = C.K, Z_MAX = C.Z_MAX, X0 = C.X0, X1 = C.X1, BANNER_Z = C.BANNER_Z, LATE = C.LATE_TOLERANCE;
export const BOX_W = C.BOX_W, BOX_H = C.BOX_H, MARGIN = C.MARGIN;
export const S = { FUTURE: 0, PAST: 1, CURRENT: 2, WIN: 3, LOSS: 4, HORIZON: 5, NOSTOP: 6, GHOST: 7 };

const r6 = (v) => Math.round(v * 1e6) / 1e6;
export const xOf = (t) => X0 + (X1 - X0) * Math.max(0, Math.min(1, t));
export const rOf = (z) => (z === null || z === undefined ? K * Z_MAX : K * Math.max(-Z_MAX, Math.min(Z_MAX, z)));

export function monotone(xs, ys, samples = 96) {
  const n = xs.length;
  if (n === 0) return [];
  if (n === 1) return [[r6(xs[0]), r6(ys[0])]];
  const d = [];
  for (let i = 0; i < n - 1; i++) d.push((ys[i + 1] - ys[i]) / (xs[i + 1] - xs[i]));
  const m = new Array(n).fill(0);
  m[0] = d[0]; m[n - 1] = d[n - 2];
  for (let i = 1; i < n - 1; i++) m[i] = d[i - 1] * d[i] <= 0 ? 0 : (d[i - 1] + d[i]) / 2;
  for (let i = 0; i < n - 1; i++) {
    if (d[i] === 0) { m[i] = 0; m[i + 1] = 0; continue; }
    const a = m[i] / d[i], b = m[i + 1] / d[i], s = a * a + b * b;
    if (s > 9) { const tau = 3 / Math.sqrt(s); m[i] = tau * a * d[i]; m[i + 1] = tau * b * d[i]; }
  }
  const out = [];
  const lo = xs[0], hi = xs[n - 1];
  let j = 0;
  for (let s = 0; s < samples; s++) {
    const x = lo + ((hi - lo) * s) / (samples - 1);
    while (j < n - 2 && x > xs[j + 1]) j++;
    const h = xs[j + 1] - xs[j], u = (x - xs[j]) / h;
    const h00 = (1 + 2 * u) * (1 - u) ** 2, h10 = u * (1 - u) ** 2, h01 = u * u * (3 - 2 * u), h11 = u * u * (u - 1);
    out.push([r6(x), r6(h00 * ys[j] + h10 * h * m[j] + h01 * ys[j + 1] + h11 * h * m[j + 1])]);
  }
  return out;
}

export function rings(scene) {
  const planned = scene.planned || [];
  const dec = scene.decision || {};
  const prop = scene.proposal || {};
  const applied = new Map();
  for (const h of scene.history || []) {
    if (h.counted && h.look_number && h.boundary_z !== null && h.boundary_z !== undefined) applied.set(h.look_number, h);
  }
  const curK = dec.look_number ?? null;
  const fired = { win: S.WIN, loss: S.LOSS, horizon: S.HORIZON }[dec.outcome] ?? null;
  const executed = prop.state === "executed";
  const out = [];
  for (const p of planned) {
    const k = p.look;
    let state = p.z === null ? S.NOSTOP : S.FUTURE;
    if (curK !== null && k < curK && p.z !== null) state = S.PAST;
    if (curK !== null && k === curK) state = fired !== null ? fired : S.CURRENT;
    let ring = { k, x: r6(xOf(p.t)), r: r6(rOf(p.z)), state, applied: false, open: 0, side: 0 };
    const a = applied.get(k);
    if (a && Math.abs(a.t - p.t) > LATE) {
      const ra = { ...ring, x: r6(xOf(a.t)), r: r6(rOf(a.boundary_z)), applied: true };
      ring.state = S.GHOST;
      out.push(ring);
      ring = ra;
    }
    if ((state === S.WIN || state === S.LOSS) && k === curK) {
      ring.side = state === S.WIN ? 1 : -1;
      ring.open = executed ? 1 : 0;
    }
    out.push(ring);
  }
  return out;
}

export function profile(scene, samples = 96) {
  const pts = (scene.planned || []).filter((p) => p.z !== null).map((p) => [xOf(p.t), rOf(p.z)]);
  if (!pts.length) return [];
  return monotone(pts.map((p) => p[0]), pts.map((p) => p[1]), samples);
}

export function thread(scene) {
  const runs = [];
  let cur = [];
  for (const h of scene.history || []) {
    if (h.z === null || h.z === undefined) { if (cur.length) runs.push(cur); cur = []; continue; }
    cur.push([r6(xOf(h.t)), r6(rOf(h.z))]);
  }
  if (cur.length) runs.push(cur);
  return runs;
}

export function beads(scene) {
  return (scene.history || []).map((h) => {
    if (h.z === null || h.z === undefined) return { seq: h.seq, x: r6(xOf(h.t)), y: 0, kind: "tick" };
    let kind = h.counted ? "bead" : "hollow";
    if (h.seq === scene.current_seq) kind = h.counted ? "current" : "current-hollow";
    return { seq: h.seq, x: r6(xOf(h.t)), y: r6(rOf(h.z)), kind, clamped: Math.abs(h.z) > Z_MAX };
  });
}

export function elevation(scene) {
  return {
    box: [r6((-BOX_W * (1 + MARGIN)) / 2), r6((-BOX_H * (1 + MARGIN)) / 2), r6(BOX_W * (1 + MARGIN)), r6(BOX_H * (1 + MARGIN))],
    mast: [X0 - 0.2, X1 + 0.2],
    banner: r6(K * BANNER_Z),
    zmax: r6(K * Z_MAX),
    rings: rings(scene),
    profile: profile(scene),
    thread: thread(scene),
    beads: beads(scene),
    proposed: (scene.proposed || []).map((p) => ({ x: r6(xOf(p.t)), r: r6(rOf(p.z)), dashed: p.z === null })),
  };
}

// The orthographic fit both drawings use (SVG preserveAspectRatio="xMidYMid meet").
export function fitFrustum(aspect, w = BOX_W * (1 + MARGIN), h = BOX_H * (1 + MARGIN)) {
  if (aspect > w / h) return { halfW: (h * aspect) / 2, halfH: h / 2 };
  return { halfW: w / 2, halfH: w / aspect / 2 };
}
