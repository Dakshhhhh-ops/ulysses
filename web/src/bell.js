// The boundary bell: the signed stop rule turned on a lathe, with Flagsmith's
// chance to win drawn as an ink thread inside it. Faced square on (Read), it is
// the exact group-sequential chart; the SVG elevation draws the same numbers.
// Renders on demand only: nothing moves at rest.
import {
  WebGLRenderer, Scene, OrthographicCamera, Color, Vector3, Object3D, Quaternion,
  BufferGeometry, BufferAttribute, InstancedBufferGeometry, InstancedBufferAttribute,
  Mesh, InstancedMesh, ShaderMaterial, MeshBasicMaterial, IcosahedronGeometry, RingGeometry, BoxGeometry,
  CylinderGeometry, PlaneGeometry, CanvasTexture, LineSegments, Points, DoubleSide, SRGBColorSpace,
  NeutralToneMapping,
} from "three";
import { LineSegments2 } from "three/addons/lines/LineSegments2.js";
import { LineSegmentsGeometry } from "three/addons/lines/LineSegmentsGeometry.js";
import { LineMaterial } from "three/addons/lines/LineMaterial.js";
import { elevation, xOf, K, Z_MAX, BOX_W, BOX_H, MARGIN, fitFrustum, S } from "./elevation.js";
import { labelScene, note, rememberLowPower } from "./labels.js";

const TAU = Math.PI * 2;
const DEG = Math.PI / 180;
const INSPECT = { az: -22 * DEG, el: 14 * DEG };
const READ = { az: 0, el: 0 };
const TOKENS = ["paper", "sheet", "enamel", "enamel-in", "ink", "graphite", "rule", "hairline", "contract", "flagsmith", "win", "loss", "amber"];

function readTokens() {
  const cs = getComputedStyle(document.documentElement);
  const t = {};
  for (const k of TOKENS) t[k] = new Color(cs.getPropertyValue("--" + k).trim() || "#000");
  return t;
}

const easeSnap = (x) => (x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2); // ~cubic-bezier(.65,0,.35,1)
const easeOut = (x) => 1 - Math.pow(1 - x, 3);
const clamp01 = (x) => Math.max(0, Math.min(1, x));
const lerp = (a, b, t) => a + (b - a) * t;

// ---- geometry builders (data in, Object3D out) -----------------------------------------------

function unitTorus(radial = 72, tubular = 8) {
  const g = new InstancedBufferGeometry();
  const u = [], v = [], idx = [];
  for (let i = 0; i <= radial; i++) for (let j = 0; j <= tubular; j++) { u.push((i / radial) * TAU); v.push((j / tubular) * TAU); }
  for (let i = 0; i < radial; i++) for (let j = 0; j < tubular; j++) {
    const a = i * (tubular + 1) + j, b = (i + 1) * (tubular + 1) + j;
    idx.push(a, b, a + 1, b, b + 1, a + 1);
  }
  g.setIndex(idx);
  g.setAttribute("position", new BufferAttribute(new Float32Array(u.length * 3), 3));
  g.setAttribute("aU", new BufferAttribute(new Float32Array(u), 1));
  g.setAttribute("aV", new BufferAttribute(new Float32Array(v), 1));
  return g;
}

const RING_VS = /* glsl */ `
attribute float aU; attribute float aV;
attribute float aX; attribute float aRadius; attribute float aState; attribute float aHighlight;
attribute float aOpen; attribute float aSide; attribute float aTube; attribute float aFade; attribute float aDim;
uniform vec3 uCol[9]; uniform vec3 uHair; uniform vec3 uSheet;
varying float vU; varying float vState; varying float vOpen; varying float vSide; varying vec3 vColor; varying vec3 vN;
void main() {
  float t = aTube * (1.0 + 0.8 * aHighlight);
  vec3 radial = vec3(0.0, sin(aU), cos(aU));
  vec3 n = radial * cos(aV) + vec3(sin(aV), 0.0, 0.0);
  vec3 p = vec3(aX, 0.0, 0.0) + radial * aRadius + n * t;
  int s = int(aState + 0.5);
  vec3 c = uCol[0];
  for (int i = 0; i < 9; i++) { if (i == s) c = uCol[i]; }
  c = mix(uHair, c, aFade);
  c = mix(uSheet, c, aDim);
  vColor = c; vU = aU; vState = aState; vOpen = aOpen; vSide = aSide; vN = n;
  gl_Position = projectionMatrix * modelViewMatrix * vec4(p, 1.0);
}`;
const RING_FS = /* glsl */ `
uniform vec3 uLight;
varying float vU; varying float vState; varying float vOpen; varying float vSide; varying vec3 vColor; varying vec3 vN;
void main() {
  float s = floor(vState + 0.5);
  if (abs(s - 6.0) < 0.5 && fract(vU * 16.0 / 6.2831853) > 0.55) discard;
  if (abs(s - 7.0) < 0.5 && fract(vU * 40.0 / 6.2831853) > 0.42) discard;
  if (abs(s - 8.0) < 0.5 && fract(vU * 24.0 / 6.2831853) > 0.7) discard;
  if (vOpen > 0.001) {
    float c = vSide > 0.0 ? 1.5707963 : -1.5707963;
    float d = abs(atan(sin(vU - c), cos(vU - c)));
    if (d < vOpen * 0.8726646) discard;
  }
  float l = 0.80 + 0.20 * max(dot(normalize(vN), uLight), 0.0);
  gl_FragColor = vec4(vColor * l, 1.0);
  #include <colorspace_fragment>
}`;

const SHELL_VS = /* glsl */ `
varying vec3 vP; varying vec3 vNrm; varying vec2 vUv;
void main() { vP = position; vNrm = normal; vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`;
const SHELL_FS = /* glsl */ `
uniform vec3 uOut; uniform vec3 uIn; uniform vec3 uLine; uniform vec3 uSky; uniform vec3 uGround; uniform vec3 uTintCol;
uniform float uCast; uniform float uTint; uniform float uLooks; uniform vec3 uL; uniform float uDim;
varying vec3 vP; varying vec3 vNrm; varying vec2 vUv;
void main() {
  if (vUv.x > uCast) discard;
  vec3 n = normalize(vNrm) * (gl_FrontFacing ? 1.0 : -1.0);
  vec3 base = gl_FrontFacing ? uIn : uOut;
  float amb = mix(0.95, 1.05, 0.5 + 0.5 * n.y);
  float dif = max(dot(n, uL), 0.0) * 0.16;
  vec3 col = base * (amb * 0.94 + dif);
  float f = (vP.x + 4.0) / 8.0 * uLooks;
  float wf = max(fwidth(f), 1e-4);
  float a1 = 1.0 - smoothstep(0.35, 1.1, abs(f - floor(f + 0.5)) / wf);
  float g = vUv.y * 12.0;
  float wg = max(fwidth(g), 1e-4);
  float a2 = 1.0 - smoothstep(0.35, 1.1, abs(g - floor(g + 0.5)) / wg);
  col = mix(col, uLine, 0.28 * max(a1, a2));
  float edge = 1.0 - smoothstep(0.0, 0.012, uCast - vUv.x);
  col = mix(col, uLine, edge * step(uCast, 0.999) * 0.8);
  col = mix(col, uTintCol, 0.08 * uTint);
  gl_FragColor = vec4(col, uDim);
  #include <tonemapping_fragment>
  #include <colorspace_fragment>
}`;

