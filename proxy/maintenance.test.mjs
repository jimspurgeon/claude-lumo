// Run: node --test proxy/maintenance.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, rmSync, writeFileSync, existsSync, statSync, utimesSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { rotateLog, pruneDir } from "./maintenance.mjs";

let dir;
test.beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), "lumo-maintenance-"));
});
test.afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
});

const setMtime = (path, daysAgo) => {
  const t = new Date(Date.now() - daysAgo * 86400 * 1000);
  utimesSync(path, t, t);
};

test("rotateLog renames a log over the threshold to .1", () => {
  const log = join(dir, "proxy.log");
  writeFileSync(log, "x".repeat(200));
  writeFileSync(log + ".1", "old generation");
  assert.equal(rotateLog(log, 100), true);
  assert.equal(existsSync(log), false);
  assert.equal(statSync(log + ".1").size, 200); // old .1 overwritten
});

test("rotateLog leaves a log under the threshold untouched", () => {
  const log = join(dir, "proxy.log");
  writeFileSync(log, "small");
  assert.equal(rotateLog(log, 1000), false);
  assert.equal(existsSync(log), true);
  assert.equal(existsSync(log + ".1"), false);
});

test("rotateLog does not throw on a missing log", () => {
  assert.doesNotThrow(() => rotateLog(join(dir, "nope.log"), 10));
  assert.equal(rotateLog(join(dir, "nope.log"), 10), false);
});

test("pruneDir deletes the oldest entries down to maxFiles", () => {
  for (const i of [0, 1, 2, 3]) {
    const f = join(dir, `dump-${i}.txt`);
    writeFileSync(f, "x");
    setMtime(f, 40 - i); // dump-0 oldest ... dump-3 newest
  }
  assert.equal(pruneDir(dir, 2), 2);
  const left = ["dump-0", "dump-1", "dump-2", "dump-3"]
    .filter((n) => existsSync(join(dir, `${n}.txt`)));
  assert.deepEqual(left, ["dump-2", "dump-3"]);
});

test("pruneDir leaves a directory at or under the cap untouched", () => {
  writeFileSync(join(dir, "a.txt"), "x");
  mkdirSync(join(dir, "sub"));
  writeFileSync(join(dir, "sub", "b.txt"), "y");
  assert.equal(pruneDir(dir, 2), 0);
  assert.ok(existsSync(join(dir, "a.txt")));
  assert.ok(existsSync(join(dir, "sub", "b.txt")));
});

test("pruneDir does not throw on a missing directory", () => {
  assert.doesNotThrow(() => pruneDir(join(dir, "nope"), 5));
  assert.equal(pruneDir(join(dir, "nope"), 5), 0);
});
