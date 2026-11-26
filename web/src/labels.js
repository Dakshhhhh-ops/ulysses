// Chips over the plate and the labels both drawings share.
import { fitFrustum } from "./elevation.js";

const LOWPOWER_KEY = "ulysses.flat-drawing";

export function lowPowerRemembered() {
  try { return localStorage.getItem(LOWPOWER_KEY) === "1"; } catch { return false; }
}
export function rememberLowPower() {
  try { localStorage.setItem(LOWPOWER_KEY, "1"); } catch { /* storage unavailable */ }
}

// ---- chips over the plate (also used by the flat drawing) ----------------------------------
export class Chips {
  constructor(layer) { this.layer = layer; this.items = new Map(); }
  set(id, html, anchor, opts = {}) {
    let it = this.items.get(id);
    if (!it) {
      const el = document.createElement("div");
      el.className = "chip " + (opts.cls || "");
      this.layer.appendChild(el);
      it = { el };
      this.items.set(id, it);
    }
    if (it.html !== html) { it.el.innerHTML = html; it.html = html; }
    it.anchor = anchor; it.align = opts.align || "right"; it.dy = opts.dy || 0; it.dx = opts.dx || 0;
    it.prio = opts.prio ?? 5;
    it.dock = opts.dock || null;   // "top" | "bottom": sit in clear paper off the drawing, with a leader line
    it.el.classList.toggle("docked", !!it.dock);
    it.wanted = opts.show !== false;
    it.el.classList.toggle("on", it.wanted);
    return it;
  }
  hide(id) { const it = this.items.get(id); if (it) { it.wanted = false; it.el.classList.remove("on"); } }
  show(id) { const it = this.items.get(id); if (it) { it.wanted = true; it.el.classList.add("on"); } }
  remove(id) { const it = this.items.get(id); if (it) { it.el.remove(); this.items.delete(id); } }
  // Place every chip at its projected anchor; higher priority first. A chip that would
  // overlap one already placed is nudged up or down; if it still collides it steps aside.
  place(project, w, h) {
    const placed = [];
    // the plate's own controls (Inspect / Read, Turn) sit over the stage: chips keep clear of them
    const stage = this.layer.parentElement;
    const ctrl = stage?.closest(".plate-stage-wrap")?.querySelector(".plate-controls");
    if (ctrl && ctrl.offsetParent) {
      const a = stage.getBoundingClientRect(), b = ctrl.getBoundingClientRect();
      placed.push({ x: b.left - a.left, y: b.top - a.top, w: b.width, h: b.height });
    }
    const hit = (r) => placed.some((p) => r.x < p.x + p.w + 4 && r.x + r.w + 4 > p.x && r.y < p.y + p.h + 4 && r.y + r.h + 4 > p.y);
    // where the drawing is on screen: docked labels stay out of this box
    let top = h, bottom = 0;
    for (const q of this.extent || []) { const [, y] = project(q); top = Math.min(top, y); bottom = Math.max(bottom, y); }
    if (top > bottom) { top = h / 2; bottom = h / 2; }
    const leaders = [];
    const items = [...this.items.values()].filter((it) => it.anchor).sort((a, b) => a.prio - b.prio);
    for (const it of items) {
      const [sx, sy] = project(it.anchor);
      const bw = it.el.offsetWidth, bh = it.el.offsetHeight;
      if (it.dock) {
        const clampX = (x) => Math.max(6, Math.min(w - bw - 6, x));
        const rows = it.dock === "top"
          ? [top - bh - 10, top - 2 * bh - 16, 6].map((y) => Math.max(6, y))
          : [bottom + 10, bottom + bh + 16, h - bh - 6].map((y) => Math.min(h - bh - 6, y));
        let r = null;
        for (const y of rows) {
          for (const dx of [0, -(bw / 2 + 8), bw / 2 + 8, -(bw + 16), bw + 16]) {
            const c = { x: clampX(sx - bw / 2 + dx), y, w: bw, h: bh };
            if (!hit(c)) { r = c; break; }
          }
          if (r) break;
        }
        if (!r || !it.wanted) { if (!r) it.el.classList.remove("on"); if (r) it.el.style.transform = `translate(${Math.round(r.x)}px, ${Math.round(r.y)}px)`; continue; }
        it.el.classList.add("on");
        placed.push(r);
        it.el.style.transform = `translate(${Math.round(r.x)}px, ${Math.round(r.y)}px)`;
        const ex = Math.max(r.x + 6, Math.min(r.x + bw - 6, sx));
        const ey = it.dock === "top" ? r.y + bh : r.y;
        leaders.push(`<line x1="${ex.toFixed(1)}" y1="${ey.toFixed(1)}" x2="${sx.toFixed(1)}" y2="${sy.toFixed(1)}"/><circle cx="${sx.toFixed(1)}" cy="${sy.toFixed(1)}" r="2.5"/>`);
        continue;
      }
      const at = (align) => {
        let x = sx + it.dx, y = sy - bh / 2 + it.dy;
        if (align === "left") x = sx - bw - it.dx;
        if (align === "center") x = sx - bw / 2;
        if (align === "above") { x = sx - bw / 2; y = sy - bh - 10 + it.dy; }
        if (align === "below") { x = sx - bw / 2; y = sy + 10 + it.dy; }
        return { x, y };
      };
      const fit = (p) => ({ x: Math.max(6, Math.min(w - bw - 6, p.x)), y: Math.max(6, Math.min(h - bh - 6, p.y)), w: bw, h: bh });
      const clean = (p) => p.x >= 6 && p.x + bw <= w - 6 && p.y >= 6 && p.y + bh <= h - 6;
      // the requested side first; if the frame would push it onto its own anchor, try the other sides
      const order = [it.align, ...(it.align === "right" || it.align === "left" ? [it.align === "right" ? "left" : "right", "above", "below"] : [])];
      const raw = order.map(at);
      let r = fit(raw.find((p) => clean(p) && !hit(fit(p))) || raw[0]);
      if (it.wanted && hit(r)) {
        const tries = [-(bh + 6), bh + 6, -2 * (bh + 6), 2 * (bh + 6)].map((d) => fit({ x: r.x, y: r.y + d }));
        r = tries.find((t) => !hit(t)) || null;
      }
      if (!r) { it.el.classList.remove("on"); continue; }
      if (it.wanted) { it.el.classList.add("on"); placed.push(r); }
      it.el.style.transform = `translate(${Math.round(r.x)}px, ${Math.round(r.y)}px)`;
    }
    if (!this.leaderSvg) {
      this.leaderSvg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      this.leaderSvg.setAttribute("class", "leaders");
      this.layer.prepend(this.leaderSvg);
    }
    const html = leaders.join("");
    if (this.leaderSvg.dataset.html !== html) { this.leaderSvg.innerHTML = html; this.leaderSvg.dataset.html = html; }
    this.leaderSvg.classList.toggle("on", leaders.length > 0 && this.leadersOn !== false);
  }
}