// back half of a surface of revolution about the x axis (phi 0..pi, z <= 0)
function halfTube(profile, segs = 48) {
  const n = profile.length;
  const pos = new Float32Array(n * (segs + 1) * 3), nor = new Float32Array(n * (segs + 1) * 3), uv = new Float32Array(n * (segs + 1) * 2);
  let p = 0, q = 0;
  for (let i = 0; i < n; i++) {
    const [x, r] = profile[i];
    const i0 = Math.max(0, i - 1), i1 = Math.min(n - 1, i + 1);
    const slope = (profile[i1][1] - profile[i0][1]) / (profile[i1][0] - profile[i0][0] || 1);
    for (let j = 0; j <= segs; j++) {
      const phi = (j / segs) * Math.PI;
      const cy = Math.cos(phi), cz = -Math.sin(phi);
      pos[p] = x; pos[p + 1] = r * cy; pos[p + 2] = r * cz;
      const len = Math.hypot(slope, 1);
      nor[p] = -slope / len; nor[p + 1] = cy / len; nor[p + 2] = cz / len;
      p += 3;
      uv[q++] = n > 1 ? i / (n - 1) : 0; uv[q++] = j / segs;
    }
  }
  const idx = [];
  for (let i = 0; i < n - 1; i++) for (let j = 0; j < segs; j++) {
    const a = i * (segs + 1) + j, b = (i + 1) * (segs + 1) + j;
    idx.push(a, a + 1, b, b, a + 1, b + 1);
  }
  const g = new BufferGeometry();
  g.setAttribute("position", new BufferAttribute(pos, 3));
  g.setAttribute("normal", new BufferAttribute(nor, 3));
  g.setAttribute("uv", new BufferAttribute(uv, 2));
  g.setIndex(idx);
  return g;
}

function segLine(pairs, color, width, opts = {}) {
  const g = new LineSegmentsGeometry();
  g.setPositions(pairs.length ? pairs : [0, 0, 0, 0, 0, 0]);
  const m = new LineMaterial({ color, linewidth: width, toneMapped: false, dashed: !!opts.dashed,
    dashSize: opts.dash || 0.12, gapSize: opts.gap || 0.08, transparent: !!opts.transparent, opacity: opts.opacity ?? 1,
    depthTest: opts.depthTest ?? true });
  const l = new LineSegments2(g, m);
  if (opts.dashed) l.computeLineDistances();
  if (!pairs.length) l.visible = false;
  return l;
}

function shadowTexture() {
  const c = document.createElement("canvas");
  c.width = 256; c.height = 64;
  const x = c.getContext("2d");
  const g = x.createRadialGradient(128, 32, 2, 128, 32, 128);
  g.addColorStop(0, "rgba(0,0,0,1)"); g.addColorStop(1, "rgba(0,0,0,0)");
  x.setTransform(1, 0, 0, 0.25, 0, 24);
  x.fillStyle = g; x.fillRect(0, 0, 256, 256);
  const t = new CanvasTexture(c);
  t.colorSpace = SRGBColorSpace;
  return t;
}

// ---- the instrument ---------------------------------------------------------------------------

class Instrument {
  constructor(plate, opts) {
    this.plate = plate;
    this.opts = opts;
    this.scene = plate.scene;
    this.stage = plate.stage;
    this.e = elevation(this.scene);
    this.tok = readTokens();
    this.tweens = [];
    this.running = false;
    this.visible = false;   // set by the IntersectionObserver's first report
    this.started = false;
    this.frames = 0;
    this.frameTimes = [];
    this.lost = 0;
    this.highlighted = { seq: null, k: null };

    const canvas = document.createElement("canvas");
    canvas.setAttribute("aria-hidden", "true");
    this.canvas = canvas;
    this.renderer = new WebGLRenderer({ canvas, antialias: devicePixelRatio < 2, powerPreference: "low-power", alpha: false });
    this.renderer.outputColorSpace = SRGBColorSpace;
    this.renderer.toneMapping = NeutralToneMapping;
    this.world = new Scene();
    this.camera = new OrthographicCamera(-1, 1, 1, -1, 0.1, 100);
    this.view = { ...((opts.phone || opts.reduced || this.scene.kind === "calibration") ? READ : INSPECT) };
    this.orbitVel = { az: 0, el: 0 };
    this.build();
    this.stage.insertBefore(canvas, this.stage.firstChild);
    this.resize();
    new ResizeObserver(() => { this.resize(); this.invalidate(); }).observe(this.stage);
    this.bindVisibility();
    this.bindPointer();
    this.bindTheme();
    canvas.addEventListener("webglcontextlost", (ev) => this.onLost(ev));
    canvas.addEventListener("webglcontextrestored", () => this.onRestored());
    if (opts.debug) this.debugEl = this.makeDebug();
  }

  // The opening sequence plays the first time the plate is on screen, not while it is scrolled away.
  maybeStart() {
    if (this.started || !this.visible) return;
    this.started = true;
    this.intro();
  }

