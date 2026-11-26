// The JS elevation must draw exactly what the Python one (service/scene.py) draws.
// tests/test_ui.py holds Python to the same fixture. Run: npm test
import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import { elevation, fitFrustum, K, Z_MAX } from "./elevation.js";

const fixture = JSON.parse(fs.readFileSync(new URL("./parity-fixture.json", import.meta.url), "utf8"));

function close(a, b, path = "") {
  if (typeof a === "number" && typeof b === "number") return assert.ok(Math.abs(a - b) <= 1e-6, `${path}: ${a} != ${b}`);
  if (Array.isArray(a)) { assert.equal(a.length, b.length, `${path} length`); return a.forEach((x, i) => close(x, b[i], `${path}[${i}]`)); }
  if (a && typeof a === "object") {
    assert.deepEqual(Object.keys(a).sort(), Object.keys(b).sort(), `${path} keys`);
    for (const k of Object.keys(a)) close(a[k], b[k], `${path}.${k}`);
    return;
  }
  assert.equal(a, b, path);
}

test("JS elevation equals the Python elevation on the shared fixture", () => {
  close(elevation(fixture.scene), fixture.elevation, "elevation");
});

test("front view: half-height of each ring divided by K is its planned z", () => {
  const e = elevation(fixture.scene);
  for (const r of e.rings.filter((r) => r.state !== 7 && !r.applied)) {
    const p = fixture.scene.planned.find((p) => p.look === r.k);
    const z = p.z === null ? Z_MAX : p.z;
    assert.ok(Math.abs(r.r / K - z) < 1e-5, `look ${r.k}`);
  }
});

test("the frustum fit matches SVG preserveAspectRatio meet", () => {
  const box = fixture.elevation.box;
  for (const aspect of [0.6, 1, 1.5625, 2.4]) {
    const f = fitFrustum(aspect);
    const meet = Math.min((2 * f.halfW) / box[2], (2 * f.halfH) / box[3]);
    assert.ok(Math.abs(meet - 1) < 1e-9, `aspect ${aspect}`);
  }
});
