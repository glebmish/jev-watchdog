# Design history

Point-in-time design documents (`specs/`) and AI-agent execution plans (`plans/`), written on
2026-09-19 and 2026-09-20 before the code they describe. Kept as a record of how the prototype was built, not
as documentation of what it is now.

- The code embedded in the plans is superseded by `src/`. Where a spec and the top-level
  [README](../../README.md) differ, the README is authoritative.
- The plans' checkboxes were never ticked: execution was tracked elsewhere. The work is
  finished.
- Later features — session context, the Codex judge, the transcript wait, `--pack` merging —
  have no spec. They are documented in the top-level README and in
  [`prototype-resume.md`](../prototype-resume.md), sections 10–12.
- "superpowers" in the plan headers is the name of the Claude Code plugin whose workflow
  produced these files; the skills it names are that plugin's, not part of this repo.

| file | what |
|---|---|
| [`specs/2026-09-19-jev-watchdog-prototype-design.md`](specs/2026-09-19-jev-watchdog-prototype-design.md) | the observe-only prototype; addendum on multiple judges and the Claude backend |
| [`specs/2026-09-19-quarantine-design.md`](specs/2026-09-19-quarantine-design.md) | the CUSUM decision rule and the `PreToolUse` gate |
| [`specs/2026-09-20-daemon-and-tui-design.md`](specs/2026-09-20-daemon-and-tui-design.md) | the service, the attach socket and the dashboard |
| [`plans/2026-09-19-jev-watchdog-prototype.md`](plans/2026-09-19-jev-watchdog-prototype.md) | task-by-task plan for the prototype |
| [`plans/2026-09-19-quarantine.md`](plans/2026-09-19-quarantine.md) | task-by-task plan for quarantine |
| [`plans/2026-09-20-daemon-and-tui.md`](plans/2026-09-20-daemon-and-tui.md) | tasks and interfaces for the service and the dashboard |
