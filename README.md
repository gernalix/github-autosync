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
- `gernalix/workflowy-importer`
- `gernalix/chrome-codex-switcher`
- `gernalix/PersonalHub`
- `gernalix/codex-usage-monitor`
- `gernalix/fedora-t7-backup`
- `gernalix/amici_fb`
- `gernalix/salute`

All other current or future GitHub repositories are ignored completely until explicitly added to `ALLOWED_REPOSITORIES`. They are not cloned, fetched, pulled, pushed, audited, registered in MegaVault, or persisted in repo state.

## One-command global reconcile

Install the global command once:

```bash
cd ~/projects/github-autosync
python3 install_global_command.py
```

Then reconcile everything relevant with one command:

```bash
github-reconcile
```

This forces a fresh network reconciliation instead of relying on the normal fingerprint fast path. It covers every non-archived GitHub repository already represented by an active MegaVault worktree, already present under `~/projects/<repo>`, or included in the normal autosync allowlist.

Canonical branches remain protected, but work is split into two phases. Agents run concurrently in dedicated `task/*` branches/worktrees and finish as soon as their queued pull request exists. The separate `repo-integrator` service serializes only final canonical integration per repository; `github-reconcile` is limited to synchronization/audit. Stale/deleted tracking branches are still repaired mechanically when unambiguous.

A failure in one repository does not abort the global pass: the remaining repositories are still reconciled, and the final JSON exposes any unresolved entries in `reconcile_issues`.

`codex-roadmap` remains special: it is delegated to the canonical guarded `roadmap_pull.py` path and canonical roadmap state is never auto-committed, preserving the single-writer boundary.


## Parallel ChatGPT/Codex work

For every repository except `codex-roadmap`, use one isolated worktree per task:

```bash
repo-task start --repo ~/projects/PersonalHub --task-id 123456 --actor codex
```

The command prints the worktree path. Work only there. Worker coordination is branch/worktree isolation, not a repository lease: a dirty canonical checkout cannot block a fresh task worktree created from the fetched remote canonical tip. `repo-task heartbeat` is retained only as a compatibility activity marker. Roadmap-launched Codex tasks use `repo-task start-roadmap` automatically and receive the worktree path directly from `roadmap_start.py`.

When the task is complete:

```bash
repo-task finish --repo ~/projects/PersonalHub --task-id 123456
```

This checkpoints the completed task, pushes its `task/123456` branch and creates a queued PR. The worker is then finished: it does not wait for CI or merge. The minute-by-minute `repo-integrator` service processes PRs FIFO per repository, waits asynchronously for checks, automatically rebases a clean queued task branch when canonical has advanced, and merges only after refreshed checks pass. Real rebase conflicts are surfaced as semantic conflicts rather than guessed. After integration, clean task worktrees and unchanged task branches are cleaned up safely; dirty or changed post-merge work is preserved rather than force-deleted.

Multiple ChatGPT/Codex sessions can therefore work on the same repository concurrently without sharing a checkout. The canonical checkout is never a worker workspace.

The canonical branch is guarded locally: direct commits/merges to it are rejected. A local fast-forward to the exact fetched remote canonical tip is allowed because it is synchronization, not a new canonical write. `codex-roadmap` keeps its existing dedicated writer/guard instead of this generic path.

Repositories whose canonical branch is intentionally owned by a dedicated local service are excluded from the generic writer. Currently `gernalix/activity-watch-data` is owned by `activity-watch-uploader`: `github-reconcile` removes only its own generic reference hook (restoring any pre-existing hook) and leaves that checkout untouched. This prevents the autosync writer from racing or blocking the service that is authoritative for the data repository.

Optional forms:

```bash
github-reconcile --dry-run
```

Two periodic timers have separate responsibilities: `github-autosync.timer` synchronizes/audits repositories, while `repo-integrator.timer` owns the queued PR integration path.

### Roadmap cockpit contract

`github-autosync` is also the authoritative local source for **repository integration state** shown by the Workflowy roadmap cockpit. It does not decide the canonical roadmap status; it reports the real Git pipeline for each roadmap `PROMPT_ID`.

Use one bulk call:

```bash
repo-task status-all --roadmap-only
```

Each task exposes `pipeline_state` plus the concrete integration observation, PR URL/number and FIFO queue position. Typical values are:

