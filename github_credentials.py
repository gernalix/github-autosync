"""Scoped vault boundary for the automation's GitHub CLI subprocesses."""
from __future__ import annotations

import os
from pathlib import Path
import shlex

REFERENCE = 'FEDORA_GITHUB_ENV'


def runtime_command() -> list[str]:
    return [str(Path.home() / '.local/bin/bws-credential'), '--ref', REFERENCE,
            '--var', 'GH_TOKEN=GITHUB_TOKEN', '--']


def prepare_process(cmd: list[str], env: dict[str, str] | None = None
                    ) -> tuple[list[str], dict[str, str] | None]:
    if not cmd or Path(cmd[0]).name != 'gh':
        return cmd, env
    clean = dict(os.environ if env is None else env)
    for key in ('GH_TOKEN', 'GITHUB_TOKEN', 'BWS_ACCESS_TOKEN', 'GH_DEBUG'):
        clean.pop(key, None)
    return [*runtime_command(), *cmd], clean


def git_credential_helper() -> str:
    # Git supplies the operation argument and private stdin/stdout protocol.
    # This is process-scoped configuration, not a global credential change.
    command = shlex.join(['/usr/bin/env', '-u', 'GH_TOKEN', '-u', 'GITHUB_TOKEN',
                          '-u', 'BWS_ACCESS_TOKEN', '-u', 'GH_DEBUG',
                          *runtime_command(), 'gh', 'auth', 'git-credential'])
    # Git's quit=true protocol stops other helpers and interactive prompting.
    # https://git-scm.com/docs/gitcredentials#_custom_helpers
    return ('!f() { ' + command + ' "$@" || { '
            '[ "${1-}" != get ] || printf "quit=true\\n\\n"; return 1; }; }; f')
