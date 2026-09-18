# github-autosync

Dedicated Fedora-side synchronizer for a fixed allowlist of repositories owned by `gernalix`.

## Managed repositories

The service manages **only** these repositories:

- `gernalix/codex-roadmap`
- `gernalix/vm_oracle`
- `gernalix/MegaVault`
- `gernalix/fedora-system-monitor`
- `gernalix/codex-usage`
- `gernalix/github-autosync`
- `gernalix/PersonalHub`
- `gernalix/codex-usage-monitor`
- `gernalix/fedora-t7-backup`
- `gernalix/amici_fb`
- `gernalix/salute`

All other current or future GitHub repositories are ignored completely until explicitly added to `ALLOWED_REPOSITORIES`. They are not cloned, fetched, pulled, pushed, audited, registered in MegaVault, persisted in repo state, or included in Telegram alerts.

## Responsibilities

- execute one GitHub metadata discovery query per timer run, then immediately filter it to the managed allowlist;
- persist a compact fingerprint only for managed repositories in `/home/daniele/.local/state/codex-github-autosync/repo-state.json`;
- clone a missing managed repository into its MegaVault canonical worktree when one is registered, otherwise under `/home/daniele/projects/<repo>`;
- fetch/update a managed repository when its GitHub metadata fingerprint changes;
- perform a lightweight **local-only audit** even when the remote fingerprint is unchanged, so dirty worktrees and local commits are not hidden by the remote fast path;
- automatically push clean, ahead-only managed repositories using a normal non-force push;
- append every successful automatic repository mutation (`clone`, fast-forward `pull`, `push`) to a durable JSONL activity ledger;
- send one Telegram notification for every automatic `push` or fast-forward `pull`, with a persistent delivery cursor so failed sends are retried on a later run;
- skip network reconciliation for unchanged, clean, synchronized repositories;
- never stash, reset, force-pull or force-push dirty/ahead/diverged repositories;
- register genuinely new managed repositories in MegaVault when its worktree is clean and synchronized;
- expose truthful machine-readable states: `ok`, `partial`, `deferred`, `error`, or `locked`;
- notify through the shared `telegram_notify` package when unresolved sync problems change;
- run from a `systemd --user` timer every 5 minutes.

The periodic path no longer uses `ghorg --fetch-all`. `ghorg` can still be used manually for bootstrap/recovery, but it is not part of the steady-state timer.

## Change detection and local audit

Each run executes one command equivalent to:

```bash
gh repo list gernalix --limit 1000 --json name,url,defaultBranchRef,pushedAt,isArchived
```

The result is filtered immediately to the allowlist. The fingerprint uses remote URL, default branch, `pushedAt`, and archived state.

If a managed repository's fingerprint changed, that repository enters the network Git reconciliation path. If the fingerprint is unchanged, `github-autosync` still checks the selected local worktree without fetching: origin, branch/upstream, dirty state, and the already-known ahead/behind relation are inspected. This catches local-only changes such as commits created by Codex. A clean ahead-only checkout can therefore be pushed even though GitHub's `pushedAt` has not changed yet.

An unchanged, clean, synchronized checkout is counted as `audited_unchanged` and then `skipped_unchanged`: it avoids `git fetch`/pull while remaining visible to the local safety audit.

## Git safety policy

For a managed repository selected as remotely changed:

- **missing**: clone it directly into the canonical MegaVault worktree when known, otherwise `/home/daniele/projects/<repo>`;
- **clean + behind only**: fetch that repository and fast-forward with `git merge --ff-only @{u}`;
- **clean + ahead only**: normal `git push`, never `--force`; a remote race is rejected by Git;
- **clean + synced**: no mutation;
- **dirty**, **diverged**, **detached**, **no upstream**, **wrong origin**, or failed relation check: no destructive recovery; report it as deferred/error as appropriate.

