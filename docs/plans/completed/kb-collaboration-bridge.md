# KB collaboration bridge

## Objective

Provide an optional bridge from the knowledge-base harness to the shared worktree and task-coordination convention. The bridge is a soft agent convention across tools; runtime checks can reject inconsistent state but do not provide a system security boundary or automatic notification.

## Committed file whitelist

- `README.md`
- `SKILL.md`
- `refs/agent-docs-layout.md`
- `refs/agents-template.md`
- `tests/test_agent_docs_layout.py` — preserve LF in isolated hook fixtures on Windows
- `tests/test_worktree_task_integration.py` — exercise the shared helper in temporary Git worktrees
- `docs/plans/completed/kb-collaboration-bridge.md`

No other files are in scope.

## Delivered behavior

- Phase 0 records an optional coordination choice for Git repositories. Phase 1 installs the common helper only when selected and preserves existing route decisions and user-written scope rules when generating AGENTS.md.
- The KB layout migration reuses `.meta/scripts/worktree_task.py` and the Git common-dir registry; it does not bundle a second runtime or add a coordination hook.
- The generated AGENTS guidance covers listing open tasks, creating or resuming an owned task, recording intent, reviewing scope, coordinating affected tasks, admission, and checking before writes. It preserves primary-host precedence and states the limits of owner labels, digests, linked worktrees, and cross-clone sharing.
- Read-only work does not create a task worktree. A non-Git installation does not install the collaboration helper or Git hooks.

## Verification

- `tests.test_agent_docs_layout`: 7 tests passed using the upstream `AGENT_DOCS_ASSETS` and Git for Windows Bash.
- `tests.test_worktree_task_integration`: 1 test passed against upstream helper SHA256 `525420855BBC14195B6497912119AC3A2F4886F4FFB1BA7ADBF42D29E66A08F7`. The isolated fixture created and resumed a task, recorded intent, and read list/review state from both linked worktrees; both observed one common-dir task record, and read-only calls left its bytes unchanged.
- Skill `quick_validate.py`: valid.
- `git diff --check`: passed; changed-file path scan found no personal paths.
- Three independent final reviews reported no blocking findings.

## Completion boundary

This verifies the skill guidance and the helper integration in isolated fixtures. It does not claim installation into a real vault, a full knowledge-base bootstrap, deployment, push, or integration into another worktree.
