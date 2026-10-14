# How we write release notes

Purpose: keep every release note consistent while preserving all technical facts (flags, file names, issue numbers, caveats).

## Title format

- Exactly: `claude-lumo X.Y.Z`
- Match the tag number; no `v` prefix.
- No descriptive suffix.

## Body structure

1. **Heading** (first line): `## claude-lumo X.Y.Z`

2. **Opening sentence**: One plain sentence describing what the release is.
   - Example: `Bug-fix release on top of 1.4.0.`
   - Example: `WebSearch-count fix and generic wipe/audit prompts on top of 1.4.2.`
   - Use "on top of X.Y.Z" only when this release builds directly on a specific prior release.

3. **Bulleted changes**: One bullet per issue or PR-grouped change.
   - Start with `**Short name (#NN).**` in bold.
   - Follow with 1–3 sentences of plain explanation.
   - Preserve all technical facts: file names, flag names, caveats like "Takes effect after LiteLLM restarts".

   Example:

   - **WebSearch "Did 0 searches" (#111).** New `proxy/lumo_websearch.py` adds `server_tool_use` and `web_search_tool_result` blocks so Claude Code counts searches correctly. Takes effect after LiteLLM restarts.

4. **Footer lines** (blank line, then in order when applicable):
   - `Closes #N.` or `Refs #N.` for each closed/referenced issue.
   - `**Tests:**` one line with pass/skip counts (e.g. `pytest 356 passed, 1 skipped; node 56 passed.`).
   - Operational note only when genuinely needed (e.g. `Restart the proxy after updating.`).

## What never appears

- Install curl commands.
- "What's Changed" GitHub boilerplate.
- PR-link credit lists.
- "Full Changelog" compare links.
- Contributor sections.
- Marketing tone or superlatives.
- Em-dashes.

## Small releases

For single-change or license-only releases, use the sentence plus one bullet. Do not inflate.

Example for v1.2.5:

    ## claude-lumo 1.2.5

    Adds the GNU AGPL-3.0 license to the repository.

    - **License (#5).** Adds GNU AGPL-3.0 license, formally granting use, modification and redistribution rights, and disclaiming all warranties.

## Stale framing

Where old notes contain framing that is now stale (e.g. "Superseded by v1.2+"), keep the note but reword to plain style.

## Registering new releases

- Tag naming: `vX.Y.Z` (git tag).
- Title format: `claude-lumo X.Y.Z` (used in release title and body heading).
- When to use "on top of X.Y.Z": when this release directly builds on a specific prior release and the opening sentence should convey lineage.
