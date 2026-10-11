// Run: node --test proxy/tool-guard.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { createToolGuard } from "./tool-guard.mjs";

const chunk = (delta, finish = null) => "data:" + JSON.stringify({ id: "c1", object: "chat.completion.chunk", model: "m", choices: [{ index: 0, delta, finish_reason: finish }] });
const call = (index, id, name, args) => ({ index, id, type: "function", function: { name, arguments: args } });

// Feeds lines through a guard and returns the parsed events it sends, in order.
const run = async (lines, retry) => {
  const g = createToolGuard();
  let out = lines.map((l) => g.line(l)).filter(Boolean).join("\n\n") + "\n\n" + await g.end(retry);
  const events = out.split("\n").filter((l) => l.startsWith("data:")).map((l) => l.slice(5));
  return { g, events: events.map((e) => (e === "[DONE]" ? e : JSON.parse(e))) };
};
const toolCalls = (events) => events.flatMap((e) => e.choices?.[0]?.delta?.tool_calls || []);
const finishOf = (events) => events.map((e) => e.choices?.[0]?.finish_reason).filter(Boolean);
const text = (events) => events.map((e) => e.choices?.[0]?.delta?.content || "").join("");

test("plain text streams through unchanged", async () => {
  const { events } = await run([chunk({ content: "hi" }), chunk({ content: " there" }), chunk({}, "stop"), "data:[DONE]"]);
  assert.equal(text(events), "hi there");
  assert.deepEqual(finishOf(events), ["stop"]);
  assert.equal(events.at(-1), "[DONE]");
});

test("a normal tool call arrives whole, then the finish", async () => {
  const { g, events } = await run([
    chunk({ content: "Reading." }),
    chunk({ tool_calls: [call(0, "t1", "Read", '{"file_')] }),
    chunk({ tool_calls: [{ index: 0, function: { arguments: 'path":"a"}' } }] }),
    chunk({}, "tool_calls"), "data:[DONE]",
  ]);
  const tcs = toolCalls(events);
  assert.equal(tcs.length, 1);
  assert.deepEqual([tcs[0].id, tcs[0].function.name, tcs[0].function.arguments], ["t1", "Read", '{"file_path":"a"}']);
  assert.deepEqual(finishOf(events), ["tool_calls"]);
  assert.equal(text(events), "Reading.");
  assert.deepEqual(g.stats, { dropped: 0, repaired: 0, retry: 0 });
});

test("several calls in one chunk go out one per chunk", async () => {
  const { events } = await run([chunk({ tool_calls: [call(0, "a", "Read", "{}"), call(1, "b", "Read", "{}")] }, "tool_calls")]);
  const withCalls = events.filter((e) => e.choices?.[0]?.delta?.tool_calls);
  assert.equal(withCalls.length, 2);
  assert.ok(withCalls.every((e) => e.choices[0].delta.tool_calls.length === 1));
});

test("finish tool_calls with no call retries, and forwards only the retry's call (cap-18 shape)", async () => {
  let retried = 0;
  const retryBody = [chunk({ content: "duplicate text" }), chunk({ tool_calls: [call(0, "r1", "Write", '{"file_path":"x","content":"y"}')] }), chunk({}, "tool_calls"), "data:[DONE]"].join("\n\n");
  const { g, events } = await run([chunk({ content: "Now the test." }), ": keepalive", chunk({}, "tool_calls"), "data:[DONE]"], async () => { retried++; return retryBody; });
  assert.equal(retried, 1);
  assert.equal(text(events), "Now the test.");
  assert.deepEqual(toolCalls(events).map((t) => t.id), ["r1"]);
  assert.deepEqual(finishOf(events), ["tool_calls"]);
  assert.equal(g.stats.retry, 1);
});

test("arguments leaked as arg_key/arg_value tags are repaired", async () => {
  const leaked = "<arg_key>file_path</arg_key><arg_value>a.txt</arg_value><arg_key>limit</arg_key><arg_value>40</arg_value>";
  const { g, events } = await run([chunk({ tool_calls: [call(0, "t1", "Read", leaked)] }, "tool_calls")], async () => assert.fail("no retry expected"));
  assert.deepEqual(JSON.parse(toolCalls(events)[0].function.arguments), { file_path: "a.txt", limit: 40 });
  assert.equal(g.stats.repaired, 1);
});

test("if the retry fails too, the turn ends with a visible message", async () => {
  const { g, events } = await run([chunk({ tool_calls: [call(0, "t1", "Edit", '{"file_path":"a",')] }, "tool_calls")], async () => "");
  assert.equal(toolCalls(events).length, 0);
  assert.match(text(events), /tool call was lost in transit/);
  assert.deepEqual(finishOf(events), ["stop"]);
  assert.equal(events.at(-1), "[DONE]");
  assert.equal(g.stats.dropped, 1);
});

test("a retry that throws still ends the turn visibly", async () => {
  const { events } = await run([chunk({}, "tool_calls")], async () => { throw new Error("down"); });
  assert.match(text(events), /lost in transit/);
});
