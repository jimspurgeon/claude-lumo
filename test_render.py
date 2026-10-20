"""Tests for render.py (personal agents, re-host settings and prompt parts). Run: python test_render.py"""
import json, os, re, subprocess, sys, tempfile, unittest

import render

HERE = os.path.dirname(os.path.abspath(__file__))


def run_render(personal, state, *extra):
    return subprocess.run([sys.executable, os.path.join(HERE, "render.py"), HERE, state, personal, "python", *extra],
                          capture_output=True, text=True, check=True)


def run_script(path, args, env_extra=None, stdin="{}"):
    env = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_LUMO",)}
    env.update(env_extra or {})
    return subprocess.run([sys.executable, os.path.join(HERE, path), *args], input=stdin, env=env,
                          capture_output=True, text=True, encoding="utf-8", check=True)


class PersonalAgents(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.personal = os.path.join(self.tmp.name, "personal")
        self.state = os.path.join(self.tmp.name, "state")
        self.agents = os.path.join(self.personal, "agents")
        os.makedirs(self.agents)
        self.out = os.path.join(self.state, "agents.json")

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, name, text):
        with open(os.path.join(self.agents, name), "w", encoding="utf-8") as f:
            f.write(text)

    def test_no_agents_means_no_file_and_a_stale_one_is_removed(self):
        os.makedirs(self.state)
        with open(self.out, "w") as f:
            f.write("{}")
        run_render(self.personal, self.state)
        self.assertFalse(os.path.exists(self.out))
        self.assertTrue(os.path.exists(os.path.join(self.state, "settings.json")))

    def test_frontmatter_and_body_become_an_agents_entry(self):
        self.add("gatekeeper.md", "---\nname: gatekeeper\ndescription: Reviews \"diffs\" in C:\\repo\n"
                 "model: lumo-max\ntools: Read, Grep, Bash\n---\n\nYou review.\n\n---\n\nStill the prompt.\n")
        self.add("qa.md", "---\nname: qa\ndescription: >\n  Runs the thing\n  and reports.\n"
                 "tools:\n  - Read\n  - Bash\ndisallowedTools: [Edit, 'Write']\n---\nYou test.\n")
        run_render(self.personal, self.state)
        with open(self.out, encoding="utf-8") as f:
            agents = json.load(f)
        self.assertEqual(agents["gatekeeper"], {
            "description": 'Reviews "diffs" in C:\\repo', "model": "lumo-max",
            "tools": ["Read", "Grep", "Bash"], "prompt": "You review.\n\n---\n\nStill the prompt.\n"})
        self.assertEqual(agents["qa"], {"description": "Runs the thing and reports.", "tools": ["Read", "Bash"],
                                        "disallowedTools": ["Edit", "Write"], "prompt": "You test.\n"})

    def test_name_defaults_to_the_file_name_and_bad_files_are_skipped(self):
        self.add("reviewer.md", "---\ndescription: Reviews\n---\nBody\n")
        self.add("broken.md", "no frontmatter here\n")
        self.add("nodesc.md", "---\nname: nodesc\n---\nBody\n")
        result = run_render(self.personal, self.state)
        with open(self.out, encoding="utf-8") as f:
            self.assertEqual(list(json.load(f)), ["reviewer"])
        self.assertIn("broken.md", result.stderr)
        self.assertIn("nodesc.md", result.stderr)

    def test_agents_past_the_command_line_limit_are_skipped(self):
        self.add("a.md", "---\ndescription: small\n---\nshort\n")
        self.add("b.md", "---\ndescription: big\n---\n" + "x" * render.AGENTS_LIMIT + "\n")
        result = run_render(self.personal, self.state)
        with open(self.out, encoding="utf-8") as f:
            self.assertEqual(list(json.load(f)), ["a"])
        self.assertIn("skipping agent b", result.stderr)

    def test_agents_limit_can_be_overridden_by_environment(self):
        # AGENTS_LIMIT is read at import time, so check it in a fresh interpreter with the variable set.
        out = subprocess.run([sys.executable, "-c", "import render; print(render.AGENTS_LIMIT)"],
                              capture_output=True, text=True, check=True, cwd=HERE,
                              env={**os.environ, "LUMO_AGENTS_LIMIT": "4321"}).stdout.strip()
        self.assertEqual(out, "4321")


