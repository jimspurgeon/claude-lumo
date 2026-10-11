"""Build this machine's claude-lumo settings and system prompt (called by claude-lumo.sh at launch).

    python render.py <lumo dir> <state dir> <personal dir> <python command>

Writes <state dir>/settings.json and <state dir>/system-prompt.md. Tracked files in this repo are
only read, so a clone stays clean and updates keep working.

Personal additions (the personal dir, default ~/.claude/lumo-local) are optional, only read, and
always win:
- settings.json is merged over settings-lumo.json: objects merge key by key, lists are appended
  (so personal hooks or permissions add to ours), anything else is replaced.
- LUMO.md is appended after this repo's LUMO.md.
"""
import json, os, sys


def merge(base, extra):
    if isinstance(base, dict) and isinstance(extra, dict):
        out = dict(base)
        for k, v in extra.items():
            out[k] = merge(base[k], v) if k in base else v
        return out
    if isinstance(base, list) and isinstance(extra, list):
        return base + [x for x in extra if x not in base]
    return extra


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


if __name__ == "__main__":
    main()
