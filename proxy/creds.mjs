// Reads the live Lumo login (AUTH-<uid> cookie) from the user's browser.
//
// Supported, in priority order:
//   1. LUMO_TOKEN + LUMO_UID environment variables
//   2. ~/.claude-lumo/login.json  (manual fallback: paste the cookie once, any browser)
//   3. Firefox / LibreWolf — cookie store is plaintext SQLite
//   4. Chrome / Edge / Brave / Vivaldi / Opera — encrypted SQLite, decrypted locally
//
// Every browser file is copied to a temp dir first so no browser's lock is ever touched.
// Works on Windows, macOS and Linux. Chromium v20 (app-bound) cookies on very new Chrome
// can't be decrypted; those fall through to the manual fallback (see README).
import { DatabaseSync } from "node:sqlite";
import { execFileSync } from "node:child_process";
import { copyFileSync, existsSync, mkdtempSync, readFileSync, readdirSync, rmSync, statSync } from "node:fs";
import { createDecipheriv, pbkdf2Sync } from "node:crypto";
import { join } from "node:path";
import { homedir, tmpdir } from "node:os";
import { platform } from "node:process";

// ---------- candidate cookie databases ----------

// `rootsOverride` (used by tests): an explicit list of profile dirs to search instead of
// the real browser locations below. `undefined` keeps the default behavior.
function firefoxDbs(rootsOverride) {
  const roots = rootsOverride ?? [
    // Windows
    process.env.APPDATA && join(process.env.APPDATA, "Mozilla", "Firefox", "Profiles"),
    process.env.APPDATA && join(process.env.APPDATA, "LibreWolf", "Profiles"),
    // macOS
    join(homedir(), "Library", "Application Support", "Firefox", "Profiles"),
    join(homedir(), "Library", "Application Support", "LibreWolf", "Profiles"),
    // Linux
    join(homedir(), ".mozilla", "firefox"),
    join(homedir(), ".librewolf"),
    // explicit override (path to the profile dir containing cookies.sqlite)
    process.env.LUMO_FIREFOX_PROFILE,
  ].filter(Boolean);
  const out = [];
  for (const root of roots) {
    if (!existsSync(root)) continue;
    const db = join(root, "cookies.sqlite");
    if (existsSync(db)) out.push(db);
    for (const e of readdirSync(root, { withFileTypes: true }))
      if (e.isDirectory() && existsSync(join(root, e.name, "cookies.sqlite")))
        out.push(join(root, e.name, "cookies.sqlite"));
  }
  return out.sort((a, b) => statSync(b).mtimeMs - statSync(a).mtimeMs);
}

function chromiumDbs() {
  // [userDataDir, browser name]; "Local State" (with the encryption key) sits in userDataDir,
  // cookies in userDataDir/<profile>/Cookies (Opera: userDataDir/Cookies).
  const dirs = [];
  const la = process.env.LOCALAPPDATA, as = join(homedir(), "Library", "Application Support");
  const cfg = process.env.XDG_CONFIG_HOME || join(homedir(), ".config");
  if (la) dirs.push(
    [join(la, "Google", "Chrome", "User Data"), "Chrome"],
    [join(la, "Microsoft", "Edge", "User Data"), "Edge"],
    [join(la, "BraveSoftware", "Brave-Browser", "User Data"), "Brave"],
    [join(la, "Vivaldi", "User Data"), "Vivaldi"],
    [join(la, "Opera Software", "Opera Stable"), "Opera"]);
  dirs.push(
    [join(as, "Google", "Chrome"), "Chrome"],
    [join(as, "Microsoft Edge"), "Edge"],
    [join(as, "BraveSoftware", "Brave-Browser"), "Brave"],
    [join(as, "Vivaldi"), "Vivaldi"],
    [join(as, "com.operasoftware.Opera"), "Opera"],
    [join(cfg, "google-chrome"), "Chrome"],
    [join(cfg, "microsoft-edge"), "Edge"],
    [join(cfg, "brave-browser"), "Brave"],
    [join(cfg, "vivaldi"), "Vivaldi"],
    [join(cfg, "opera"), "Opera"]);
  const out = [];
  for (const [dir, browser] of dirs) {
    if (!existsSync(dir)) continue;
    const state = join(dir, "Local State");
    if (!existsSync(state)) continue;
    const profiles = existsSync(join(dir, "Cookies")) ? ["."] // Opera layout
      : readdirSync(dir, { withFileTypes: true }).filter(e => e.isDirectory() &&
          (e.name === "Default" || e.name.startsWith("Profile ")) &&
          existsSync(join(dir, e.name, "Cookies"))).map(e => e.name);
    for (const p of profiles)
      out.push({ dir, browser, cookies: p === "." ? join(dir, "Cookies") : join(dir, p, "Cookies"), profile: p });
  }
  return out;
}