// The front view projection, for the flat drawing (same fit as the SVG viewBox).
export function flatProjector(stage) {
  return (p) => {
    const w = stage.clientWidth, h = stage.clientHeight;
    const f = fitFrustum(w / h);
    return [((p[0] + f.halfW) / (2 * f.halfW)) * w, ((f.halfH - p[1]) / (2 * f.halfH)) * h];
  };
}

// Labels shared by the flat drawing and the 3D instrument.
export function labelScene(scene, e, chips, { flat = false } = {}) {
  const dec = scene.decision;
  const bannerY = e.banner;
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const busy = !!dec;
  chips.extent = [];
  for (const r of e.rings) chips.extent.push([r.x, r.r, 0], [r.x, -r.r, 0], [r.x, 0, r.r], [r.x, 0, -r.r]);
  chips.extent.push([e.mast[0], 0, 0], [e.mast[1], 0, 0], [e.mast[0] + 0.2, e.banner, 0], [e.mast[0] + 0.2, -e.banner, 0]);
  if (scene.kind !== "calibration" && !busy) {
    chips.set("banner", "Flagsmith docs: over 95% reads as Winning", [e.mast[0] + 0.5, -bannerY, 0], { cls: "flag quiet", dock: "bottom", prio: 9 });
  }
  if (dec && dec.ctw !== null && dec.ctw !== undefined) {
    const bead = e.beads.find((b) => b.kind === "current");
    const ring = e.rings.filter((r) => r.state !== 7).find((r) => r.k === dec.look_number);
    if (bead && dec.outcome === "hold" && dec.needs) {
      const top = bead.y >= 0 ? ring?.r ?? 0 : -(ring?.r ?? 0);
      chips.set("dim", `has ${esc(dec.ctw)} · needs ${esc(dec.needs)} (z&nbsp;${esc(dec.bz_text)})`,
        [bead.x + 0.2, (bead.y + top) / 2, 0], { align: "right", dx: 10, prio: 1 });
    }
    if (bead && scene.money) {
      // pinned where the thread first leaves the banner cylinder on its way to this look
      const y = scene.money === "Winning" ? bannerY : -bannerY;
      let cx = bead.x;
      for (const run of e.thread) for (let i = 0; i < run.length - 1; i++) {
        const [x0, y0] = run[i], [x1, y1] = run[i + 1];
        if ((Math.abs(y0) <= bannerY) !== (Math.abs(y1) <= bannerY) && Math.sign(y1) === Math.sign(y)) cx = x0 + ((y - y0) / (y1 - y0)) * (x1 - x0);
      }
      chips.set("money", `<b>Flagsmith docs would call this ${scene.money}.</b> The contract holds.`, [cx, y, 0],
        { cls: "money", dock: scene.money === "Winning" ? "top" : "bottom", prio: 2 });
    }
  }
  if ((scene.kind === "sign" && !scene.proposed) || scene.kind === "example" || scene.kind === "calibration") {
    const fl = scene.first_look ?? (scene.planned || []).find((p) => p.z !== null)?.look;
    const planned = scene.planned || [];
    const first = planned.find((p) => p.look === fl);
    const last = planned[planned.length - 1];
    if (first && scene.kind !== "calibration") {
      const r = e.rings.find((x) => x.k === first.look && x.state !== 7);
      chips.set("first", `earliest stop · look ${first.look} · needs ${esc(first.needs)} (z&nbsp;${esc(first.z_text)})`, [r.x, r.r, 0], { dock: "top", prio: 3 });
    }
    if (last && last.z !== null && scene.kind !== "calibration") {
      const r = e.rings.find((x) => x.k === last.look && x.state !== 7);
      chips.set("last", `last look ${last.look} · needs ${esc(last.needs)} (z&nbsp;${esc(last.z_text)})`, [r.x, -r.r, 0], { dock: "bottom", prio: 3 });
    }
  }
  if (scene.action_now) {
    chips.set("now", `authorises now: ${esc(scene.action_now.replace("_", " "))}`, [e.mast[1] - 0.6, e.zmax * 0.75, 0], { cls: "amber", dock: "top", prio: 4 });
  }
  return chips;
}

export function note(plate, text) {
  let n = plate.stage.querySelector(".plate-note");
  if (!n) { n = document.createElement("p"); n.className = "plate-note"; plate.stage.appendChild(n); }
  n.textContent = text;
}

