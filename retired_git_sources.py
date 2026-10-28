"""Explicit Git-only preservation policy for the retired Codex Roadmap sources.

Never opens the C3 database, changes lifecycle records or starts/stops a unit.
Automatic legacy collection remains disabled. Call only under the owner lock.
"""
from datetime import datetime
import os
from pathlib import Path

import repo_single_writer as writer

HOME = Path.home()
PROC = Path('/proc')
CUTOFF = int(datetime.fromisoformat('2026-10-06T00:00:00+00:00').timestamp())


def historical_owner(payload):
    return bool(payload.get('roadmap_prompt_id')) or payload.get('task_id') == 'c3-retirement-20261005'


def historical_path(path):
    roots = [HOME/'.local/share/c2-task-state', HOME/'.local/share/chatgpt-task-state',
             HOME/'.local/share/chatgpt-c2-state-alerts', HOME/'Documents/ChatGPT/C2',
             HOME/'.local/share/codex-github-autosync/worktrees/gernalix_codex-roadmap',
             HOME/'projects/codex-roadmap-android-favorites-probe',
             HOME/'projects/codex-roadmap-uptime-red',
             HOME/'.local/share/c2-bootstrap', HOME/'.local/share/c2-chatgpt-worktrees',
             HOME/'.local/share/c2-maintenance', HOME/'.local/share/c2-supervisor',
             HOME/'.local/share/chatgpt-rdc-supervisor/worktrees/codex-roadmap',
             HOME/'.local/share/chatgpt-watchdog/codex-roadmap',
             HOME/'projects/codex-roadmap-worktrees']
    if any(path == root or root in path.parents for root in roots):return True
    app = HOME/'.codex/worktrees'
    return app in path.parents and path.name == 'codex-roadmap'


def inactive_source(path):
    """No source-referencing process, including argv options and working dirs."""
    for proc in PROC.iterdir():
        if not proc.name.isdigit():continue
        try:
            argv=(proc/'cmdline').read_bytes().decode(errors='replace').split('\0')
            try:cwd=Path(os.readlink(proc/'cwd'))
            except OSError:cwd=None
        except OSError:
            try:owned=proc.stat().st_uid==os.getuid()
            except FileNotFoundError:continue
            if owned:raise ValueError('source process coverage unavailable')
            continue
        values=[arg.partition('=')[2] if arg.startswith('--') and '=' in arg else arg for arg in argv]
        if (cwd and (cwd==path or path in cwd.parents)) or any(arg==str(path) or arg.startswith(str(path)+'/') for arg in values):
            raise ValueError('retired source is still in use by a process')


def verify_retired_source(repo, review, scope_guard, *, worktree=None, branch=None):
    """Revalidate one exact reviewed source; this grants no lifecycle authority."""
    from repo_gc import _verify_retired_git_review
    repo=Path(repo).resolve()
    if (scope_guard is None or getattr(scope_guard,'project_id',None)!=51
            or writer._repo_slug(repo).lower()!='gernalix/codex-roadmap'
            or review.get('disposition')!='superseded'):
        raise ValueError('retired source review is restricted to Codex Roadmap')
    scope_guard(repo,'gernalix/codex-roadmap')
    _verify_retired_git_review(review)
    live=writer.run(['systemctl','--user','list-units','c3-*',
                    '--state=active,activating,reloading,deactivating','--no-legend','--plain','--no-pager'],timeout=20)
    if live.returncode or live.stdout.strip():raise ValueError('C3 units are not confirmed inactive')
    canonical=review['canonical_sha']
    instructions=writer._ok(repo,'show',canonical+':AGENTS.md')
    if 'C2/C3' not in instructions or 'ritirato' not in instructions:
        raise ValueError('canonical retirement declaration unavailable')
    tip=review.get('expected_head') or review.get('expected_sha')
    stamp=int(writer._ok(repo,'log','-1','--format=%ct',tip+'^{commit}'))
    if stamp>CUTOFF and writer._git(repo,'merge-base','--is-ancestor',tip+'^{commit}',canonical).returncode:
        raise ValueError('unintegrated source is newer than retirement cutoff')
    if worktree is not None:
        path=Path(worktree).resolve();matched=[]
        for file in writer._task_dir_for_slug(writer._repo_slug(repo)).glob('*.json'):
            import json
            payload=json.loads(file.read_text())
            if Path(payload.get('worktree') or '/').resolve()==path:matched.append(payload)
        if any(not historical_owner(payload) for payload in matched):
            raise ValueError('current task owner forbids retired-source archival')
        if not historical_path(path) and not matched:raise ValueError('source is outside historical checkout namespaces')
        inactive_source(path)
    if branch is not None:
        found=writer._task_by_branch(writer._repo_slug(repo),branch)
        if found and not historical_owner(found[1]):raise ValueError('current task owner forbids retired-ref collection')


def independent_git_snapshot(path):
    """Capture a self-contained clone without exposing local configuration values."""
    import hashlib
    path=Path(path).resolve();git=path/'.git'
    if git.is_symlink() or not git.is_dir() or writer._git_common_dir(path).resolve()!=git:
        raise ValueError('independent source must have a self-contained Git directory')
    for directory,dirs,files in os.walk(git/'objects',followlinks=False):
        if Path(directory).is_symlink() or any((Path(directory)/name).is_symlink() for name in dirs+files):
            raise ValueError('external Git object storage is not self-contained')
    alternate=git/'objects/info/alternates'
    if alternate.exists() and alternate.read_text().strip():raise ValueError('Git alternates are not self-contained')
    if writer._git(path,'config','--get','extensions.partialClone').stdout.strip():
        raise ValueError('partial clone is not self-contained')
    refs={line.split(' ',1)[1]:line.split(' ',1)[0] for line in writer._ok(path,'for-each-ref','--format=%(objectname) %(refname)').splitlines()}
    return {'head':writer._ok(path,'rev-parse','HEAD'),
            'symbolic_head':writer._git(path,'symbolic-ref','-q','HEAD').stdout.strip(),
            'refs':refs,'config_sha256':hashlib.sha256((git/'config').read_bytes()).hexdigest()}


def verify_independent_git(repo,path,review):
    import hashlib,json
    if writer._repo_slug(path).lower()!=writer._repo_slug(repo).lower():
        raise ValueError('independent source origin differs')
    state=independent_git_snapshot(path)
    digest=hashlib.sha256(json.dumps(state,sort_keys=True).encode()).hexdigest()
    if digest!=review.get('independent_git_fingerprint'):
        raise ValueError('independent Git state changed or was not reviewed')
    for tip in set(state['refs'].values())|{state['head']}:
        # Tags pointing to non-commit objects are preserved by --all too.
        commit=writer._git(path,'rev-parse','--verify',tip+'^{commit}')
        if commit.returncode:continue
        stamp=int(writer._ok(path,'log','-1','--format=%ct',tip+'^{commit}'))
        if stamp>CUTOFF and writer._git(repo,'merge-base','--is-ancestor',tip+'^{commit}',review['canonical_sha']).returncode:
            raise ValueError('independent source contains recent unintegrated history')
    return state
