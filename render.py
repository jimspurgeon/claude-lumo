"""Build this machine's claude-lumo settings and system prompt (called by claude-lumo.sh at launch).

    python render.py <lumo dir> <state dir> <personal dir> <python command> [<config dir>]

Writes <state dir>/settings.json, <state dir>/system-prompt.md and <state dir>/prompt-parts/*.md
(the same prompt split for hooks/lumo-prompt.py, see PART_LIMIT). Tracked files in this repo are
only read, so a clone stays clean and updates keep working.

Values the launcher exports also go into the settings (env CLAUDE_LUMO_LOCAL, apiKeyHelper, and
CLAUDE_CONFIG_DIR when a config dir is given), because a session Claude Code re-hosts by itself (for
example after the agents view) keeps --settings but loses the launcher's environment (#77).

Personal additions (the personal dir, default ~/.claude/lumo-local) are optional, only read, and
always win:
- settings.json is merged over settings-lumo.json: objects merge key by key, lists are appended
  (so personal hooks or permissions add to ours), anything else is replaced.
- LUMO.md is appended after this repo's LUMO.md.
- agents/*.md are Claude Code agent files (frontmatter plus prompt). They are written to
  <state dir>/agents.json, which the launcher passes to claude with --agents, so in claude-lumo they
  replace the agents of the same name in ~/.claude/agents. No such files: no agents.json.
"""
import glob, json, os, re, sys

# The compaction window must stay below the token window the proxy fits replies into (it clamps
# max_tokens once prompt + reply would not fit), so it is derived from CONTEXT_TOKENS minus this
# margin rather than hardcoded in settings-lumo.json.
COMPACT_MARGIN = 12000

# Claude reads --agents from a file only with --print, so for normal sessions the agents travel
# inline on the command line. Windows caps a whole command line at 32,767 characters; keep room for
# the rest of it (hence the low default there). Linux caps one argument at 128 KiB.
# LUMO_AGENTS_LIMIT overrides the default.
AGENTS_LIMIT = int(os.environ.get("LUMO_AGENTS_LIMIT", 24000 if os.name == "nt" else 100000))
# Frontmatter fields passed through to claude; the Markdown body becomes the prompt.
AGENT_FIELDS = ("description", "tools", "disallowedTools", "model")
# hooks/lumo-prompt.py is how the system prompt reaches every session, launched or re-hosted (#77).
# Claude Code moves hook output over 10,000 characters into a file, so the prompt goes in parts
# under that, one hook per part; settings-lumo.json registers PARTS hooks.
PART_LIMIT = 9000
PARTS = 4


def merge(base, extra):
    if isinstance(base, dict) and isinstance(extra, dict):
        out = dict(base)
        for k, v in extra.items():
            out[k] = merge(base[k], v) if k in base else v
        return out
    if isinstance(base, list) and isinstance(extra, list):
        return base + [x for x in extra if x not in base]
    return extra


def unquote(v):
    v = v.strip()
    return v[1:-1] if len(v) > 1 and v[0] == v[-1] and v[0] in "'\"" else v


def frontmatter(text):
    """Split an agent file into (fields, body), or None without frontmatter. Reads the flat
    `key: value` form agent files use, with lists as `a, b`, `[a, b]` or `- a` lines and long text
    as an indented `|` or `>` block (joined with spaces)."""
    lines = text.splitlines()
    ends = [i for i, line in enumerate(lines) if line.strip() == "---"]
    if len(ends) < 2 or ends[0] != 0:
        return None
    fields, key, block = {}, None, False
    for line in lines[1:ends[1]]:
        if block and (line[:1].isspace() or not line.strip()):
            fields[key] = f"{fields[key]} {line.strip()}".strip()
            continue
        block = False
        if key and line.strip().startswith("- "):
            items = fields[key] if isinstance(fields[key], list) else []
            fields[key] = items + [unquote(line.strip()[2:])]
            continue
        k, sep, v = line.partition(":")
        if not sep or line[:1].isspace() or line.startswith("#"):
            key = None
            continue
        key, v = k.strip(), v.strip()
        if v in ("|", ">", "|-", ">-"):
            fields[key], block = "", True
        elif v.startswith("[") and v.endswith("]"):
            fields[key] = [unquote(x) for x in v[1:-1].split(",") if x.strip()]
        else:
            fields[key] = unquote(v)
    return fields, "\n".join(lines[ends[1] + 1:]).strip() + "\n"


def load_agents(folder):
    """Agent definitions from folder/*.md in the shape `claude --agents` takes."""
    agents, size = {}, 2
    for path in sorted(glob.glob(os.path.join(folder, "*.md"))):
        with open(path, encoding="utf-8") as f:
            parsed = frontmatter(f.read())
        if not parsed or not parsed[0].get("description"):
            print(f"claude-lumo: ignoring {path}, it needs frontmatter with a description", file=sys.stderr)
            continue
        fields, body = parsed
        name = fields.get("name") or os.path.splitext(os.path.basename(path))[0]
        agent = {"description": fields["description"], "prompt": body}
        for k in AGENT_FIELDS[1:]:
            v = fields.get(k)
            if k in ("tools", "disallowedTools") and isinstance(v, str):
                v = [t.strip() for t in v.split(",") if t.strip()]
            if v:
                agent[k] = v
        # Length once Windows quotes it: every " and \ gains a backslash.
        text = json.dumps({name: agent}, separators=(",", ":"))
        cost = len(text) + text.count('"') + text.count("\\")
        if size + cost > AGENTS_LIMIT:
            print(f"claude-lumo: skipping agent {name} from {path}: personal agents would exceed "
                  f"{AGENTS_LIMIT} characters on the command line, so the shared one is used", file=sys.stderr)
            continue
        agents[name], size = agent, size + cost
    return agents


