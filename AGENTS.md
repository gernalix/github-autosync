# Repository entrypoint

Read `project-capsule.yaml` for repository identity, scope, verification commands, runtime components, and safety boundaries. The detailed operating contract is in `README.md`.

## Core boundaries

- `github-autosync` synchronizes only its explicit managed repository allowlist. Do not expand it implicitly.
- Keep canonical branches protected. Use isolated `task/*` worktrees and `repo-task` for repository changes; `repo-integrator` owns queued PR integration.
- `codex-roadmap` keeps its dedicated guarded pull and single-writer lifecycle. Do not mutate its database or materialized state directly.
- Preserve user work and external state. Do not stash, reset, force-pull, or force-push.
- Treat systemd installation, deployment, and forced network reconciliation as operational actions; use the documented commands only when explicitly required.
