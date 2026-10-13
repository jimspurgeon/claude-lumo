// Rolling history of recent chat requests (issue #63).
//
// The status line must show the model that actually answered a session. The
// transcript cannot: LiteLLM echoes the requested model name back to Claude
// Code, so a session configured for lumo-max that was served with lumo-lite
// still records lumo-max in every transcript entry. This proxy is the only
// component that sees the truth - but LiteLLM forwards no session identity
// with the request, so the proxy cannot tell which session it served.
//
// Instead it writes this small rolling history (model + start/end times of the
// last few chat requests); statusline.py matches its transcript's last
// main-session assistant-entry timestamp against the request end times (an
// entry is written moments after its stream ends) to attribute its session.

import { mkdirSync, readFileSync, renameSync, writeFileSync } from "node:fs";
import { dirname } from "node:path";

// Loads (tolerantly) and persists the history. record() never throws: a state
// write must never break proxying. Writes are atomic (tmp + rename) so a
// concurrently reading status line never sees a truncated file.
export function createModelHistory({ file, maxEntries = 200 }) {
  let entries = [];
  try {
    const loaded = JSON.parse(readFileSync(file, "utf8"));
    if (Array.isArray(loaded)) entries = loaded.filter((e) => e && typeof e === "object");
  } catch {} // missing or corrupt file: start fresh
  return {
    entries: () => entries.slice(),
    record({ model, t0, tEnd }) {
      if (typeof model !== "string" || !model) return;
      entries.push({ model, t0, tEnd });
      if (entries.length > maxEntries) entries = entries.slice(-maxEntries);
      try {
        mkdirSync(dirname(file), { recursive: true });
        const tmp = file + ".tmp";
        writeFileSync(tmp, JSON.stringify(entries));
        renameSync(tmp, file);
      } catch {} // unwritable location: keep proxying, just lose the history
    },
  };
}
