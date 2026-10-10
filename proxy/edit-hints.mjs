// Adds a hint to failed Edit and Write results before they reach the model.
//
// Lumo models often rebuild an Edit's old_string from memory: a dropped word, an import that is not
// there, a typographic dash or quote. Claude Code then rejects the call with "String to replace not
// found in file" and nothing else, so the model guesses again. Claude Code checks this before any
// hook runs, so no hook can help; the proxy sees every request, so the hint is added here: the
// closest real text in the file, between markers, ready to copy.
//
// Only the newest tool results (after the last assistant turn) are changed, so the history the
// model sees later is unchanged. Any problem leaves the request exactly as it was.
import { readFileSync, statSync } from "node:fs";

const MAX_BYTES = 2_000_000, MAX_LINES = 60, MAX_FILE_LINES = 20_000;
const TYPO = { "—": "-", "–": "-", "‘": "'", "’": "'", "“": '"', "”": '"', " ": " ", "…": "..." };
const norm = (s) => s.replace(/[—–‘’“” …]/g, (c) => TYPO[c]).split(/\s+/).filter(Boolean).join(" ");

const bigrams = (s) => {
  const m = new Map();
  for (let i = 0; i < s.length - 1; i++) { const b = s.slice(i, i + 2); m.set(b, (m.get(b) || 0) + 1); }
  return m;
};
// Dice similarity of character bigrams: 1 = same text, 0 = nothing in common.
const similarity = (a, bA, b) => {
  if (!a.length || !b.length) return a === b ? 1 : 0;
  const bB = bigrams(b);
  let common = 0;
  for (const [k, v] of bA) common += Math.min(v, bB.get(k) || 0);
  return (2 * common) / (Math.max(a.length - 1, 1) + Math.max(b.length - 1, 1));
};

const block = (lines, i, n) => "<<<\n" + lines.slice(i, i + n).join("\n") + "\n>>>";

export function editHint(path, oldString) {
  if (!path || !oldString) return "";
  try { if (statSync(path).size > MAX_BYTES) return ""; } catch { return `\n\n[claude-lumo] ${path} does not exist. Check the path (Glob) before editing.`; }
  const text = readFileSync(path, "utf8").replace(/\r\n/g, "\n");
  const lines = text.split("\n");
  if (lines.length > MAX_FILE_LINES) return "";
  const want = oldString.replace(/\r\n/g, "\n").replace(/^\n+|\n+$/g, "").split("\n");
  const n = want.length;
  if (n > MAX_LINES) return "\n\n[claude-lumo] old_string is too long to match reliably. Read the exact lines again and edit a smaller, unique block.";
  const normed = lines.map(norm), target = want.map(norm);

  // Same lines once whitespace and typographic dashes/quotes are ignored.
  const hits = [];
  for (let i = 0; i + n <= lines.length && hits.length < 2; i++) if (target.every((t, j) => normed[i + j] === t)) hits.push(i);
  if (hits.length === 1) {
    const i = hits[0];
    return `\n\n[claude-lumo] old_string differs from the file only in whitespace or dashes/quotes. The exact text (lines ${i + 1}-${i + n} of ${path}) is between the markers; retry with old_string copied from it exactly, without the markers:\n${block(lines, i, n)}`;
  }

  // Otherwise the closest block, allowing up to 2 lines fewer or more (a dropped or invented line).
  const joined = target.join("\n"), jB = bigrams(joined);
  let best = 0, bi = -1, bn = n;
  for (let size = Math.max(1, n - 2); size <= n + 2; size++) {
    for (let i = 0; i + size <= lines.length; i++) {
      const cand = normed.slice(i, i + size).join("\n");
      if ((2 * Math.min(cand.length, joined.length)) / (cand.length + joined.length) <= best) continue; // cannot beat best
      const r = similarity(joined, jB, cand);
      if (r > best) { best = r; bi = i; bn = size; }
    }
  }
  if (best < 0.5) return "\n\n[claude-lumo] Nothing in the file is close to old_string: the file changed since you read it, or the text was recalled from memory. Read the lines again before editing.";
  return `\n\n[claude-lumo] The closest text is lines ${bi + 1}-${bi + bn} of ${path} (${Math.round(best * 100)}% similar). Retry with old_string copied exactly from between the markers, without the markers:\n${block(lines, bi, bn)}`;
}

const textOf = (c) => typeof c === "string" ? c : Array.isArray(c) ? c.map((p) => p?.text || "").join("") : "";
const append = (msg, extra) => {
  if (typeof msg.content === "string") msg.content += extra;
  else if (Array.isArray(msg.content)) msg.content.push({ type: "text", text: extra });
};

export function addEditHints(body) {
  try {
    const req = JSON.parse(body.toString("utf8"));
    const msgs = req.messages;
    if (!Array.isArray(msgs)) return body;
    let last = -1;
    for (let i = msgs.length - 1; i >= 0; i--) if (msgs[i].role === "assistant") { last = i; break; }
    if (last < 0 || !Array.isArray(msgs[last].tool_calls)) return body;
    const calls = new Map(msgs[last].tool_calls.map((tc) => [tc.id, tc.function || {}]));
    let changed = false;
    for (const m of msgs.slice(last + 1)) {
      if (m.role !== "tool" || !calls.has(m.tool_call_id)) continue;
      const fn = calls.get(m.tool_call_id), text = textOf(m.content);
      let args = {}; try { args = JSON.parse(fn.arguments || "{}"); } catch { continue; }
      let extra = "";
      if (fn.name === "Edit" && text.includes("String to replace not found")) extra = editHint(args.file_path, args.old_string);
      else if ((fn.name === "Edit" || fn.name === "Write") && text.includes("has not been read yet"))
        extra = `\n\n[claude-lumo] Call the Read tool on ${args.file_path} now (reading it with Bash does not count), then repeat this ${fn.name}.`;
      if (extra) { append(m, extra); changed = true; }
    }
    return changed ? Buffer.from(JSON.stringify(req), "utf8") : body;
  } catch {
    return body;
  }
}
