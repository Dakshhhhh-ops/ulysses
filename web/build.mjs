// Build the instrument bundle and copy the fonts into the Python package.
//
//   cd web && npm ci && npm run build
//
// Output (committed, so `uv run ulysses serve` needs no Node and no network):
//   src/ulysses/service/static/dist/instrument.js (+ one lazily loaded chunk)
//   src/ulysses/service/static/dist/BUILD        (hash of the sources it was built from)
//   src/ulysses/service/static/fonts/*.woff2 and their OFL licences
// The build fails if the gzip size of the JavaScript is over the 190 KB budget.

import { build } from "esbuild";
import { createHash } from "node:crypto";
import { gzipSync } from "node:zlib";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
const pkg = path.resolve(here, "..", "src", "ulysses", "service");
const outdir = path.join(pkg, "static", "dist");
const fontdir = path.join(pkg, "static", "fonts");
const BUDGET = 190 * 1024;

fs.rmSync(outdir, { recursive: true, force: true });
fs.mkdirSync(outdir, { recursive: true });
fs.mkdirSync(fontdir, { recursive: true });

await build({
  entryPoints: [path.join(here, "src", "instrument.js")],
  bundle: true,
  splitting: true,
  format: "esm",
  minify: true,
  treeShaking: true,
  target: ["es2020"],
  outdir,
  chunkNames: "[name]-[hash]",
  legalComments: "none",
  logLevel: "warning",
});

let total = 0;
const report = [];
for (const f of fs.readdirSync(outdir).filter((f) => f.endsWith(".js"))) {
  const buf = fs.readFileSync(path.join(outdir, f));
  const gz = gzipSync(buf, { level: 9 }).length;
  total += gz;
  report.push(`${f.padEnd(28)} ${String(buf.length).padStart(8)} B  ${String(gz).padStart(7)} B gzip`);
}
console.log(report.join("\n"));
console.log(`total gzip ${(total / 1024).toFixed(1)} KB (budget ${BUDGET / 1024} KB)`);
if (total > BUDGET) {
  console.error("over budget: the instrument bundle must stay at or under 190 KB gzip");
  process.exit(1);
}

// fonts: Latin subsets only, with each family's OFL licence
const fonts = [
  ["@fontsource/ibm-plex-sans-condensed", ["ibm-plex-sans-condensed-latin-500-normal.woff2", "ibm-plex-sans-condensed-latin-600-normal.woff2"], "OFL-IBM-Plex.txt"],
  ["@fontsource/ibm-plex-mono", ["ibm-plex-mono-latin-400-normal.woff2", "ibm-plex-mono-latin-500-normal.woff2"], null],
  ["@fontsource-variable/source-serif-4", ["source-serif-4-latin-opsz-normal.woff2"], "OFL-Source-Serif-4.txt"],
];
for (const [name, files, licence] of fonts) {
  const dir = path.join(here, "node_modules", ...name.split("/"));
  for (const f of files) fs.copyFileSync(path.join(dir, "files", f), path.join(fontdir, f));
  if (licence) fs.copyFileSync(path.join(dir, "LICENSE"), path.join(fontdir, licence));
}

// record what the bundle was built from, so a stale bundle is caught by the Python tests
const h = createHash("sha256");
const walk = (d) => {
  for (const e of fs.readdirSync(d, { withFileTypes: true }).sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0))) {
    const p = path.join(d, e.name);
    if (e.isDirectory()) walk(p);
    else if (/\.(js|mjs|json)$/.test(e.name) && !e.name.endsWith(".test.js")) {
      h.update(path.relative(here, p).split(path.sep).join("/"));
      h.update(fs.readFileSync(p).toString("utf8").replace(/\r\n/g, "\n"));
    }
  }
};
walk(path.join(here, "src"));
h.update("elevation_constants.json");
h.update(fs.readFileSync(path.join(pkg, "elevation_constants.json")).toString("utf8").replace(/\r\n/g, "\n"));
fs.writeFileSync(path.join(outdir, "BUILD"), `sources-sha256 ${h.digest("hex")}\n`);
