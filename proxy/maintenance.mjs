// Run: node --test proxy/maintenance.test.mjs
// Startup housekeeping helpers (log rotation, debug-dump pruning), split out of
// proxy.mjs so they are unit-testable without starting the server.
import { readdirSync, renameSync, rmSync, statSync } from "node:fs";

// If logPath exists and exceeds maxBytes, rename it to logPath + ".1" (one
// generation, overwriting any previous .1). Errors are swallowed, like the
// proxy's other log writes. Returns true when a rotation happened.
export function rotateLog(logPath, maxBytes) {
  try {
    const size = statSync(logPath).size;
    if (size <= maxBytes) return false;
    renameSync(logPath, logPath + ".1");
    return true;
  } catch {
    return false;
  }
}

// If dir holds more than maxFiles entries, delete the oldest (by mtime) until
// at most maxFiles remain. Errors are swallowed; links are removed, not followed.
export function pruneDir(dir, maxFiles) {
  try {
    const entries = readdirSync(dir).map((name) => {
      const path = `${dir}/${name}`;
      try { return { path, mtime: statSync(path).mtimeMs }; }
      catch { return { path, mtime: Infinity }; } // unreadable: never the oldest
    });
    const excess = entries.length - maxFiles;
    if (excess <= 0) return 0;
    entries.sort((a, b) => a.mtime - b.mtime);
    for (const { path } of entries.slice(0, excess)) {
      try { rmSync(path, { recursive: true, force: true }); } catch {}
    }
    return excess;
  } catch {
    return 0;
  }
}