// ---------- decryption (Chromium family) ----------

function dpapiUnprotect(b64) {
  // Windows: the AES key in Local State is itself DPAPI-encrypted; unwrap via PowerShell.
  const ps = `$b=[Convert]::FromBase64String('${b64}');` +
    `$u=[Security.Cryptography.ProtectedData]::Unprotect($b,$null,'CurrentUser');` +
    `[Convert]::ToBase64String($u)`;
  return Buffer.from(execFileSync("powershell.exe",
    ["-NoProfile", "-NonInteractive", "-Command", ps], { windowsHide: true, encoding: "utf8" }).trim(), "base64");
}

function chromiumEncryptionKey(userDataDir) {
  // Non-Windows: returns the password bytes (marker stripped later), not an AES key.
  try {
    const local = JSON.parse(readFileSync(join(userDataDir, "Local State"), "utf8"));
    let b64 = local.os_crypt?.encrypted_key ?? "";
    if (!b64) return null;
    const raw = Buffer.from(b64, "base64");
    if (platform === "win32") {
      if (raw.subarray(0, 5).toString() !== "DPAPI") return null;
      return dpapiUnprotect(raw.subarray(5).toString("base64"));
    }
    return raw; // password bytes for PBKDF2 (see chromiumAesKey)
  } catch { /* unreadable key: Chromium unusable, fall back */ }
  return null;
}

function chromiumAesKey(userDataDir, browser) {
  if (platform === "win32") {
    return chromiumEncryptionKey(userDataDir); // DPAPI-unwrapped raw AES-256 key
  }
  let password = null;
  if (platform === "darwin") {
    password = macSafeStoragePassword(browser); // Keychain, e.g. "Chrome Safe Storage"
  } else {
    // Linux: Local State may pin a password; otherwise the well-known default.
    const key = chromiumEncryptionKey(userDataDir);
    if (key) {
      let raw = key.toString("latin1");
      if (raw.startsWith("v10")) raw = raw.slice(3);
      password = raw;
    } else {
      password = "peanuts";
    }
  }
  return password ? deriveKey(password) : null;
}

function deriveKey(password) {
  // macOS/Linux Chromium: PBKDF2-HMAC-SHA1, salt "saltysalt", length 16.
  // Iterations differ: macOS 1003, Linux 1.
  return pbkdf2Sync(password, "saltysalt", platform === "darwin" ? 1003 : 1, 16, "sha1");
}

