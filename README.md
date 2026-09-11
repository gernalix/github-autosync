# github-autosync

Dedicated Fedora-side synchronizer for repositories owned by `gernalix`.

## Responsibilities

- discover all current and future GitHub repositories owned by `gernalix`;
- clone missing repositories under `/home/daniele/projects/<repo>`;
- safely update existing clones with `ghorg --protect-local --fetch-all --fetch-prune --no-clean`;
- never delete local repositories merely because a remote repository disappears;
- never reset, stash, force-pull or force-push application repositories;
- register genuinely new repositories in the authoritative MegaVault when the MegaVault worktree is clean and synchronized;
- run from a dedicated `systemd --user` timer every 5 minutes.

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
systemctl --user status github-autosync.timer --no-pager
systemctl --user list-timers github-autosync.timer --all --no-pager
```