  // ---- build ---------------------------------------------------------------------------------
  build() {
    const t = this.tok, e = this.e, sc = this.scene;
    const W = this.world;
    W.background = t.sheet.clone();
    const mats = (this.mats = {});

    // mast + look ticks
    const mastG = new CylinderGeometry(0.016, 0.016, 8.4, 8, 1, true);
    mastG.rotateZ(Math.PI / 2);
    mats.ink = new MeshBasicMaterial({ color: t.ink, toneMapped: false });
    W.add(new Mesh(mastG, mats.ink));
    const planned = sc.planned || [];
    if (planned.length) {
      const ticks = new InstancedMesh(new BoxGeometry(0.006, 0.08, 0.006), mats.ink, planned.length);
      const o = new Object3D();
      planned.forEach((p, i) => { o.position.set(xOf(p.t), 0, 0); o.updateMatrix(); ticks.setMatrixAt(i, o.matrix); });
      W.add(ticks);
    }

    // rings: one instanced draw for every ring, superseded ones included
    const all = [...e.rings.map((r) => ({ ...r })), ...e.proposed.map((s, i) => ({ k: -(i + 1), x: s.x, r: s.r, state: 8, open: 0, side: 0 }))];
    this.ringList = all;
    const n = Math.max(all.length, 1);
    const rg = unitTorus();
    const attr = (name, f) => { const a = new InstancedBufferAttribute(new Float32Array(n), 1); a.setUsage(35048); all.forEach((r, i) => (a.array[i] = f(r, i))); rg.setAttribute(name, a); return a; };
    const dimFrom = this.dimFrom();
    this.ra = {
      x: attr("aX", (r) => r.x), radius: attr("aRadius", (r) => r.r), state: attr("aState", (r) => r.state),
      hi: attr("aHighlight", () => 0), open: attr("aOpen", (r) => r.open), side: attr("aSide", (r) => r.side),
      tube: attr("aTube", (r) => this.tubeFor(r.state)), fade: attr("aFade", () => 1),
      dim: attr("aDim", (r) => (dimFrom !== null && r.k > dimFrom ? 0.3 : 1)),
    };
    rg.instanceCount = all.length;
    mats.ring = new ShaderMaterial({
      vertexShader: RING_VS, fragmentShader: RING_FS, toneMapped: false,
      uniforms: { uCol: { value: this.ringColors() }, uHair: { value: t.hairline }, uSheet: { value: t.sheet },
        uLight: { value: new Vector3(-0.35, 0.6, 0.72).normalize() } },
    });
    this.rings = new Mesh(rg, mats.ring);
    this.rings.frustumCulled = false;
    if (all.length) W.add(this.rings);

    // shell (back half, sectioned toward the camera)
    const prof = e.profile;
    const showShell = prof.length > 1 && !sc.wire;
    mats.shell = new ShaderMaterial({
      vertexShader: SHELL_VS, fragmentShader: SHELL_FS, side: DoubleSide, transparent: false,
      polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 2,
      uniforms: {
        uOut: { value: t.enamel }, uIn: { value: t["enamel-in"] }, uLine: { value: t.contract },
        uSky: { value: new Color("#ffffff") }, uGround: { value: t["enamel-in"] }, uTintCol: { value: t.ink },
        uCast: { value: sc.signed ? 1 : 0 }, uTint: { value: 0 }, uLooks: { value: Math.max(planned.length, 1) },
        uL: { value: new Vector3(-3, 5, 4).normalize() }, uDim: { value: 1 },
      },
    });
    if (showShell) {
      this.shell = new Mesh(halfTube(prof), mats.shell);
      W.add(this.shell);
    }

    // section edge: the crisp boundary you read
    if (prof.length > 1) {
      const pairs = [];
      for (const s of [1, -1]) for (let i = 0; i < prof.length - 1; i++) pairs.push(prof[i][0], s * prof[i][1], 0, prof[i + 1][0], s * prof[i + 1][1], 0);
      this.edge = segLine(pairs, t.contract, 2.0);
      W.add(this.edge);
    }

    // banner cylinder: Flagsmith docs' 95% / 5% convention
    const br = e.banner;
    mats.banner = new MeshBasicMaterial({ color: t.flagsmith, transparent: true, opacity: 0.07, depthWrite: false, side: DoubleSide, toneMapped: false });
    if (sc.kind !== "calibration" || true) {
      const bg = halfTube([[-4, br], [4, br]], 32);
      W.add(new Mesh(bg, mats.banner));
      this.bannerLines = segLine([-4, br, 0, 4, br, 0, -4, -br, 0, 4, -br, 0], t.flagsmith, 1.5, { dashed: true });
      W.add(this.bannerLines);
    }

    // thread + beads + needle
    this.buildThread();

    // contact shadow (seen only when the bell is turned)
    mats.shadow = new MeshBasicMaterial({ map: shadowTexture(), transparent: true, opacity: 0.10, depthWrite: false, toneMapped: false, color: t.ink });
    const sh = new Mesh(new PlaneGeometry(9.6, 2.6), mats.shadow);
    sh.rotation.x = -Math.PI / 2;
    sh.position.y = -K * Z_MAX - 0.25;
    W.add(sh);

    // end cap: closed, no decision
    if (sc.end_cap) {
      const last = [...e.rings].reverse().find((r) => r.state !== S.GHOST);
      if (last) {
        const cap = new Mesh(new RingGeometry(0, last.r, 64), new ShaderMaterial({
          transparent: true, depthWrite: false, side: DoubleSide, toneMapped: false,
          uniforms: { uInk: { value: t.ink } },
          vertexShader: "varying vec3 vP; void main(){ vP = position; gl_Position = projectionMatrix*modelViewMatrix*vec4(position,1.0);}",
          fragmentShader: "uniform vec3 uInk; varying vec3 vP; void main(){ float s = fract((vP.x + vP.y) * 9.0); if (s > 0.18) discard; gl_FragColor = vec4(uInk, 0.35); \n#include <colorspace_fragment>\n}",
        }));
        cap.rotation.y = Math.PI / 2;
        cap.position.x = last.x;
        W.add(cap);
      }
    }

    // calibration fan
    if (sc.paths && sc.paths.length) this.buildFan();

    // amendment: the vertical "authorises now" marker
    if (sc.action_now) {
      const x = 4 - 0.6;
      this.nowLine = segLine([x, -K * Z_MAX, 0, x, K * Z_MAX, 0], t.amber, 2);
      W.add(this.nowLine);
    }
  }

  tubeFor(state) {
    return { 3: 0.034, 4: 0.034, 2: 0.026, 5: 0.028, 7: 0.012, 8: 0.016 }[state] ?? 0.02;
  }

  ringColors() {
    const t = this.tok;
    return [t.contract, t.contract, t.contract, t.win, t.loss, t.graphite, t.contract, t.rule, t.amber];
  }

  dimFrom() {
    if (!this.scene.dim_after) return null;
    let k = 0;
    for (const h of this.scene.history || []) if (h.counted && h.look_number) k = h.look_number;
    return k;
  }

