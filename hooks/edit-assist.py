"""PreToolUse hook for Edit: when old_string is not in the file, block the call and show the
model the exact text it was aiming at, so the retry copies it instead of guessing again.

Silent (exit 0, no output) whenever the edit would apply, so it never changes a working edit.
Built for claude-lumo, whose models rebuild old_string from memory (dropped words, an import that
is not there, typographic dashes and quotes), but harmless on Claude too.
"""
import difflib, json, os, sys

TYPO = str.maketrans({"—": "-", "–": "-", "‘": "'", "’": "'", "“": '"',
                      "”": '"', " ": " ", "…": "..."})
MAX_BYTES = 2_000_000
MAX_SHOW = 60
NL = "\n"


def norm(line):
    return " ".join(line.translate(TYPO).split())


def deny(reason):
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse",
                                             "permissionDecision": "deny",
                                             "permissionDecisionReason": reason}}))
    sys.exit(0)


def main():
    try:
        # Claude Code sends UTF-8; on Windows sys.stdin would decode it as cp1252 and turn every
        # non-ASCII character (em dash, curly quote) into mojibake, so a valid edit looked wrong.
        inp = json.loads(sys.stdin.buffer.read().decode("utf-8")).get("tool_input") or {}
        path, old = inp.get("file_path"), inp.get("old_string")
        if not path or not old or not os.path.isfile(path) or os.path.getsize(path) > MAX_BYTES:
            return
        text = open(path, encoding="utf-8", errors="replace").read().replace("\r\n", NL)
    except Exception:  # noqa: BLE001 - never block an edit because the hook failed
        return
    old = old.replace("\r\n", NL)
    if old in text:
        return

    lines, want = text.split(NL), old.strip(NL).split(NL)
    n = len(want)
    if n > len(lines):
        return
    target = [norm(l) for l in want]
    normed = [norm(l) for l in lines]

    # Same lines once whitespace and typographic dashes/quotes are ignored.
    hits = [i for i in range(len(lines) - n + 1) if normed[i:i + n] == target]
    if len(hits) == 1:
        i = hits[0]
        deny("old_string is not in the file verbatim; it differs only in whitespace or in dashes/quotes. "
             f"The exact text (lines {i + 1}-{i + n}) is below between the markers. Retry the Edit with "
             "old_string copied from it exactly, without the markers:" + NL + "<<<" + NL
             + NL.join(lines[i:i + n]) + NL + ">>>")

    # Otherwise show the closest block, letting it be up to 2 lines shorter or longer than old_string
    # (the model often drops or invents a line).
    joined = NL.join(target)
    best, best_i, best_len = 0.0, -1, n
    for size in range(max(1, n - 2), n + 3):
        for i in range(len(lines) - size + 1):
            m = difflib.SequenceMatcher(None, joined, NL.join(normed[i:i + size]), autojunk=False)
            if m.real_quick_ratio() <= best or m.quick_ratio() <= best:
                continue
            r = m.ratio()
            if r > best:
                best, best_i, best_len = r, i, size
    if best < 0.5 or n > MAX_SHOW:
        deny("old_string was not found in the file and no close match exists. The file has changed since you "
             "last read it, or the text was recalled from memory. Read the exact lines again before editing.")
    real = lines[best_i:best_i + best_len]
    diff = []
    for op, a1, a2, b1, b2 in difflib.SequenceMatcher(None, target, normed[best_i:best_i + best_len]).get_opcodes():
        if op != "equal":
            diff += [f"  yours: {w}" for w in want[a1:a2]] + [f"  file:  {f}" for f in real[b1:b2]]
    deny(f"old_string was not found in the file. Closest text is lines {best_i + 1}-{best_i + best_len} "
         f"({best:.0%} similar). Lines that differ:" + NL + NL.join(diff[:16]) + NL
         + "If this is the spot you meant, retry with old_string copied exactly from between the markers "
         "(without them); if not, Read the file again." + NL + "<<<" + NL + NL.join(real) + NL + ">>>")


if __name__ == "__main__":
    main()
