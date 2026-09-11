# github-autosync

Dedicated Fedora-side synchronizer for repositories owned by `gernalix`.

## Responsibilities

- discover all current and future GitHub repositories owned by `gernalix`;
- clone missing repositories under `/home/daniele/projects/<repo>`;
- safely update existing clones with `ghorg --protect-local --fetch-all --fetch-prune --no-clean`;
- audit every active canonical Git worktree listed in `/home/daniele/MegaVault/megavault.sqlite`;
- automatically push **only** repositories that are clean, ahead of their configured upstream, not behind/diverged, and whose push remains a normal non-force fast-forward;
- never auto-push a dirty, diverged, detached, mismatched-remote or no-upstream repository;
- never delete local repositories merely because a remote repository disappears;
- never reset, stash, force-pull or force-push application repositories;
- register genuinely new repositories in the authoritative MegaVault when the MegaVault worktree is clean and synchronized;
- notify through the shared `telegram_notify` package whenever an unresolved sync problem exists, and send one resolution notification when the problem set clears;
- deduplicate persistent Telegram alerts so a problem that remains unchanged is not repeated every 5 minutes;
- run from a dedicated `systemd --user` timer every 5 minutes.

## Git safety policy

Before reconciliation, each MegaVault-listed worktree is fetched and classified against its real upstream.

- **clean + synced**: no action;
- **clean + ahead only**: normal `git push` to its upstream branch, never `--force`; a remote race is rejected by Git;
- **clean + behind only**: normal remote-to-local synchronization is left to the protected ghorg pass;
- **dirty**, **dirty + ahead**, **diverged**, **no upstream**, **wrong origin**, **fetch/status/relation failure**, **missing worktree**: no destructive recovery; the condition is reported as an issue;
- after ghorg/registration, all MegaVault-listed worktrees are checked again, so unresolved behind/diverged/dirty states are surfaced.

In particular, local commits that cannot be auto-pushed safely (for example because the worktree is dirty or the branch diverged) remain untouched and are reported on Telegram with the MegaVault `project_id`.

## Telegram alerts

Notifications use the existing shared top-level Python package:

```text
python3 -m telegram_notify
```

The package resolves its configured default destination; this repository does not store or print Telegram secrets or chat IDs.

The state file is:

```text
/home/daniele/.local/state/codex-github-autosync/telegram-alert-state.json
```

A newly detected or changed problem set sends one `GitHub autosync: attenzione` message. An unchanged problem set stays quiet. When all previously reported problems disappear, one `GitHub autosync: risolto` message is sent. If Telegram delivery itself fails, the alert state is not advanced so the next run retries, and the service exits as failed.

`--dry-run` and explicit `--no-telegram` never send or advance Telegram alert state.

## Runtime

The canonical Fedora checkout is:

```text
/home/daniele/projects/github-autosync
```

The user units are:

```text
github-autosync.service
github-autosync.timer
```

The existing lock/state directory remains `/home/daniele/.local/state/codex-github-autosync` so migration from the former `codex-usage-monitor` ownership keeps the same concurrency guard.

## Install / migrate on Fedora

The migration must disable and remove the legacy `codex-github-autosync.service/.timer` before enabling the new units, so old and new implementations can never run concurrently.

After cloning this repository, install the unit files from `systemd/` into `~/.config/systemd/user/`, run `systemctl --user daemon-reload`, then enable/start `github-autosync.timer`.

The migration workflow is intentionally tracked in `gernalix/codex-roadmap`; do not run both generations of units at the same time.

## Manual verification

```bash
python3 github_autosync.py --dry-run run
python3 -m unittest discover -s tests
systemctl --user status github-autosync.timer --no-pager
systemctl --user list-timers github-autosync.timer --all --no-pager
```