- `running`: worker worktree active;
- `integration`: PR queued, checks pending, refreshed after rebase, or integrating;
- `needs-fix`: a real integration blocker such as semantic conflict or failed checks;
- `done`: merge completed.

The integrator persists observations such as `queued`, `checks-pending`, `rebasing`, `integrating`, `semantic-conflict` and `merged` in the task record. Workflowy consumes this contract instead of inferring state from Markdown, branch names or PR titles.

## Responsibilities

- execute one GitHub metadata discovery query per timer run, then immediately filter it to the managed allowlist;
- persist a compact fingerprint only for managed repositories in `/home/daniele/.local/state/codex-github-autosync/repo-state.json`;
- clone a missing managed repository into its MegaVault canonical worktree when one is registered, otherwise under `/home/daniele/projects/<repo>`;
- fetch/update a managed repository when its GitHub metadata fingerprint changes;
- perform a lightweight **local-only audit** even when the remote fingerprint is unchanged, so dirty worktrees and local commits are not hidden by the remote fast path;
- protect canonical branches with a local reference guard while workers remain branch-isolated and integration is asynchronous;
- recover a rejected clean push with one evidence-producing fetch, then retry or rebase-and-push only when the new relation makes that safe;
- always reconcile `codex-roadmap` through its canonical `tools/roadmap_pull.py` path, even when the GitHub fingerprint is unchanged, so generated-view dirt, interrupted guarded fast-forwards, and stale/missing pull guards heal automatically;
- append every successful automatic repository mutation (`clone`, fast-forward `pull`, `push`) to a durable JSONL activity ledger;
- skip network reconciliation for unchanged, clean, synchronized repositories except `codex-roadmap`, whose guarded reconciler is intentionally checked every run;
- never stash, hard-reset, force-pull or force-push user work; unresolved semantic conflicts remain deferred for review;
- register genuinely new managed repositories in MegaVault when its worktree is clean and synchronized;
- expose truthful machine-readable states: `ok`, `partial`, `deferred`, `error`, or `locked`;
- deploy explicitly allowlisted local runtimes once per checked-out revision and retry failed deploys on later minute ticks;
- report health only through the existing Uptime Kuma Push monitor; this service sends no Telegram notifications;
- run from a `systemd --user` calendar timer every minute.

The periodic path no longer uses `ghorg --fetch-all`. `ghorg` can still be used manually for bootstrap/recovery, but it is not part of the steady-state timer.

### Runtime deployment after sync

Repositories that have a local runtime may opt into a narrow, explicit post-sync deploy command. Deployment is keyed by the checked-out commit SHA and persisted in `runtime-deploy-state.json`, so a given revision is deployed once, while a failed deployment is retried on the next minute tick without repeating Git work.

Current deploy contracts:

- `gernalix/workflowy-importer` → `python3 deploy_runtime.py`; this refreshes the user-systemd units, restarts the Workflowy bridge, and immediately runs one roadmap sync.
- `gernalix/chrome-codex-switcher` → `bash install.sh`; this refreshes the installed host/extension files and restarts the local switcher service.

A deploy failure is surfaced as `runtime_deploy_failed` and does not advance the deploy-state SHA. The ordinary repository fingerprint may still advance because Git synchronization itself succeeded; the next timer run therefore retries only the deployment step.

## Change detection and local audit

Each run executes one command equivalent to:

```bash
gh repo list gernalix --limit 1000 --json name,url,defaultBranchRef,pushedAt,isArchived
```

The result is filtered immediately to the allowlist. The fingerprint uses remote URL, default branch, `pushedAt`, and archived state.

If a managed repository's fingerprint changed, that repository enters the network Git reconciliation path. If the fingerprint is unchanged, `github-autosync` still checks the selected local worktree without fetching: origin, branch/upstream, dirty state, and the already-known ahead/behind relation are inspected. This catches local-only changes such as commits created by Codex. A clean ahead-only checkout can therefore be pushed even though GitHub's `pushedAt` has not changed yet.

An unchanged, clean, synchronized checkout is counted as `audited_unchanged` and then `skipped_unchanged`: it avoids `git fetch`/pull while remaining visible to the local safety audit.

