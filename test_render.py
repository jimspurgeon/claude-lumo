"""Tests for render.py's personal agents. Run: python test_render.py"""
import json, os, subprocess, sys, tempfile, unittest

import render

HERE = os.path.dirname(os.path.abspath(__file__))


def run_render(personal, state):
    return subprocess.run([sys.executable, os.path.join(HERE, "render.py"), HERE, state, personal, "python"],
                          capture_output=True, text=True, check=True)


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


if __name__ == "__main__":
    unittest.main()