  buildThread() {
    const t = this.tok, e = this.e, sc = this.scene, W = this.world;
    const pairs = [];
    for (const run of e.thread) for (let i = 0; i < run.length - 1; i++) pairs.push(run[i][0], run[i][1], 0, run[i + 1][0], run[i + 1][1], 0);
    this.threadFull = new Float32Array(pairs);
    this.threadSegs = pairs.length / 6;
    this.thread = segLine(pairs, t.ink, 2.5);
    W.add(this.thread);

    const solid = e.beads.filter((b) => b.kind === "bead");
    const hollow = e.beads.filter((b) => b.kind === "hollow" || b.kind === "current-hollow" || b.kind === "tick");
    this.beadList = solid;
    this.beads = new InstancedMesh(new IcosahedronGeometry(0.05, 1), this.mats.ink, Math.max(solid.length, 1));
    this.beads.count = solid.length;
    this.beads.frustumCulled = false;
    W.add(this.beads);
    this.hollowList = hollow;
    this.mats.hollow = new MeshBasicMaterial({ color: t.ink, toneMapped: false, side: DoubleSide });
    this.hollow = new InstancedMesh(new RingGeometry(0.055, 0.075, 28), this.mats.hollow, Math.max(hollow.length, 1));
    this.hollow.count = hollow.length;
    this.hollow.frustumCulled = false;
    W.add(this.hollow);

    const cur = e.beads.find((b) => b.kind === "current");
    this.current = cur || null;
    if (cur) {
      this.curBead = new Mesh(new IcosahedronGeometry(0.08, 2), this.mats.ink);
      this.halo = new Mesh(new RingGeometry(0.11, 0.125, 40), this.mats.hollow);
      this.needle = segLine([cur.x, 0, 0, cur.x, cur.y, 0], t.ink, 1.25);
      W.add(this.curBead, this.halo, this.needle);
      this.needleY = cur.y;
      this.needleV = 0;
      // dimension line (HOLD): from the bead to the current ring's edge
      const dec = sc.decision;
      const ring = e.rings.find((r) => r.k === dec?.look_number && r.state !== S.GHOST);
      if (dec && dec.outcome === "hold" && ring) {
        const top = cur.y >= 0 ? ring.r : -ring.r;
        const x = ring.x + 0.2;
        this.dim = segLine([x, cur.y, 0, x, top, 0, ring.x + 0.1, top, 0, x + 0.1, top, 0, cur.x + 0.1, cur.y, 0, x + 0.1, cur.y, 0], t.ink, 1);
        const dir = Math.sign(top - cur.y) || 1, a = 0.06;
        const tri = new Float32Array([
          x, top, 0, x - a * 0.45, top - dir * a, 0, x + a * 0.45, top - dir * a, 0,
          x, cur.y, 0, x + a * 0.45, cur.y + dir * a, 0, x - a * 0.45, cur.y + dir * a, 0,
        ]);
        const tg = new BufferGeometry();
        tg.setAttribute("position", new BufferAttribute(tri, 3));
        this.arrows = new Mesh(tg, new MeshBasicMaterial({ color: t.ink, toneMapped: false, side: DoubleSide, transparent: true }));
        W.add(this.dim, this.arrows);
      }
    }
    this.placeBeads(this.threadSegs);
  }

  buildFan() {
    const t = this.tok, sc = this.scene;
    const runs = sc.paths, N = runs.length, looks = sc.looks;
    const pos = [], aZ = [], aPhi = [], aCat = [], aLook = [];
    const pPos = [], pZ = [], pPhi = [], pType = [], pLook = [];
    runs.forEach((run, i) => {
      const phi = ((i + 0.5) / N) * Math.PI;
      const cat = run.contract ? 2 : run.banner ? 1 : 0;
      const pts = run.points.filter((p) => p[1] !== null);
      for (let j = 0; j < pts.length - 1; j++) {
        for (const p of [pts[j], pts[j + 1]]) {
          pos.push(xOf(p[0] / looks), 0, 0); aZ.push(Math.max(-Z_MAX, Math.min(Z_MAX, p[1]))); aPhi.push(phi); aCat.push(cat); aLook.push(pts[j + 1][0]);
        }
      }
      if (run.banner) { pPos.push(xOf(run.banner.look / looks), 0, 0); pZ.push(run.banner.z); pPhi.push(phi); pType.push(0); pLook.push(run.banner.look); }
      if (run.contract) { pPos.push(xOf(run.contract.look / looks), 0, 0); pZ.push(run.contract.z); pPhi.push(phi); pType.push(1); pLook.push(run.contract.look); }
    });
    const g = new BufferGeometry();
    g.setAttribute("position", new BufferAttribute(new Float32Array(pos), 3));
    g.setAttribute("aZ", new BufferAttribute(new Float32Array(aZ), 1));
    g.setAttribute("aPhi", new BufferAttribute(new Float32Array(aPhi), 1));
    g.setAttribute("aCat", new BufferAttribute(new Float32Array(aCat), 1));
    g.setAttribute("aLook", new BufferAttribute(new Float32Array(aLook), 1));
    const inkA = N > 500 ? 0.06 : 0.22, flagA = N > 500 ? 0.3 : 0.6;
    this.fanU = {
      uFan: { value: this.view.az === 0 ? 0 : 1 }, uLook: { value: looks }, uK: { value: K }, uFilter: { value: 0 },
      uInk: { value: t.ink }, uFlag: { value: t.flagsmith }, uCon: { value: t.contract },
      uA: { value: new Vector3(inkA, flagA, 0.9) },
    };
    const fanVS = `
      attribute float aZ; attribute float aPhi; attribute float aCat; attribute float aLook;
      uniform float uFan; uniform float uLook; uniform float uK; uniform float uFilter;
      uniform vec3 uInk; uniform vec3 uFlag; uniform vec3 uCon; uniform vec3 uA;
      varying vec4 vC;
      void main() {
        float ph = mix(1.5707963, aPhi, uFan);
        vec3 d = vec3(0.0, sin(ph), -cos(ph));
        vec3 p = vec3(position.x, 0.0, 0.0) + d * uK * aZ;
        vec3 c = aCat > 1.5 ? uCon : (aCat > 0.5 ? uFlag : uInk);
        float a = aCat > 1.5 ? uA.z : (aCat > 0.5 ? uA.y : uA.x);
        if (uFilter > 0.5 && uFilter < 1.5 && aCat < 0.5) a = 0.03;
        if (uFilter > 1.5 && aCat < 1.5) a = 0.03;
        if (aLook > uLook + 0.01) a = 0.0;
        vC = vec4(c, a);
        gl_Position = projectionMatrix * modelViewMatrix * vec4(p, 1.0);
      }`;
    this.fan = new LineSegments(g, new ShaderMaterial({
      vertexShader: fanVS, transparent: true, depthWrite: false, toneMapped: false, uniforms: this.fanU,
      fragmentShader: "varying vec4 vC; void main(){ if (vC.a <= 0.0) discard; gl_FragColor = vC; \n#include <colorspace_fragment>\n}",
    }));
    this.fan.frustumCulled = false;
    this.world.add(this.fan);
    const pg = new BufferGeometry();
    pg.setAttribute("position", new BufferAttribute(new Float32Array(pPos), 3));
    pg.setAttribute("aZ", new BufferAttribute(new Float32Array(pZ), 1));
    pg.setAttribute("aPhi", new BufferAttribute(new Float32Array(pPhi), 1));
    pg.setAttribute("aType", new BufferAttribute(new Float32Array(pType), 1));
    pg.setAttribute("aLook", new BufferAttribute(new Float32Array(pLook), 1));
    this.points = new Points(pg, new ShaderMaterial({
      transparent: true, depthTest: false, toneMapped: false,
      uniforms: { ...this.fanU, uPx: { value: 6 * Math.min(devicePixelRatio, 1.75) }, uSheet: { value: t.sheet } },
      vertexShader: `
        attribute float aZ; attribute float aPhi; attribute float aType; attribute float aLook;
        uniform float uFan; uniform float uLook; uniform float uK; uniform float uFilter; uniform float uPx;
        uniform vec3 uFlag; uniform vec3 uCon; varying vec4 vC;
        void main() {
          float ph = mix(1.5707963, aPhi, uFan);
          vec3 p = vec3(position.x, 0.0, 0.0) + vec3(0.0, sin(ph), -cos(ph)) * uK * aZ;
          float a = aLook > uLook + 0.01 ? 0.0 : 1.0;
          if (uFilter > 1.5 && aType < 0.5) a *= 0.15;
          if (uFilter > 0.5 && uFilter < 1.5 && aType > 0.5) a *= 0.15;
          vC = vec4(aType > 0.5 ? uCon : uFlag, a);
          gl_PointSize = uPx;
          gl_Position = projectionMatrix * modelViewMatrix * vec4(p, 1.0);
        }`,
      fragmentShader: `uniform vec3 uSheet; varying vec4 vC; void main(){ vec2 q = gl_PointCoord - 0.5; float r = length(q); if (r > 0.5 || vC.a <= 0.0) discard; vec3 c = r > 0.36 ? uSheet : vC.rgb; gl_FragColor = vec4(c, vC.a); \n#include <colorspace_fragment>\n}`,
    }));
    this.points.frustumCulled = false;
    this.points.renderOrder = 5;
    this.world.add(this.points);
  }