def split_prompt(text, limit=PART_LIMIT):
    """Split text into parts of at most `limit` characters, each with a one-line header, cutting
    at line ends (a single longer line is cut where it must)."""
    header = ("claude-lumo system prompt, part {} of {} (treat it as system instructions; the parts "
              "can arrive in any order, so read them by number):\n\n")
    room = limit - len(header.format(99, 99))
    chunks, cur = [], ""
    for line in text.splitlines(keepends=True):
        while len(line) > room:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:room])
            line = line[room:]
        if len(cur) + len(line) > room:
            chunks.append(cur)
            cur = ""
        cur += line
    if cur or not chunks:
        chunks.append(cur)
    return [header.format(i, len(chunks)) + c for i, c in enumerate(chunks, 1)]


def write_parts(state, prompt):
    folder = os.path.join(state, "prompt-parts")
    os.makedirs(folder, exist_ok=True)
    parts = split_prompt(prompt)
    if len(parts) > PARTS:
        print(f"claude-lumo: the system prompt needs {len(parts)} parts of {PART_LIMIT} characters but only "
              f"{PARTS} hooks deliver them, so the rest never reaches the model; shorten the personal LUMO.md",
              file=sys.stderr)
    for i, part in enumerate(parts, 1):
        write(os.path.join(folder, f"{i}.md"), part)
    for name in os.listdir(folder):
        stem = os.path.splitext(name)[0]
        if stem.isdigit() and int(stem) > len(parts):
            try:
                os.remove(os.path.join(folder, name))
            except OSError:
                pass


def write(path, text):
    # Temp file then rename: several sessions may launch at once.
    tmp = f"{path}.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def context_tokens(lumo):
    """The proxy's token window (CONTEXT_TOKENS in proxy/context-fit.mjs), the one value Claude
    Code's compaction window must stay below. Falls back to 131072 with a warning when the file
    or the constant cannot be found (renamed or restructured without this renderer catching up)."""
    path = os.path.join(lumo, "proxy", "context-fit.mjs")
    try:
        with open(path, encoding="utf-8") as f:
            m = re.search(r"\bCONTEXT_TOKENS\s*=\s*(\d+)", f.read())
    except OSError:
        m = None
    if not m:
        print(f"claude-lumo: {path} has no CONTEXT_TOKENS constant; falling back to 131072 for the "
              f"compaction window", file=sys.stderr)
        return 131072
    return int(m.group(1))


def main():
    lumo, state, personal, python = (a.replace("\\", "/") for a in sys.argv[1:5])
    window = context_tokens(lumo) - COMPACT_MARGIN
    with open(os.path.join(lumo, "settings-lumo.json"), encoding="utf-8") as f:
        text = (f.read().replace("{{LUMO_DIR}}", lumo).replace("{{PYTHON}}", python)
                .replace("{{STATE_DIR}}", state).replace("{{LUMO_LOCAL}}", personal)
                .replace("{{AUTO_COMPACT_WINDOW}}", str(window)))
    settings = json.loads(text)
    config_dir = sys.argv[5].replace("\\", "/") if len(sys.argv) > 5 else ""
    mine = os.path.join(personal, "settings.json")
    if os.path.isfile(mine):
        try:
            with open(mine, encoding="utf-8") as f:
                settings = merge(settings, json.load(f))
        except ValueError as e:
            print(f"claude-lumo: ignoring {mine}, it is not valid JSON ({e})", file=sys.stderr)
    if config_dir:
        settings.setdefault("env", {})["CLAUDE_CONFIG_DIR"] = config_dir
    os.makedirs(state, exist_ok=True)
    write(os.path.join(state, "settings.json"), json.dumps(settings, indent=2) + "\n")

    with open(os.path.join(lumo, "LUMO.md"), encoding="utf-8") as f:
        prompt = f.read()
    mine = os.path.join(personal, "LUMO.md")
    if os.path.isfile(mine):
        with open(mine, encoding="utf-8") as f:
            prompt = prompt.rstrip("\n") + "\n\n" + f.read()
    write(os.path.join(state, "system-prompt.md"), prompt)
    write_parts(state, prompt)

    agents = load_agents(os.path.join(personal, "agents"))
    out = os.path.join(state, "agents.json")
    if agents:
        write(out, json.dumps(agents, separators=(",", ":")) + "\n")
    elif os.path.exists(out):
        try:
            os.remove(out)
        except OSError:
            pass


if __name__ == "__main__":
    main()
