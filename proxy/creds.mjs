// Reads the live Lumo login (AUTH-<uid> cookie) from Firefox, like pi-lumo does.
// Copies cookies.sqlite (+ -wal) to a temp dir so Firefox's lock is never touched.
import { DatabaseSync } from "node:sqlite";
import { copyFileSync, existsSync, mkdtempSync, readdirSync, rmSync, statSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";

const roots = [process.env.LUMO_FIREFOX_PROFILE,
  process.env.APPDATA && join(process.env.APPDATA, "Mozilla", "Firefox", "Profiles"),
  process.env.APPDATA && join(process.env.APPDATA, "LibreWolf", "Profiles")].filter(Boolean);

function databases() {
  const out = [];
  for (const root of roots) {
    if (!existsSync(root)) continue;
    if (existsSync(join(root, "cookies.sqlite"))) out.push(join(root, "cookies.sqlite"));
    for (const e of readdirSync(root, { withFileTypes: true }))
      if (e.isDirectory() && existsSync(join(root, e.name, "cookies.sqlite"))) out.push(join(root, e.name, "cookies.sqlite"));
  }
  return out.sort((a, b) => statSync(b).mtimeMs - statSync(a).mtimeMs);
}

export function readLumoCredentials() {
  if (process.env.LUMO_TOKEN && process.env.LUMO_UID) return { uid: process.env.LUMO_UID, token: process.env.LUMO_TOKEN, source: "env" };
  for (const db of databases()) {
    const dir = mkdtempSync(join(tmpdir(), "lumo-proxy-"));
    try {
      copyFileSync(db, join(dir, "cookies.sqlite"));
      if (existsSync(db + "-wal")) copyFileSync(db + "-wal", join(dir, "cookies.sqlite-wal"));
      const con = new DatabaseSync(join(dir, "cookies.sqlite"));
      const row = con.prepare("SELECT name, value FROM moz_cookies WHERE host LIKE '%lumo.proton.me' AND name LIKE 'AUTH-%' AND value != '' ORDER BY expiry DESC LIMIT 1").get();
      con.close();
      if (row) return { uid: row.name.slice(5), token: row.value, source: db };
    } catch { /* locked or incompatible profile: try the next */ }
    finally { try { rmSync(dir, { recursive: true, force: true }); } catch {} }
  }
  return null;
}
