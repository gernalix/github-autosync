#!/usr/bin/env python3
from __future__ import annotations

import os
from pathlib import Path

COMMAND_NAME = "github-reconcile"
TASK_COMMAND_NAME = "repo-task"
DEFAULT_REPO = Path.home() / "projects" / "github-autosync"


def main() -> int:
    repo = DEFAULT_REPO.resolve()
    entrypoint = repo / "github_autosync.py"
    if not entrypoint.is_file():
        raise SystemExit(f"missing entrypoint: {entrypoint}")

    bindir = Path.home() / ".local" / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    target = bindir / COMMAND_NAME
    tmp = target.with_suffix(".tmp")
    tmp.write_text(
        "#!/bin/sh\n"
        f'exec /usr/bin/python3 "{entrypoint}" "$@" reconcile-all\n',
        encoding="utf-8",
    )
    os.chmod(tmp, 0o755)
    tmp.replace(target)

    task_entrypoint = repo / "repo_single_writer.py"
    task_target = bindir / TASK_COMMAND_NAME
    task_tmp = task_target.with_suffix(".tmp")
    task_tmp.write_text(
        "#!/bin/sh\n"
        f'exec /usr/bin/python3 "{task_entrypoint}" "$@"\n',
        encoding="utf-8",
    )
    os.chmod(task_tmp, 0o755)
    task_tmp.replace(task_target)

    print(target)
    print(task_target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