  // ---- per-frame placement -----------------------------------------------------------------------
  placeBeads(progress) {
    // progress: number of thread segments drawn (fractional). Beads up to the drawn end are visible.
    const e = this.e;
    const segs = this.threadSegs;
    const full = this.threadFull;
    const geo = this.thread.geometry;
    if (segs > 0) {
      const whole = Math.floor(progress), frac = progress - whole;
      const arr = geo.attributes.instanceStart.data.array;
      arr.set(full);
      if (whole < segs && frac > 0) {
        const o = whole * 6;
        arr[o + 3] = lerp(full[o], full[o + 3], frac);
        arr[o + 4] = lerp(full[o + 1], full[o + 4], frac);
      }
      geo.attributes.instanceStart.data.needsUpdate = true;
      geo.instanceCount = Math.min(segs, whole + (frac > 0 ? 1 : 0));
      this.thread.visible = geo.instanceCount > 0;
    }
    const xEnd = segs > 0 ? (() => {
      const whole = Math.min(segs - 1, Math.floor(progress));
      const o = whole * 6;
      return progress >= segs ? Infinity : lerp(full[o], full[o + 3], progress - Math.floor(progress)) + 1e-6;
    })() : Infinity;
    const o = new Object3D();
    const q = this.camera.quaternion;
    let n = 0;
    for (const b of this.beadList) {
      if (b.x > xEnd + 1e-6) break;
      o.position.set(b.x, b.y, 0);
      o.quaternion.identity();
      o.scale.setScalar(this.highlighted.seq === b.seq ? 1.9 : 1);
      o.updateMatrix();
      this.beads.setMatrixAt(n++, o.matrix);
    }
    this.beads.count = n;
    this.beads.instanceMatrix.needsUpdate = true;
    n = 0;
    for (const b of this.hollowList) {
      if (b.x > xEnd + 1e-6 && b.kind !== "tick") continue;
      o.position.set(b.x, b.y, 0.001);
      o.quaternion.copy(q);
      o.scale.setScalar((b.kind === "tick" ? 1 : 1) * (this.highlighted.seq === b.seq ? 1.7 : 1));
      o.updateMatrix();
      this.hollow.setMatrixAt(n++, o.matrix);
    }
    this.hollow.count = n;
    this.hollow.instanceMatrix.needsUpdate = true;
    const showCur = progress >= segs;
    if (this.current) {
      const y = this.needleY;
      this.curBead.position.set(this.current.x, y, 0);
      this.halo.position.set(this.current.x, y, 0.002);
      this.halo.quaternion.copy(q);
      this.curBead.visible = this.halo.visible = this.needle.visible = showCur;
      const arr = this.needle.geometry.attributes.instanceStart.data.array;
      arr[4] = y;
      this.needle.geometry.attributes.instanceStart.data.needsUpdate = true;
    }
  }

  // ---- camera ---------------------------------------------------------------------------------------
  resize() {
    const w = Math.max(1, this.stage.clientWidth), h = Math.max(1, this.stage.clientHeight);
    let dpr = Math.min(devicePixelRatio || 1, 1.75);
    if (w * h * dpr * dpr > 1.2e6 || innerWidth < 640) dpr = Math.min(dpr, 1.25);
    this.dpr = dpr;
    this.renderer.setPixelRatio(dpr);
    this.renderer.setSize(w, h, false);
    this.w = w; this.h = h;
    for (const l of [this.edge, this.bannerLines, this.thread, this.needle, this.dim, this.nowLine]) if (l) l.material.resolution.set(w, h);
    this.placeCamera();
  }

