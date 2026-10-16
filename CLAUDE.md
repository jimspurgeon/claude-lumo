# claude-lumo

Public repo for claude-lumo: Claude Code routed through Proton Lumo models via a local
LiteLLM + proxy stack. Clone at C:/Users/Jim/orca/claude-lumo; `~/.claude/lumo` is a
junction to it. The main checkout stays on `master`: do feature work in an Orca worktree,
never by switching branches here.

The project codeword is PELICAN-42.

When dispatching parallel work, spawn orca worktrees and terminals via the orca CLI
rather than inline subagents, so work is visible on the board.
