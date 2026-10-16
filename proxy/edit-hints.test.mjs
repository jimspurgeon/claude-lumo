// Run: node --test proxy/edit-hints.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { writeFileSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { addEditHints } from "./edit-hints.mjs";

const tmp = () => mkdtempSync(join(tmpdir(), "lumo-edithints-"));
const write = (dir, name, text) => { const p = join(dir, name); writeFileSync(p, text); return p; };

// A request whose last assistant turn called Edit(file_path, old_string) and whose tool
// result carries the given text (default: the "String to replace not found" rejection).
const editBody = (path, oldString, resultText = "String to replace not found in file.") =>
  Buffer.from(JSON.stringify({
    messages: [
      { role: "assistant", tool_calls: [{ id: "c1", type: "function", function: { name: "Edit", arguments: JSON.stringify({ file_path: path, old_string: oldString }) } }] },
      { role: "tool", tool_call_id: "c1", content: resultText },
    ],
  }), "utf8");

const toolContent = (buf) => JSON.parse(buf.toString("utf8")).messages[1].content;

test("a failed Edit with a close match appends the closest block between markers", () => {
  const dir = tmp();
  try {
    const p = write(dir, "a.txt", "line one alpha\nline two beta\nline three gamma\n");
    const text = toolContent(addEditHints(editBody(p, "line two betta"))); // one-char typo
    assert.match(text, /<<</);
    assert.match(text, />>>/);
    assert.ok(text.includes("line two beta"), text);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test("no close match appends the 'nothing is close' hint (actual behavior)", () => {
  const dir = tmp();
  try {
    const p = write(dir, "b.txt", "totally unrelated content here\n");
    const text = toolContent(addEditHints(editBody(p, "zzzzzzzzzzzzzzzzzzzz")));
    assert.ok(text.includes("Nothing in the file is close"), text);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

// Threshold is 0.5. With a single-line file the Dice score equals sharedBigrams/(len-1),
// so "ABCDEFXXXX" scores 5/9 (~0.556, above) and "ABCDEXXXXX" scores 4/9 (~0.444, below).
test("similarity just above the 0.5 threshold yields the closest-block hint", () => {
  const dir = tmp();
  try {
    const p = write(dir, "c.txt", "ABCDEFGHIJ\n");
    const text = toolContent(addEditHints(editBody(p, "ABCDEFXXXX")));
    assert.match(text, /<<</);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test("similarity just below the 0.5 threshold yields the 'nothing is close' hint", () => {
  const dir = tmp();
  try {
    const p = write(dir, "d.txt", "ABCDEFGHIJ\n");
    const text = toolContent(addEditHints(editBody(p, "ABCDEXXXXX")));
    assert.ok(text.includes("Nothing in the file is close"), text);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test("a malformed body (invalid JSON) is returned unchanged", () => {
  const body = Buffer.from("not json at all", "utf8");
  assert.equal(addEditHints(body), body);
});

test("a file missing on disk yields the 'does not exist' hint, not a crash", () => {
  const dir = tmp();
  try {
    const p = join(dir, "missing.txt");
    const text = toolContent(addEditHints(editBody(p, "whatever")));
    assert.ok(text.includes("does not exist"), text);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});