function macSafeStoragePassword(browser) {
  // macOS: the Chromium AES password lives in the Keychain ("Chrome Safe Storage" etc.).
  const svc = browser === "Edge" ? "Microsoft Edge Safe Storage"
    : browser === "Brave" ? "Brave Safe Storage"
    : "Chrome Safe Storage";
  try {
    return execFileSync("security", ["find-generic-password", "-w", "-s", svc],
      { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"] }).trim();
  } catch { return null; }
}

function decryptCookieValue(blob, key) {
  // Chromium cookie values: "v10"/"v11" prefix + payload.
  //   Windows (v10/v11): AES-256-GCM, 12-byte nonce after the prefix, 16-byte tag at the end.
  //   macOS/Linux (v10/v11): AES-128-CBC, IV is 16 space chars.
  //   v20 (app-bound, Chrome >= 127 on Windows): not decryptable here.
  const tag = blob.subarray(0, 3).toString();
  if (!tag.startsWith("v1")) return blob.toString("utf8"); // legacy plaintext
  if (tag === "v20") throw new Error("app-bound cookie (unsupported)");
  const data = blob.subarray(3);
  if (platform === "win32") {
    const d = createDecipheriv("aes-256-gcm", key, data.subarray(0, 12));
    d.setAuthTag(data.subarray(data.length - 16));
    return Buffer.concat([d.update(data.subarray(12, data.length - 16)), d.final()]).toString("utf8");
  }
  const d = createDecipheriv("aes-128-cbc", key, Buffer.alloc(16, 0x20));
  return Buffer.concat([d.update(data), d.final()]).toString("utf8");
}

// ---------- readers ----------

function readFromDb(cookiesPath, query, params = []) {
  // Copy the DB (and wal) so the browser's lock is never held, then query it.
  const dir = mkdtempSync(join(tmpdir(), "lumo-proxy-"));
  try {
    copyFileSync(cookiesPath, join(dir, "cookies.sqlite"));
    if (existsSync(cookiesPath + "-wal")) copyFileSync(cookiesPath + "-wal", join(dir, "cookies.sqlite-wal"));
    const con = new DatabaseSync(join(dir, "cookies.sqlite"));
    try { return con.prepare(query).get(...params); } finally { con.close(); }
  } finally { try { rmSync(dir, { recursive: true, force: true }); } catch {} }
}

// Firefox and Chrome cookie stores are both SQLite but with different schemas:
//   Firefox: moz_cookies(name, value, host, lastAccessed)
//   Chromium: cookies(name, encrypted_value, host_key, last_access_utc)
const FF_SELECT = "SELECT name, value FROM moz_cookies " +
  "WHERE host LIKE '%lumo.proton.me' AND name LIKE 'AUTH-%' AND value != '' " +
  "ORDER BY lastAccessed DESC LIMIT 1";
const CHR_SELECT = "SELECT name, encrypted_value FROM cookies " +
  "WHERE host_key LIKE '%lumo.proton.me' AND name LIKE 'AUTH-%' " +
  "ORDER BY last_access_utc DESC LIMIT 1";

function readFirefox(db) {
  const row = readFromDb(db, FF_SELECT);
  if (row && row.value) return { uid: row.name.slice(5), token: row.value, source: db };
  return null;
}

function readChromium(entry) {
  const key = chromiumAesKey(entry.dir, entry.browser);
  if (!key) return null;
  const row = readFromDb(entry.cookies, CHR_SELECT);
  if (row && row.encrypted_value) {
    try {
      const token = decryptCookieValue(Buffer.from(row.encrypted_value), key);
      if (token) return { uid: row.name.slice(5), token, source: entry.cookies };
    } catch { /* v20 app-bound or key mismatch: skip */ }
  }
  return null;
}

// ---------- entry point ----------

// `opts.firefoxProfiles` (tests only): an array of profile dirs to search for cookies.sqlite
// instead of the real browser locations. When given, the env/manual/Firefox/Chromium fallback
// chain is replaced by those dirs alone, so nothing outside them is read.
export function readLumoCredentials(opts = {}) {
  if (opts.firefoxProfiles) {
    for (const root of opts.firefoxProfiles) {
      for (const db of firefoxDbs([root])) {
        try { const c = readFirefox(db); if (c) return c; } catch {}
      }
    }
    return null;
  }

  // 1. Environment
  if (process.env.LUMO_TOKEN && process.env.LUMO_UID)
    return { uid: process.env.LUMO_UID, token: process.env.LUMO_TOKEN, source: "env" };

  // 2. Manual fallback: ~/.claude-lumo/login.json (any browser, see README)
  const manual = join(homedir(), ".claude-lumo", "login.json");
  try {
    const j = JSON.parse(readFileSync(manual, "utf8"));
    if (j.uid && j.token) return { uid: j.uid, token: j.token, source: manual };
  } catch {}

  // 3. Firefox family (plaintext, always works)
  for (const db of firefoxDbs()) {
    try { const c = readFirefox(db); if (c) return c; } catch {}
  }

  // 4. Chromium family (best effort; falls back to manual on v20/app-bound)
  for (const entry of chromiumDbs()) {
    try { const c = readChromium(entry); if (c) return c; } catch {}
  }

  return null;
}
