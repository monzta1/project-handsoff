const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "../..");
const fleetHtml = fs.readFileSync(path.join(root, "fleet/index.html"), "utf8");
const fleetApp = fs.readFileSync(path.join(root, "fleet/app.js"), "utf8");
const runHtml = fs.readFileSync(path.join(root, "dashboard/index.html"), "utf8");
const runApp = fs.readFileSync(path.join(root, "dashboard/app.js"), "utf8");

// #149: Fleet's tab icon is its own inline SVG in Fleet's accent, distinct
// from the run dashboard's state-coloured canvas favicon.
test("fleet declares an inline SVG favicon in its accent and never rewrites it", () => {
  const link = fleetHtml.match(/<link rel="icon" type="image\/svg\+xml" href="data:image\/svg\+xml,([^"]+)">/);
  assert.ok(link, "fleet/index.html declares an inline SVG icon");
  const svg = decodeURIComponent(link[1]);
  assert.match(svg, /<svg xmlns='http:\/\/www\.w3\.org\/2000\/svg'/);
  assert.ok((svg.match(/<circle /g) || []).length >= 4, "a constellation of mission dots");
  assert.match(svg, /#5fd3ff/i);
  assert.doesNotMatch(fleetHtml, /href="\/logo\.png"[^>]*rel="icon"/);
  assert.doesNotMatch(fleetApp, /favicon/i);
});

test("the run dashboard keeps its state-coloured canvas favicon", () => {
  assert.match(runHtml, /<link rel="icon" id="favicon" href="">/);
  assert.match(runApp, /const faviconCanvas = document\.createElement\("canvas"\)/);
  assert.match(runApp, /function setFaviconState/);
});
