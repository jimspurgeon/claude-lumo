// Run: node --test proxy/maintenance.test.mjs
// Housekeeping helpers (state-dir paths, log rotation, debug-dump pruning), split
// out of proxy.mjs so they are unit-testable without starting the server.
import { readdirSync, renameSync, rmSync, statSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

// Where the proxy keeps its logs and debug dumps (#134): under the Lumo state
// dir, so a repo checkout stays clean. CLAUDE_LUMO_STATE_DIR wins, then
// ~/.claude-lumo, the same convention every other claude-lumo script uses.
export function resolveStatePaths(env = process.env, home = homedir()) {
  const stateDir = env.CLAUDE_LUMO_STATE_DIR || join(home, ".claude-lumo");
  return {
    stateDir,
    logPath: join(stateDir, "logs", "proxy.log"),
    debugDir: join(stateDir, "debug"),
  };
}

// One housekeeping pass: rotate the log if it is over maxLogBytes, prune the
// debug dir down to maxDumpFiles. Called at startup and after every log line,
// so the caps hold for a long-running proxy, not only at startup (#134).
export function makeHousekeeper(logPath, debugDir, { maxLogBytes, maxDumpFiles } = {}) {
  return () => {
    if (maxLogBytes) rotateLog(logPath, maxLogBytes);
    if (maxDumpFiles) pruneDir(debugDir, maxDumpFiles);
  };
}

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
