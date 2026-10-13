// Run: node --test proxy/model-history.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { createModelHistory } from "./model-history.mjs";

const newDir = () => mkdtempSync(join(tmpdir(), "model-history-"));

test("record persists entries atomically; entries() returns a copy", () => {
  const dir = newDir();
  const h = createModelHistory({ file: join(dir, "model-history.json"), maxEntries: 100 });
  h.record({ model: "lumo-lite", t0: 1, tEnd: 2 });
  const entries = h.entries();
  assert.deepEqual(entries, [{ model: "lumo-lite", t0: 1, tEnd: 2 }]);
  entries.push({ model: "fake", t0: 0, tEnd: 0 });
  assert.equal(h.entries().length, 1); // caller mutation must not leak in
  assert.deepEqual(JSON.parse(readFileSync(join(dir, "model-history.json"), "utf8")),
                   [{ model: "lumo-lite", t0: 1, tEnd: 2 }]);
  rmSync(dir, { recursive: true, force: true });
});

test("entries beyond maxEntries are trimmed to the newest", () => {
  const dir = newDir();
  const h = createModelHistory({ file: join(dir, "model-history.json"), maxEntries: 3 });
  for (let i = 0; i < 5; i++) h.record({ model: `m${i}`, t0: i, tEnd: i + 0.5 });
  assert.deepEqual(h.entries(), [
    { model: "m2", t0: 2, tEnd: 2.5 },
    { model: "m3", t0: 3, tEnd: 3.5 },
    { model: "m4", t0: 4, tEnd: 4.5 },
  ]);
  rmSync(dir, { recursive: true, force: true });
});

test("previous entries are loaded from an existing file", () => {
  const dir = newDir();
  const f = join(dir, "model-history.json");
  const prev = [{ model: "prev", t0: 0, tEnd: 1 }];
  writeFileSync(f, JSON.stringify(prev));
  const h = createModelHistory({ file: f });
  assert.deepEqual(h.entries(), prev);
  rmSync(dir, { recursive: true, force: true });
});

test("record with a missing or invalid model is ignored", () => {
  const dir = newDir();
  const h = createModelHistory({ file: join(dir, "model-history.json") });
  h.record({});
  h.record({ model: null, t0: 1, tEnd: 2 });
  h.record({ model: "", t0: 1, tEnd: 2 });
  h.record({ model: 42, t0: 1, tEnd: 2 });
  assert.equal(h.entries().length, 0);
  rmSync(dir, { recursive: true, force: true });
});

test("a corrupt history file loads as empty", () => {
  const dir = newDir();
  const f = join(dir, "model-history.json");
  writeFileSync(f, "not json");
  const h = createModelHistory({ file: f });
  assert.equal(h.entries().length, 0);
  rmSync(dir, { recursive: true, force: true });
});

test("record to an unwritable file does not throw", () => {
  // A file occupies the parent's name, so mkdir of its dirname fails (ENOTDIR).
  const dir = newDir();
  const blocker = join(dir, "blocker");
  writeFileSync(blocker, "");
  const h = createModelHistory({ file: join(blocker, "model-history.json") });
  h.record({ model: "lumo-max", t0: 1, tEnd: 2 }); // must not throw
  assert.equal(h.entries().length, 1); // kept in memory even when persistence fails
  rmSync(dir, { recursive: true, force: true });
});