class RehostedSessions(unittest.TestCase):
    """#77: a session Claude Code re-hosts by itself keeps --settings but loses the launcher's
    environment and every flag but --settings, so the settings must carry the env and the prompt hooks."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.personal = os.path.join(self.tmp.name, "personal").replace("\\", "/")
        self.state = os.path.join(self.tmp.name, "state").replace("\\", "/")
        os.makedirs(self.personal)

    def tearDown(self):
        self.tmp.cleanup()

    def settings(self, *extra):
        run_render(self.personal, self.state, *extra)
        with open(os.path.join(self.state, "settings.json"), encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("{{", text)
        return json.loads(text)

    def parts(self):
        folder = os.path.join(self.state, "prompt-parts")
        names = sorted(os.listdir(folder), key=lambda n: int(n.split(".")[0]))
        out = []
        for name in names:
            with open(os.path.join(folder, name), encoding="utf-8") as f:
                out.append(f.read())
        return out

    def test_settings_carry_the_launcher_values(self):
        s = self.settings()
        self.assertEqual(s["env"]["CLAUDE_LUMO"], "1")
        self.assertEqual(s["env"]["CLAUDE_LUMO_LOCAL"], self.personal)
        self.assertIn(f"scripts/api-key.py {self.state}", s["apiKeyHelper"])
        self.assertTrue(s["statusLine"]["command"].endswith("statusline.py --lumo"))
        self.assertNotIn("CLAUDE_CONFIG_DIR", s["env"])

    def test_config_dir_is_carried_when_given(self):
        s = self.settings("C:\\x\\data")
        self.assertEqual(s["env"]["CLAUDE_CONFIG_DIR"], "C:/x/data")
        self.assertNotIn("CLAUDE_CONFIG_DIR", self.settings("")["env"])

    def test_prompt_hooks_are_registered_for_every_part(self):
        hooks = [h for entry in self.settings()["hooks"]["SessionStart"] for h in entry["hooks"]
                 if "lumo-prompt.py" in h["command"]]
        self.assertEqual([h["command"].split()[-1] for h in hooks],
                         [str(i) for i in range(1, render.PARTS + 1)])
        entry = next(e for e in self.settings()["hooks"]["SessionStart"]
                     if any("lumo-prompt.py" in h["command"] for h in e["hooks"]))
        # Not resume/fork: a resumed transcript already restores the system prompt it was started with.
        self.assertEqual(entry["matcher"], "startup|clear|compact|resume|fork")

    def test_parts_stay_under_the_hook_output_cap_and_rebuild_the_prompt(self):
        # Within the PARTS cap (overflow behaviour is covered by PromptOverflow below).
        with open(os.path.join(self.personal, "LUMO.md"), "w", encoding="utf-8") as f:
            f.write("# Personal\n\n" + ("A rule that is long enough. " * 9 + "\n") * 80)
        self.settings()
        with open(os.path.join(self.state, "system-prompt.md"), encoding="utf-8") as f:
            prompt = f.read()
        parts = self.parts()
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= render.PART_LIMIT < 10000 for p in parts))
        self.assertEqual("".join(p.split("):\n\n", 1)[1] for p in parts), prompt)
        self.assertTrue(parts[0].startswith(f"claude-lumo system prompt, part 1 of {len(parts)}"))

    def test_stale_parts_are_removed(self):
        os.makedirs(os.path.join(self.state, "prompt-parts"))
        with open(os.path.join(self.state, "prompt-parts", "4.md"), "w") as f:
            f.write("STALE-PART-77")
        self.settings()
        self.assertEqual(len(os.listdir(os.path.join(self.state, "prompt-parts"))), len(self.parts()))
        self.assertNotIn("STALE-PART-77", "".join(self.parts()))

    def test_hook_prints_its_part_whatever_the_environment(self):
        # The hook is the only source of the prompt, so no inherited variable may silence it.
        self.settings()
        out = run_script("hooks/lumo-prompt.py", [self.state, "1"]).stdout
        self.assertEqual(out, self.parts()[0])
        self.assertIn("Running on Proton Lumo", out)
        inherited = run_script("hooks/lumo-prompt.py", [self.state, "1"], {"CLAUDE_LUMO": "1", "CLAUDE_LUMO_WRAPPER": "1"}).stdout
        self.assertEqual(inherited, out)
        self.assertEqual(run_script("hooks/lumo-prompt.py", [self.state, "99"]).stdout, "")

    def test_hook_on_resume_or_fork_only_when_the_transcript_lacks_the_prompt(self):
        self.settings()
        part = self.parts()[0]
        body_line = next(l for l in part.split("\n\n", 1)[1].splitlines() if l.strip()).strip()
        attachment = lambda kind, content: json.dumps(
            {"type": "attachment", "attachment": {"type": kind, "content": content}}) + "\n"
        read_source = json.dumps({"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "header = (\"claude-lumo system prompt, part {} of {}\"\n" + body_line}]}}) + "\n"
        hook = attachment("hook_success", part)
        boundary = json.dumps({"type": "system", "subtype": "compact_boundary"}) + "\n"
        cases = {
            "empty": ("", part),
            "hook output": (hook, ""),
            "non-ASCII hook output": (attachment("hook_success", part + " é"), ""),
            # A pre-#77 session had it from a launcher flag that a resume no longer passes.
            "pre-#77 prompt_snapshot": (attachment("prompt_snapshot", "x\n" + body_line), part),
            "compacted after the hook": (hook + boundary, part),
            "hook again after compaction": (hook + boundary + hook, ""),
            "only read the source": (read_source, part),
            "attached as a file": (attachment("file", part), part),
            "bad lines around hook output": ("not json\n\"attachment\"\n" + hook, ""),
        }
        for n, (name, (content, expected)) in enumerate(cases.items()):
            transcript = os.path.join(self.state, f"t-{n}.jsonl")
            with open(transcript, "w", encoding="utf-8") as f:
                f.write(content)
            for source in ("resume", "fork"):
                event = json.dumps({"source": source, "transcript_path": transcript})
                out = run_script("hooks/lumo-prompt.py", [self.state, "1"], stdin=event).stdout
                self.assertEqual(out, expected, f"{name} / {source}")
        startup = json.dumps({"source": "startup", "transcript_path": transcript})
        self.assertEqual(run_script("hooks/lumo-prompt.py", [self.state, "1"], stdin=startup).stdout, part)
        missing = json.dumps({"source": "resume", "transcript_path": os.path.join(self.state, "nope.jsonl")})
        self.assertEqual(run_script("hooks/lumo-prompt.py", [self.state, "1"], stdin=missing).stdout, part)

    def test_launcher_passes_no_prompt_flag(self):
        with open(os.path.join(HERE, "claude-lumo.sh"), encoding="utf-8") as f:
            launcher = f.read()
        self.assertNotIn("--append-system-prompt", launcher)
        self.assertNotIn("CLAUDE_LUMO_WRAPPER", launcher)
        # The key comes only from apiKeyHelper; a token in the environment would win over it (#86).
        self.assertNotIn("export ANTHROPIC_AUTH_TOKEN", launcher)
        self.assertIn("unset ANTHROPIC_AUTH_TOKEN ANTHROPIC_API_KEY", launcher)

    def test_compact_window_stays_below_the_proxy_token_window(self):
        # The proxy clamps replies once the prompt nears CONTEXT_TOKENS, so Claude Code must compact
        # before that point: the window is CONTEXT_TOKENS minus a margin of at least 12000.
        s = self.settings()
        context = render.context_tokens(HERE)
        self.assertLessEqual(s["autoCompactWindow"], context - render.COMPACT_MARGIN)
        self.assertGreaterEqual(context - s["autoCompactWindow"], 12000)
        self.assertLessEqual(s["autoCompactWindow"], 119072)

    def test_settings_template_has_no_duplicate_keys(self):
        # json.load keeps only the last of two equal keys, which once dropped a SessionStart hook.
        def no_dupes(pairs):
            keys = [k for k, _ in pairs]
            dupes = {k for k in keys if keys.count(k) > 1}
            self.assertFalse(dupes, f"duplicate keys: {dupes}")
            return dict(pairs)
        with open(os.path.join(HERE, "settings-lumo.json"), encoding="utf-8") as f:
            # Placeholders are filled in by render.py before this file is parsed as JSON.
            text = (f.read().replace("{{LUMO_LOCAL}}", "x").replace("{{PYTHON}}", "x")
                    .replace("{{LUMO_DIR}}", "x").replace("{{STATE_DIR}}", "x")
                    .replace("{{AUTO_COMPACT_WINDOW}}", "0"))
        json.loads(text, object_pairs_hook=no_dupes)

    def test_settings_deny_credential_and_cookie_reads(self):
        # #136: a Lumo-specific deny floor. Lumo sessions inherit every user allow rule, so
        # without denies a weaker model inherits allowances written for Opus; these block reads
        # of the master key, the manual login fallback, Claude Code credentials and any .env.
        deny = self.settings()["permissions"]["deny"]
        for rule in ("Read(~/.claude-lumo/master.key)", "Read(~/.claude-lumo/login.json)",
                     "Read(~/.claude/.credentials.json)", "Read(~/.claude.json)",
                     "Read(//**/.env)", "Read(//**/.env.*)"):
            self.assertIn(rule, deny)
        # Browser cookie stores hold the AUTH-<uid> Lumo login the proxy reads (proxy/creds.mjs),
        # so they are denied too, on every OS. Windows paths are matched in POSIX form (//c/...).
        for rule in ("Read(//c/Users/*/AppData/**/Cookies)",
                     "Read(//c/Users/*/AppData/**/cookies.sqlite)",
                     "Read(~/Library/Application Support/**/Cookies)",
                     "Read(~/Library/Application Support/**/cookies.sqlite)",
                     "Read(~/.mozilla/**/cookies.sqlite)",
                     "Read(~/.librewolf/**/cookies.sqlite)",
                     "Read(~/.config/**/Cookies)"):
            self.assertIn(rule, deny)

    def test_api_key_helper_prints_the_key(self):
        os.makedirs(self.state)
        with open(os.path.join(self.state, "master.key"), "w") as f:
            f.write("sk-lumo-test\n")
        self.assertEqual(run_script("scripts/api-key.py", [self.state]).stdout, "sk-lumo-test\n")

    def test_statusline_flag_shows_lumo_without_the_env(self):
        env = {"CLAUDE_LUMO_STATE_DIR": self.state, "CLAUDE_LUMO_LOCAL": self.personal,
               "CLAUDE_LUMO_UPDATE_CHECK": "0"}  # no real update check from a test (#120)
        self.assertIn("[LUMO]", run_script("statusline.py", ["--lumo"], env).stdout)
        self.assertIn("[CLAUDE]", run_script("statusline.py", [], env).stdout)


class PromptOverflow(unittest.TestCase):
    """Audit 4: a prompt over PARTS * PART_LIMIT used to lose parts silently; now it is trimmed
    with a visible notice, or fails loudly with LUMO_FAIL_ON_OVERFLOW=1."""

    LUMO = ("A rule that is long enough to fill parts. " * 20 + "\n")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.personal = os.path.join(self.tmp.name, "personal")
        self.state = os.path.join(self.tmp.name, "state")
        os.makedirs(self.personal)

    def tearDown(self):
        self.tmp.cleanup()

    def oversized(self):
        # Enough text that the split prompt needs more than PARTS parts.
        with open(os.path.join(self.personal, "LUMO.md"), "w", encoding="utf-8") as f:
            f.write("# Personal\n\n" + self.LUMO * (render.PARTS * render.PART_LIMIT // 800))

    def run_overflow(self, env_extra=None):
        self.oversized()
        env = {**os.environ, **(env_extra or {})}
        return subprocess.run([sys.executable, os.path.join(HERE, "render.py"), HERE, self.state,
                               self.personal, "python"], capture_output=True, text=True, env=env)

    def parts(self):
        folder = os.path.join(self.state, "prompt-parts")
        return sorted(os.listdir(folder), key=lambda n: int(n.split(".")[0]))

    def test_overflow_is_trimmed_to_parts_with_a_notice(self):
        result = self.run_overflow()
        self.assertEqual(result.returncode, 0)
        files = self.parts()
        self.assertEqual(files, [f"{i}.md" for i in range(1, render.PARTS + 1)])
        texts = []
        for name in files:
            with open(os.path.join(self.state, "prompt-parts", name), encoding="utf-8") as f:
                texts.append(f.read())
        # The last delivered part is cut to fit and ends with the visible truncation notice.
        self.assertTrue(all(len(t) <= render.PART_LIMIT for t in texts))
        lost = int(re.search(r"(\d+) chars lost", texts[-1]).group(1))
        self.assertGreater(lost, 0)
        self.assertIn(f"[claude-lumo: system prompt trimmed, {lost} chars lost; shorten LUMO.md]",
                      texts[-1])
        self.assertNotIn("never reaches the model", "".join(texts))
        # The stderr error names the personal LUMO.md and the excess character count.
        self.assertIn("LUMO.md", result.stderr)
        self.assertIn(str(lost), result.stderr)

    def test_overflow_fails_with_the_fail_flag(self):
        result = self.run_overflow({"LUMO_FAIL_ON_OVERFLOW": "1"})
        self.assertEqual(result.returncode, 1)
        self.assertIn("LUMO.md", result.stderr)
        self.assertIn("characters over", result.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.state, "prompt-parts")))


class SplitPrompt(unittest.TestCase):
    def test_a_line_longer_than_a_part_is_cut(self):
        text = "short\n" + "y" * 20000 + "\nend\n"
        parts = render.split_prompt(text, limit=5000)
        self.assertTrue(all(len(p) <= 5000 for p in parts))
        self.assertEqual("".join(p.split("):\n\n", 1)[1] for p in parts), text)


if __name__ == "__main__":
    unittest.main()