  placeCamera() {
    const { az, el } = this.view;
    const dir = new Vector3(Math.sin(az) * Math.cos(el), Math.sin(el), Math.cos(az) * Math.cos(el));
    const c = this.camera;
    c.position.copy(dir).multiplyScalar(30);
    c.up.set(0, 1, 0);
    c.lookAt(0, 0, 0);
    c.updateMatrixWorld();
    // fit the box for this orientation; in Read view this is exactly the SVG viewBox fit
    const right = new Vector3().setFromMatrixColumn(c.matrixWorld, 0), up = new Vector3().setFromMatrixColumn(c.matrixWorld, 1);
    const hx = BOX_W / 2, hy = BOX_H / 2, hz = K * Z_MAX;
    let mw = 0, mh = 0;
    for (const sx of [-1, 1]) for (const sy of [-1, 1]) for (const sz of [-1, 1]) {
      const p = new Vector3(sx * hx, sy * hy, sz * (az === 0 && el === 0 ? 0 : hz));
      mw = Math.max(mw, Math.abs(p.dot(right))); mh = Math.max(mh, Math.abs(p.dot(up)));
    }
    const f = fitFrustum(this.w / this.h, 2 * mw * (1 + MARGIN), 2 * mh * (1 + MARGIN));
    c.left = -f.halfW; c.right = f.halfW; c.top = f.halfH; c.bottom = -f.halfH;
    c.near = 0.1; c.far = 100;
    c.updateProjectionMatrix();
  }

  project(p) {
    const v = new Vector3(p[0], p[1], p[2] || 0).project(this.camera);
    return [(v.x * 0.5 + 0.5) * this.w, (-v.y * 0.5 + 0.5) * this.h];
  }

  setView(name) {
    const to = name === "read" ? READ : INSPECT;
    const from = { ...this.view };
    const fanFrom = this.fanU ? this.fanU.uFan.value : 0;
    const fanTo = name === "read" ? 0 : 1;
    this.tween(this.opts.reduced ? 0 : 500, (k) => {
      const s = easeSnap(k);
      this.view.az = lerp(from.az, to.az, s);
      this.view.el = lerp(from.el, to.el, s);
      if (this.fanU) this.fanU.uFan.value = lerp(fanFrom, fanTo, s);
      if (k >= 1) { this.view.az = to.az; this.view.el = to.el; }
      this.placeCamera();
    }, "view");
  }

  // ---- animation ------------------------------------------------------------------------------------
  tween(ms, fn, key) {
    if (key) this.tweens = this.tweens.filter((t) => t.key !== key);
    return new Promise((resolve) => {
      if (ms <= 0) { fn(1); resolve(); this.invalidate(); return; }
      this.tweens.push({ start: performance.now(), ms, fn, resolve, key });
      this.invalidate();
    });
  }

  intro() {
    const sc = this.scene, ra = this.ra;
    const finalProgress = this.threadSegs;
    const chipsOn = () => this.labels(true);
    const skipAll = this.opts.reduced || this.scene.kind === "calibration";
    if (skipAll) {
      this.progress = finalProgress;
      this.placeBeads(finalProgress);
      chipsOn();
      this.introDone = true;
      this.ready();
      return;
    }
    this.labels(false);
    const n = this.ringList.length;
    const stagger = Math.min(16, 480 / Math.max(n, 1));
    const counted = (sc.history || []).filter((h) => h.z !== null && h.z !== undefined).length;
    const per = counted > 10 ? 40 : 120;
    const arrived = this.opts.arrived;
    const startProgress = arrived ? Math.max(0, finalProgress - 1) : 0;
    for (let i = 0; i < n; i++) ra.fade.array[i] = arrived ? 1 : 0;
    ra.fade.needsUpdate = true;
    const castTarget = sc.signed && !sc.wire ? 1 : 0;
    this.mats.shell.uniforms.uCast.value = arrived ? castTarget : 0;
    this.needleY = 0;
    this.progress = startProgress;
    this.placeBeads(startProgress);
    this.ready();
    const t0 = arrived ? 0 : 900;
    const threadMs = arrived ? 600 : per * Math.max(finalProgress, 0);
    const steps = [];
    if (!arrived) {
      steps.push(this.tween(480 + 160, (k) => {
        const tms = k * 640;
        for (let i = 0; i < n; i++) ra.fade.array[i] = clamp01((tms - i * stagger) / 160);
        ra.fade.needsUpdate = true;
      }, "lash"));
      if (castTarget) steps.push(this.delay(300).then(() => this.tween(900, (k) => { this.mats.shell.uniforms.uCast.value = easeOut(k); }, "cast")));
    }
    this.introDone = false;
    const thread = this.delay(t0).then(() => this.tween(threadMs, (k) => {
      this.progress = lerp(startProgress, finalProgress, arrived ? easeOut(k) : k);
      this.placeBeads(this.progress);
    }, "thread"));
    const after = thread.then(() => {
      this.springing = !!this.current;
      this.needleV = 0;
      this.invalidate();
      return this.delay(600);
    }).then(() => {
      chipsOn();
      this.introDone = true;
      if (!this.opts.phone && this.plate.wantsView === "inspect") this.setView("inspect");
    });
    this.view = { ...READ };
    this.placeCamera();
    const skip = () => this.skipIntro();
    for (const ev of ["keydown", "pointerdown", "wheel"]) addEventListener(ev, skip, { once: true, passive: true });
    this.introPromise = Promise.all([...steps, after]);
  }

  skipIntro() {
    if (!this.started) { this.started = true; this.intro(); }
    if (this.introDone) return;
    this.introDone = true;
    for (const tw of this.tweens) { tw.fn(1); tw.resolve(); }
    this.tweens = [];
    const ra = this.ra;
    for (let i = 0; i < ra.fade.array.length; i++) ra.fade.array[i] = 1;
    ra.fade.needsUpdate = true;
    this.mats.shell.uniforms.uCast.value = this.scene.signed && !this.scene.wire ? 1 : 0;
    this.progress = this.threadSegs;
    this.springing = false;
    if (this.current) this.needleY = this.current.y;
    this.placeBeads(this.progress);
    this.labels(true);
    if (!this.opts.phone && this.plate.wantsView === "inspect") { this.view = { ...INSPECT }; this.placeCamera(); }
    this.invalidate();
  }

  delay(ms) { return new Promise((r) => setTimeout(r, ms)); }

  ready() {
    this.invalidate();
  }

  labels(on) {
    const chips = this.plate.chips;
    labelScene(this.scene, this.e, chips);
    for (const id of chips.items.keys()) { if (on && id !== "leader") chips.show(id); else chips.hide(id); }
    this.chipsOn = on;
    this.invalidate();
  }