When a branch has no upstream, normal execution fetches that repository once, checks for `origin/<local-branch>`, configures the matching upstream when available, and reuses that fetch instead of immediately fetching again. During `--dry-run`, the service does not fetch or mutate refs merely to repair a missing upstream; it uses `git ls-remote` when it needs to verify that the matching remote branch exists.

For an unchanged remote fingerprint, the lightweight audit does not fetch every repository. It may safely push a clean ahead-only checkout against the already-known upstream ref; Git still rejects a non-fast-forward remote race.

## Status semantics

The final JSON distinguishes outcomes explicitly:

- `status=ok`: all expected phases completed and there are no unresolved issues;
- `status=partial`: useful work completed, but at least one repository/MegaVault action was deferred;
- `status=deferred`: nothing needed could be safely completed because expected conditions were deferred;
- `status=error`: a real operational failure; exit code is non-zero;
- `status=locked`: another autosync run owns the lock; exit code remains zero.

Useful counters include `audited_unchanged`, `skipped_unchanged`, `auto_pushed`, `updated`, `pushed`, `cloned`, and `deferred`.

Expected MegaVault conditions such as `deferred_dirty` and `deferred_not_synced` therefore never appear as a false `ok`, while remaining exit-code 0 so the timer does not treat a safe defer as a service crash.

## Activity audit log

Successful automatic mutations are written outside every Git worktree to avoid recursive self-commits:

```text
/home/daniele/.local/state/codex-github-autosync/activity.jsonl
```

Each JSONL row records the UTC timestamp, action, repository, branch, project ID when known, worktree, and a short detail. The ledger currently records `clone`, fast-forward `pull`, and `push`. Read-only audits, no-op checks, and fetches that do not change the checkout are intentionally not logged as mutations.

Telegram delivery progress for activity events is persisted separately in:

```text
/home/daniele/.local/state/codex-github-autosync/telegram-activity-state.json
```

Only `push` and `pull` activity rows generate a Telegram message. The cursor advances only after successful delivery, so a transient Telegram failure is retried on a later service run.

### Private Git mirror

The local ledger is also mirrored to the dedicated private repository `gernalix/github-autosync-data`. It is deliberately **not** part of `ALLOWED_REPOSITORIES`: its own infrastructure push must not generate another autosync event and recursively feed itself.

The service-owned checkout lives at:

```text
/home/daniele/.local/state/codex-github-autosync/github-autosync-data
```

Events are stored as daily JSONL files:

```text
activity/YYYY/MM/YYYY-MM-DD.jsonl
```

Each event has `schema_version=1` and a unique `event_id`. Legacy local rows without an ID receive a deterministic synthetic ID during export. One timer execution creates at most one data commit/push, even if it produced multiple events or touched multiple daily files.

Mirror progress is persisted in:

```text
/home/daniele/.local/state/codex-github-autosync/activity-data-state.json
```

The cursor advances only after the remote push is verified. A crash after commit, after push, or before cursor persistence is therefore recoverable without duplicate rows: an ahead-only data checkout is pushed on the next run and already-exported `event_id` values are de-duplicated before the cursor advances.

The local ledger remains the immediate recovery source; the private repository is the durable, remotely accessible history. Use `--no-data-mirror` only for tests or manual diagnostics.

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

The timer cadence stays at 5 minutes; efficiency comes from avoiding unnecessary network work while retaining a cheap local safety audit on all managed repositories.

## Verification

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
python3 github_autosync.py --dry-run --no-telegram run
systemctl --user status github-autosync.timer --no-pager
systemctl --user list-timers github-autosync.timer --all --no-pager
```

Regression coverage verifies that unchanged repositories are still locally audited, local ahead-only commits can reach the auto-push path, dirty unchanged worktrees are not silently skipped, a missing canonical worktree is cloned at the canonical path, dry-run upstream probing avoids fetch/mutation, upstream repair does not immediately duplicate its fetch, unmanaged repositories remain ignored, and the existing dirty/diverged safety rules remain intact.