`codex-roadmap` is the deliberate exception. Its local checkout is reconciled every timer run through `tools/roadmap_pull.py --bootstrap-guard`, never through a generic `git pull`. That helper owns the roadmap-specific invariants: it restores generated-view dirt when safe, recovers the interrupted fast-forward shape left by a rejected unguarded pull, preserves running prompts, verifies the canonical SQLite/rendered views, and refreshes the installed guard. Local commits that touch canonical roadmap state are not auto-pushed; they are surfaced as a real blocker.

## Git safety policy

For a managed repository selected as remotely changed:

- **missing**: clone it directly into the canonical MegaVault worktree when known, otherwise `/home/daniele/projects/<repo>`;
- **clean + behind only**: fetch that repository and fast-forward with `git merge --ff-only @{u}`;
- **clean + ahead only**: normal `git push`, never `--force`; if that push loses a remote race, fetch once, inspect the new relation, and perform at most one evidence-based recovery attempt;
- **clean + push-race divergence**: rebase onto the freshly fetched upstream and retry once; on any rebase conflict, abort the rebase and defer instead of guessing;
- **clean + synced**: no mutation;
- **`codex-roadmap`**: route through its guarded reconciler; generated-state dirt and interrupted guarded fast-forwards are mechanical recovery cases, while local canonical commits or semantic conflicts remain blocked;
- **dirty non-roadmap**, **pre-existing divergence**, **detached**, **no upstream**, **wrong origin**, or failed relation check: no destructive recovery; report it as deferred/error as appropriate.

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

Install or update the global command and user timer with `python3 install_systemd.py`.
The one-minute calendar timer has `Persistent=true`; systemd coalesces ticks while
the oneshot service is already running. A process lock also excludes manual runs.
The timer deliberately executes the lightweight fingerprint/audit `run` path, not
the forced `reconcile-all` path, so routine heartbeats do not require a full network
reconciliation of every repository. The service reads its private Kuma Push URL from
`~/.config/github-autosync/reconcile.env`, installed by `python3 configure_kuma.py`. The user service no longer imports that file with `EnvironmentFile=`: it passes it as the systemd credential `reconcile.env`, and the runtime reads only `GITHUB_RECONCILE_PUSH_URL` from `$CREDENTIALS_DIRECTORY`. The plaintext file is retained only as a migration source until an encrypted systemd credential is provisioned locally.
Every non-dry periodic run sends a Kuma heartbeat; fatal autosync errors send an
explicit DOWN heartbeat instead of silently aging into "No heartbeat in the time window".
A missing/unusable Push URL is treated as a heartbeat failure rather than success.
The Fedora bootstrap also compares the installed user units with the repository copies;
after a canonical checkout advances, the next reconcile repairs stale unit files and
reloads systemd automatically. An independent `github-autosync-watchdog.timer` runs every
five minutes outside the primary reconcile service: it can safely fast-forward a clean
`github-autosync/main` checkout to the fetched `origin/main`, reinstall stale user units,
re-enable the primary timers, and kick one non-blocking reconcile. It refuses dirty,
wrong-branch, or non-fast-forward checkouts instead of resetting user work. This separates
recovery from the component being recovered, so a stopped primary timer cannot indefinitely
prevent its own fix from being deployed.

`github-reconcile` remains the manual forced `reconcile-all` command. It prints a
short Italian summary. `--json` emits the full machine-readable result; an unresolved
repository returns exit code 2 after the other repositories have been processed.

The canonical checkout is never a worker workspace. Agent changes belong in `repo-task` worktrees. Completed task PRs are serialized asynchronously by `repo-integrator`; pending checks remain queued, canonical advances are rebased automatically on clean task branches, and only real semantic conflicts require repair. The roadmap keeps its own guarded mutation writer.

## Verification

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
python3 github_autosync.py --dry-run run
systemctl --user status github-autosync.timer --no-pager
systemctl --user list-timers github-autosync.timer --all --no-pager
```

Regression coverage verifies that unchanged repositories are still locally audited, local ahead-only commits can reach the auto-push path, dirty unchanged worktrees are not silently skipped, a missing canonical worktree is cloned at the canonical path, dry-run upstream probing avoids fetch/mutation, upstream repair does not immediately duplicate its fetch, unmanaged repositories remain ignored, and the existing dirty/diverged safety rules remain intact.