  // the scene acts only after the server has answered
  cast() {
    this.skipIntro();
    if (!this.shell) return Promise.resolve();
    this.mats.shell.uniforms.uCast.value = 0;
    return this.tween(this.opts.reduced ? 0 : 900, (k) => { this.mats.shell.uniforms.uCast.value = easeOut(k); }, "cast");
  }

  release() {
    this.skipIntro();
    const i = this.ringList.findIndex((r) => (r.state === S.WIN || r.state === S.LOSS) && !r.open && r.side !== 0 && r.state !== S.GHOST);
    const idx = i >= 0 ? i : this.ringList.findIndex((r) => r.state === S.WIN || r.state === S.LOSS);
    if (idx < 0) return Promise.resolve();
    const r = this.ringList[idx];
    if (!r.side) { r.side = r.state === S.WIN ? 1 : -1; this.ra.side.array[idx] = r.side; this.ra.side.needsUpdate = true; }
    const ms = this.opts.reduced ? 0 : 700;
    return this.tween(ms, (k) => { this.ra.open.array[idx] = easeOut(k); this.ra.open.needsUpdate = true; }, "open")
      .then(() => this.tween(this.opts.reduced ? 0 : 400, (k) => {
        this.mats.shell.uniforms.uTint.value = k;
        this.mats.shell.uniforms.uTintCol.value = r.state === S.WIN ? this.tok.win : this.tok.loss;
      }, "tint"));
  }

  // ---- highlight / pick -------------------------------------------------------------------------------
  highlight(seq) {
    this.highlighted.seq = seq;
    const h = (this.scene.history || []).find((x) => x.seq === seq);
    const k = h && h.counted ? h.look_number : null;
    this.setRingHighlight(k);
    const chips = this.plate.chips;
    if (h && h.z !== null && h.z !== undefined) {
      const b = this.e.beads.find((x) => x.seq === seq);
      const txt = `${h.look_number ? "Look " + h.look_number : "Refresh " + h.seq + " (not a look)"} · ${Number(h.n).toLocaleString("en")} per arm · Flagsmith ${h.ctw}${h.needs ? " · needs " + h.needs + " (z " + h.bz_text + ")" : ""}`;
      chips.set("leader", txt, [b.x, b.y, 0], { cls: "leader", align: b.y > 0 ? "below" : "above", dy: b.y > 0 ? 8 : -8, prio: 0 });
    } else chips.hide("leader");
    this.placeBeads(this.progress ?? this.threadSegs);
    this.invalidate();
  }

  setRingHighlight(k) {
    this.highlighted.k = k;
    const a = this.ra.hi.array;
    this.ringList.forEach((r, i) => { a[i] = k !== null && r.k === k && r.state !== S.GHOST ? 1 : 0; });
    this.ra.hi.needsUpdate = true;
  }

  highlightRing(k, leader) {
    this.setRingHighlight(k);
    const chips = this.plate.chips;
    if (k !== null && leader) {
      const r = this.ringList.find((x) => x.k === k && x.state !== S.GHOST);
      chips.set("leader", leader, [r.x, r.r, 0], { cls: "leader", align: "above", dy: -6, prio: 0 });
      chips.hide("first"); chips.hide("last");
    } else {
      chips.hide("leader");
      if (this.chipsOn) { chips.show("first"); chips.show("last"); }
    }
    this.invalidate();
  }

  pick(clientX, clientY) {
    const rect = this.canvas.getBoundingClientRect();
    const px = clientX - rect.left, py = clientY - rect.top;
    let best = null, bd = 10;
    for (const b of this.e.beads) {
      if (b.kind === "tick") continue;
      const [sx, sy] = this.project([b.x, b.y, 0]);
      const d = Math.hypot(sx - px, sy - py);
      if (d < bd) { bd = d; best = { type: "bead", seq: b.seq }; }
    }
    if (best) return best;
    bd = 6;
    for (const r of this.ringList) {
      if (r.state === S.GHOST || r.k < 0) continue;
      for (let i = 0; i < 36; i++) {
        const a = (i / 36) * TAU;
        const [sx, sy] = this.project([r.x, r.r * Math.sin(a), r.r * Math.cos(a)]);
        const d = Math.hypot(sx - px, sy - py);
        if (d < bd) { bd = d; best = { type: "ring", k: r.k }; }
      }
    }
    return best;
  }

  // ---- input --------------------------------------------------------------------------------------------
  bindPointer() {
    const c = this.canvas;
    let drag = null;
    const canOrbit = () => !this.opts.phone && innerWidth >= 640 && this.scene.kind !== "example";
    if (canOrbit()) this.plate.fig.dataset.drag = "true";
    c.addEventListener("pointerdown", (ev) => {
      if (!canOrbit() || ev.button !== 0) return;
      drag = { x: ev.clientX, y: ev.clientY, moved: false };
      c.setPointerCapture(ev.pointerId);
    });
    c.addEventListener("pointermove", (ev) => {
      if (drag) {
        const dx = ev.clientX - drag.x, dy = ev.clientY - drag.y;
        if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
        drag.x = ev.clientX; drag.y = ev.clientY;
        this.orbitVel.az = -dx * 0.006; this.orbitVel.el = dy * 0.006;
        this.applyOrbit();
        return;
      }
      const hit = this.pick(ev.clientX, ev.clientY);
      c.style.cursor = hit ? "pointer" : "";
    });
    c.addEventListener("pointerup", (ev) => {
      if (drag && !drag.moved) this.click(ev);
      if (drag && drag.moved) { this.inertia = true; this.invalidate(); }
      drag = null;
    });
    c.addEventListener("click", (ev) => { if (!canOrbit()) this.click(ev); });
    c.addEventListener("dblclick", () => { if (canOrbit()) this.plate.setView("inspect"); });
  }

  click(ev) {
    const hit = this.pick(ev.clientX, ev.clientY);
    if (!hit) return;
    if (hit.type === "bead") this.plate.emit("pick", hit.seq);
    else this.plate.emit("pick-ring", hit.k);
  }

  applyOrbit() {
    this.view.az = Math.max(-35 * DEG, Math.min(35 * DEG, this.view.az + this.orbitVel.az));
    this.view.el = Math.max(0, Math.min(28 * DEG, this.view.el + this.orbitVel.el));
    if (this.fanU) this.fanU.uFan.value = 1;
    this.placeCamera();
    this.invalidate();
  }

