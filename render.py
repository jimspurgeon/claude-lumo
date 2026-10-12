"""Build this machine's claude-lumo settings and system prompt (called by claude-lumo.sh at launch).

    python render.py <lumo dir> <state dir> <personal dir> <python command>

Writes <state dir>/settings.json and <state dir>/system-prompt.md. Tracked files in this repo are
only read, so a clone stays clean and `git pull` keeps working.

Personal additions (the personal dir, default ~/.claude/lumo-local) are optional, only read, and
always win:
- settings.json is merged over settings-lumo.json: objects merge key by key, lists are appended
  (so personal hooks or permissions add to ours), anything else is replaced.
- LUMO.md is appended after this repo's LUMO.md.
- agents/*.md are Claude Code agent files (frontmatter plus prompt). They are written to
  <state dir>/agents.json, which the launcher passes to claude with --agents, so in claude-lumo they
  replace the agents of the same name in ~/.claude/agents. No such files: no agents.json.
"""
import glob, json, os, sys

# Claude reads --agents from a file only with --print, so for normal sessions the agents travel
# inline on the command line. Windows caps a whole command line at 32,767 characters; keep room for
# the rest of it. Linux caps one argument at 128 KiB.
AGENTS_LIMIT = 24000 if os.name == "nt" else 100000
# Frontmatter fields passed through to claude; the Markdown body becomes the prompt.
AGENT_FIELDS = ("description", "tools", "disallowedTools", "model")


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


def write(path, text):
    # Temp file then rename: several sessions may launch at once.
    tmp = f"{path}.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def main():
    lumo, state, personal, python = (a.replace("\\", "/") for a in sys.argv[1:5])
    with open(os.path.join(lumo, "settings-lumo.json"), encoding="utf-8") as f:
        text = f.read().replace("{{LUMO_DIR}}", lumo).replace("{{PYTHON}}", python)
    settings = json.loads(text)
    mine = os.path.join(personal, "settings.json")
    if os.path.isfile(mine):
        try:
            with open(mine, encoding="utf-8") as f:
                settings = merge(settings, json.load(f))
        except ValueError as e:
            print(f"claude-lumo: ignoring {mine}, it is not valid JSON ({e})", file=sys.stderr)
    os.makedirs(state, exist_ok=True)
    write(os.path.join(state, "settings.json"), json.dumps(settings, indent=2) + "\n")

    with open(os.path.join(lumo, "LUMO.md"), encoding="utf-8") as f:
        prompt = f.read()
    mine = os.path.join(personal, "LUMO.md")
    if os.path.isfile(mine):
        with open(mine, encoding="utf-8") as f:
            prompt = prompt.rstrip("\n") + "\n\n" + f.read()
    write(os.path.join(state, "system-prompt.md"), prompt)

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
