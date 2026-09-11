# github-autosync

Dedicated Fedora-side synchronizer for repositories owned by `gernalix`.

## Responsibilities

- discover all current and future GitHub repositories owned by `gernalix` with one metadata query per timer run;
- persist a compact fingerprint per repository in `/home/daniele/.local/state/codex-github-autosync/repo-state.json`;
- clone missing repositories under `/home/daniele/projects/<repo>`;
- fetch/update only repositories whose GitHub metadata fingerprint changed;
- skip unchanged repositories without running `git fetch`/pull on them;
- audit active canonical worktrees from MegaVault without a global fetch pass;
- automatically push only clean, ahead-only repositories using a normal non-force push;
- never stash, reset, force-pull or force-push dirty/ahead/diverged repositories;
- register genuinely new repositories in MegaVault when its worktree is clean and synchronized;
- expose truthful machine-readable states: `ok`, `partial`, `deferred`, `error`, or `locked`;
- notify through the shared `telegram_notify` package when unresolved sync problems change;
- run from a `systemd --user` timer every 5 minutes.

The periodic path no longer uses `ghorg --fetch-all`. `ghorg` can still be used manually for bootstrap/recovery, but it is not part of the steady-state timer.

## Change detection

Each run executes one command equivalent to:

```bash
gh repo list gernalix --limit 1000 --json name,url,defaultBranchRef,pushedAt,isArchived
```

The fingerprint uses remote URL, default branch, `pushedAt`, and archived state. If the fingerprint is unchanged and the worktree exists, the repository is skipped with no fetch/pull. A new or changed repository alone enters the Git reconciliation path.

## Git safety policy

For a repository selected as changed:

- **missing**: clone it directly;
- **clean + behind only**: fetch that repository and fast-forward with `git merge --ff-only @{u}`;
- **clean + ahead only**: normal `git push`, never `--force`; a remote race is rejected by Git;
- **clean + synced**: no mutation;
- **dirty**, **diverged**, **detached**, **no upstream**, **wrong origin**, or failed relation check: no destructive recovery; report it as deferred/error as appropriate.

The lightweight inventory audit does not fetch every repository. It may safely push a clean ahead-only checkout against the already-known upstream ref; Git still rejects a non-fast-forward remote race.

## Status semantics

The final JSON distinguishes outcomes explicitly:

- `status=ok`: all expected phases completed and there are no unresolved issues;
- `status=partial`: useful work completed, but at least one repository/MegaVault action was deferred;
- `status=deferred`: nothing needed could be safely completed because expected conditions were deferred;
- `status=error`: a real operational failure; exit code is non-zero;
- `status=locked`: another autosync run owns the lock; exit code remains zero.

Expected MegaVault conditions such as `deferred_dirty` and `deferred_not_synced` therefore never appear as a false `ok`, while remaining exit-code 0 so the timer does not treat a safe defer as a service crash.

## Telegram alerts

Notifications use the existing shared top-level Python package:

```text
python3 -m telegram_notify
```

The package resolves its configured default destination; this repository does not store or print Telegram secrets or chat IDs. Persistent alerts remain deduplicated in:

```text
/home/daniele/.local/state/codex-github-autosync/telegram-alert-state.json
```

`--dry-run` and explicit `--no-telegram` never send or advance Telegram alert state.

## Runtime

Canonical Fedora checkout:

```text
/home/daniele/projects/github-autosync
```

User units:

```text
github-autosync.service
github-autosync.timer
```

The timer cadence stays at 5 minutes; efficiency comes from doing less work per tick, not from checking less often.

## Verification

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
python3 github_autosync.py --dry-run --no-telegram run
systemctl --user status github-autosync.timer --no-pager
systemctl --user list-timers github-autosync.timer --all --no-pager
```

Unit coverage verifies that an unchanged second run performs no repository sync, only a changed repository is updated, a new repository is cloned, dirty/diverged state is preserved, expected MegaVault defers are not reported as `ok`, and real GitHub failures remain non-zero.