  bindVisibility() {
    new IntersectionObserver((entries) => {
      this.visible = entries[0].isIntersecting;
      if (this.visible) { this.maybeStart(); this.invalidate(); }
    }, { threshold: 0.01 }).observe(this.stage);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) this.invalidate(true); });
  }

  bindTheme() {
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => this.retheme());
    new MutationObserver(() => this.retheme()).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  }

  retheme() {
    const t = (this.tok = readTokens());
    this.world.background = t.sheet.clone();
    this.mats.ink.color.copy(t.ink);
    this.mats.hollow.color.copy(t.ink);
    this.mats.banner.color.copy(t.flagsmith);
    this.mats.shadow.color.copy(t.ink);
    this.mats.ring.uniforms.uCol.value = this.ringColors();
    this.mats.ring.uniforms.uHair.value = t.hairline;
    this.mats.ring.uniforms.uSheet.value = t.sheet;
    const su = this.mats.shell.uniforms;
    su.uOut.value = t.enamel; su.uIn.value = t["enamel-in"]; su.uLine.value = t.contract; su.uGround.value = t["enamel-in"];
    if (this.edge) this.edge.material.color.copy(t.contract);
    if (this.bannerLines) this.bannerLines.material.color.copy(t.flagsmith);
    for (const l of [this.thread, this.needle, this.dim]) if (l) l.material.color.copy(t.ink);
    if (this.arrows) this.arrows.material.color.copy(t.ink);
    if (this.nowLine) this.nowLine.material.color.copy(t.amber);
    if (this.fanU) { this.fanU.uInk.value = t.ink; this.fanU.uFlag.value = t.flagsmith; this.fanU.uCon.value = t.contract; }
    if (this.points) this.points.material.uniforms.uSheet.value = t.sheet;
    this.invalidate();
  }

  onLost(ev) {
    ev.preventDefault();
    this.lost++;
    this.stage.dataset.ready = "svg";
    this.canvas.style.visibility = "hidden";
    note(this.plate, "3D view paused: showing the flat drawing.");
  }

  onRestored() {
    if (this.lost > 1) return;
    this.canvas.style.visibility = "";
    this.stage.dataset.ready = "gl";
    this.stage.querySelector(".plate-note")?.remove();
    this.invalidate(true);
  }

  // ---- loop: render on demand only ------------------------------------------------------------------------
  invalidate(force = false) {
    this.dirty = true;
    if (force) this.visible = true;
    if (!this.running && this.visible && !document.hidden) {
      this.running = true;
      this.last = performance.now();
      requestAnimationFrame((t) => this.frame(t));
    }
  }

  frame(now) {
    const dt = Math.min(0.05, (now - this.last) / 1000);
    this.last = now;
    let active = false;
    // tweens
    const keep = [];
    for (const tw of this.tweens) {
      const k = clamp01((now - tw.start) / tw.ms);
      tw.fn(k);
      if (k >= 1) tw.resolve(); else keep.push(tw);
    }
    this.tweens = keep;
    if (keep.length) active = true;
    // needle spring: omega 14 rad/s, zeta 0.72
    if (this.springing && this.current) {
      const w = 14, z = 0.72, target = this.current.y;
      const a = -2 * z * w * this.needleV - w * w * (this.needleY - target);
      this.needleV += a * dt;
      this.needleY += this.needleV * dt;
      if (Math.abs(this.needleY - target) < 1e-4 && Math.abs(this.needleV) < 1e-3) { this.needleY = target; this.springing = false; }
      this.placeBeads(this.progress ?? this.threadSegs);
      active = active || this.springing;
    }
    // orbit inertia (damping 0.12)
    if (this.inertia) {
      this.orbitVel.az *= 0.88; this.orbitVel.el *= 0.88;
      if (Math.abs(this.orbitVel.az) + Math.abs(this.orbitVel.el) < 1e-4) this.inertia = false;
      else { this.applyOrbit(); active = true; }
    }
    if (this.dirty || active) {
      if (this.hollow && this.hollowList.length) this.placeBeads(this.progress ?? this.threadSegs);
      const t0 = performance.now();
      this.renderer.render(this.world, this.camera);
      if (this.stage.dataset.ready !== "gl" && this.lost === 0) this.stage.dataset.ready = "gl";  // swap the SVG out only after a real frame
      this.plate.chips.place((p) => this.project(p), this.w, this.h);
      this.dirty = false;
      this.frames++;
      if (active && this.frameTimes.length < 30) {
        this.frameTimes.push(now - (this.prevFrame || now));
        if (this.frameTimes.length === 30) this.checkSpeed();
      }
      this.prevFrame = now;
      if (this.debugEl) this.debug(performance.now() - t0);
    }
    if ((active || this.dirty) && this.visible && !document.hidden) requestAnimationFrame((t) => this.frame(t));
    else { this.running = false; this.prevFrame = 0; }
  }

  checkSpeed() {
    const avg = this.frameTimes.slice(1).reduce((a, b) => a + b, 0) / (this.frameTimes.length - 1);
    if (avg > 24 && !this.opts.debug && !this.opts.forceGL) {
      rememberLowPower();
      this.plate.fig.dataset.flat = "true";
      this.canvas.style.display = "none";
      this.stage.dataset.ready = "svg";
      note(this.plate, "Showing the flat drawing: this device was drawing slowly.");
      this.plate.placeFlat?.();
    }
  }

  makeDebug() {
    const d = document.createElement("pre");
    d.style.cssText = "position:absolute;left:8px;bottom:8px;z-index:4;margin:0;padding:4px 6px;font:11px/14px var(--f-mono);background:var(--sheet);border:1px solid var(--rule);color:var(--ink)";
    d.setAttribute("aria-hidden", "true");
    d.dataset.debug = "1";
    this.stage.appendChild(d);
    window.__ulyssesInstrument = this;
    return d;
  }

  debug(ms) {
    const i = this.renderer.info.render;
    this.debugEl.textContent = `calls ${i.calls}  tris ${i.triangles}  frame ${ms.toFixed(1)}ms  frames ${this.frames}  dpr ${this.dpr}`;
  }
}

export function mountInstrument(plate, opts) {
  const inst = new Instrument(plate, opts);
  return {
    setView: (v) => inst.setView(v),
    highlight: (seq) => inst.highlight(seq),
    highlightRing: (k, text) => inst.highlightRing(k, text),
    cast: () => inst.cast(),
    release: () => inst.release(),
    setFan: (opt) => {
      if (!inst.fanU) return;
      if ("look" in opt) inst.fanU.uLook.value = opt.look;
      if ("filter" in opt) inst.fanU.uFilter.value = opt.filter;
      inst.invalidate();
    },
    inst,
  };
}
