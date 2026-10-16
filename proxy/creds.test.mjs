// Run: node --test proxy/creds.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { DatabaseSync } from "node:sqlite";
import { mkdtempSync, mkdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { readLumoCredentials } from "./creds.mjs";

// Builds a Firefox-style profile dir containing a cookies.sqlite with the given rows.
// Columns beyond the ones FF_SELECT needs (name, value, host, lastAccessed) mirror the
// real schema loosely; only the queried ones matter.
function makeFirefoxProfile(rows, { subdir = null } = {}) {
  const root = mkdtempSync(join(tmpdir(), "lumo-creds-test-"));
  const profile = subdir ? (mkdirSync(join(root, subdir), { recursive: true }), join(root, subdir)) : root;
  const dbPath = join(profile, "cookies.sqlite");
  const con = new DatabaseSync(dbPath);
  try {
    con.exec(`CREATE TABLE moz_cookies (
      id INTEGER PRIMARY KEY, name TEXT, value TEXT, host TEXT,
      path TEXT DEFAULT '/', expiry INTEGER, isSecure INTEGER, isHttpOnly INTEGER,
      lastAccessed INTEGER, creationTime INTEGER)`);
    const ins = con.prepare("INSERT INTO moz_cookies (name, value, host, lastAccessed) VALUES (?, ?, ?, ?)");
    for (const [name, value, host, lastAccessed] of rows) ins.run(name, value, host, lastAccessed);
  } finally { con.close(); }
  return { root, dbPath };
}

const GOOD = ["AUTH-12345", "tok123", "lumo.proton.me", 2000];
const DISTRACTORS = [
  ["AUTH-99999", "older-token", "lumo.proton.me", 1000],   // older AUTH- cookie: must lose on ordering
  ["AUTH-00000", "", "lumo.proton.me", 3000],               // empty value: excluded
  ["SESSIONID", "not-auth", "lumo.proton.me", 4000],         // non-AUTH name: excluded
  ["AUTH-77777", "wrong-host", "example.com", 5000],        // other host: excluded by LIKE
];

test("Firefox fixture profile yields uid, token and the cookies.sqlite source", () => {
  const { root, dbPath } = makeFirefoxProfile([GOOD, ...DISTRACTORS]);
  try {
    const creds = readLumoCredentials({ firefoxProfiles: [root] });
    assert.deepEqual(creds, { uid: "12345", token: "tok123", source: dbPath });
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test("profile found in a subdirectory of the searched root is also scanned", () => {
  const { root, dbPath } = makeFirefoxProfile([GOOD], { subdir: "abc123.default-release" });
  try {
    const creds = readLumoCredentials({ firefoxProfiles: [root] });
    assert.equal(creds?.uid, "12345");
    assert.equal(creds?.source, dbPath);
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test("a profile with no matching AUTH cookie returns null, without falling through", () => {
  // Rows that each fail exactly one WHERE clause of FF_SELECT.
  const { root } = makeFirefoxProfile([
    ["AUTH-00000", "", "lumo.proton.me", 3000],       // empty value: excluded
    ["SESSIONID", "not-auth", "lumo.proton.me", 4000], // non-AUTH name: excluded
    ["AUTH-77777", "wrong-host", "example.com", 5000], // other host: excluded by LIKE
  ]);
  try {
    assert.equal(readLumoCredentials({ firefoxProfiles: [root] }), null);
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test("a missing profile directory is skipped without throwing", () => {
  assert.equal(readLumoCredentials({ firefoxProfiles: [join(tmpdir(), "lumo-no-such-profile")] }), null);
});

test("without the seam, LUMO_TOKEN/LUMO_UID env vars take priority over everything", () => {
  // Only exercises the env branch; never touches real browser profiles.
  const saved = { LUMO_TOKEN: process.env.LUMO_TOKEN, LUMO_UID: process.env.LUMO_UID };
  try {
    process.env.LUMO_TOKEN = "envtok";
    process.env.LUMO_UID = "42";
    assert.deepEqual(readLumoCredentials(), { uid: "42", token: "envtok", source: "env" });
  } finally {
    for (const [k, v] of Object.entries(saved))
      if (v === undefined) delete process.env[k]; else process.env[k] = v;
  }
});
