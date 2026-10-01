"""Terminal-aware Git maintenance in the existing integrator, not a lifecycle."""
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import time

import repo_single_writer as writer

C3_DB = Path.home() / '.local/state/c3-control/roadmap.sqlite'


def c3_allows(payload):
    prompt = str(payload.get('roadmap_prompt_id') or '')
    if not prompt and re.fullmatch(r'\d{6}', str(payload.get('task_id') or '')):
        prompt = str(payload['task_id'])
    if not C3_DB.exists():
        return not prompt
    try:
        with closing(sqlite3.connect(C3_DB.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            if prompt:
                item = db.execute('SELECT work_item_id,status FROM work_items WHERE prompt_id=?', (prompt,)).fetchone()
                if not item or item[1] not in ('completed', 'cancelled', 'superseded'):
                    return False
                if db.execute("SELECT 1 FROM work_item_checkpoints WHERE work_item_id=? AND next_action IS NOT NULL LIMIT 1", (item[0],)).fetchone():
                    return False
            for run in db.execute("SELECT work_item_id,worker_ref,metadata_json FROM work_item_runs WHERE state IN ('claimed','running','recovering')"):
                if prompt and run[0] == item[0]:
                    return False
                evidence = str(run[1]) + str(run[2])
                if any(str(payload.get(field) or '\0') in evidence for field in ('worktree', 'branch')):
                    return False
        return True
    except (OSError, sqlite3.Error):
        return False


def collect_record(payload, *, dry_run=False):
    if payload.get('status') != 'merged' or not c3_allows(payload):
        return {'removed': [], 'reason': 'nonterminal-active-or-recovery'}
    repo = Path(str(payload.get('repo_path') or '')).expanduser().resolve()
    branch = str(payload.get('branch') or '')
    path = Path(str(payload.get('worktree') or '')).expanduser().resolve()
    if not branch.startswith('task/') or path == repo or not payload.get('worktree'):
        return {'removed': [], 'reason': 'invalid-target'}
    if not path.is_relative_to(writer.WORKTREE_ROOT.resolve()):
        return {'removed': [], 'reason': 'unmanaged-worktree'}
    if not repo.is_dir() or writer._git(repo, 'rev-parse', '--is-inside-work-tree').returncode:
        return {'removed': [], 'reason': 'repository-unavailable'}
    canonical = str(payload.get('canonical_branch') or 'main')
    base = 'refs/remotes/origin/' + canonical
    head = writer._git(repo, 'rev-parse', '--verify', 'refs/heads/' + branch)
    expected = payload.get('integrated_head') or payload.get('head_sha')
    has_local = head.returncode == 0
    if head.returncode:
        if not expected or not re.fullmatch(r'[0-9a-f]{40}', expected):
            return {'removed': [], 'reason': 'branch-missing'}
        tip = expected
    else:
        tip = head.stdout.strip()
    if expected and tip != expected:
        return {'removed': [], 'reason': 'changed-after-merge'}
    if writer._git(repo, 'merge-base', '--is-ancestor', tip, base).returncode:
        return {'removed': [], 'reason': 'unintegrated-tip'}
    if path.exists():
        dirty = writer._git(path, 'status', '--porcelain', '--untracked-files=all')
        actual = writer._git(path, 'rev-parse', 'HEAD')
        if (dirty.returncode or dirty.stdout.strip() or actual.returncode
                or actual.stdout.strip() != tip or writer._operation_in_progress(path)):
            return {'removed': [], 'reason': 'dirty-changed-or-conflicted'}
    # Another record or checked-out worktree can still reference this branch.
    for record in writer.STATE_ROOT.glob('*/tasks/*.json'):
        try:
            other = json.loads(record.read_text())
        except (OSError, ValueError):
            return {'removed': [], 'reason': 'unreadable-ownership'}
        if (other.get('repo') == payload.get('repo') and other.get('branch') == branch
                and other.get('status') != 'merged'):
            return {'removed': [], 'reason': 'referenced-branch'}
    removed = []
    if path.exists():
        if not c3_allows(payload):
            return {'removed': [], 'reason': 'active-at-delete'}
        if dry_run:
            removed.append('worktree')
        elif writer._git(repo, 'worktree', 'remove', str(path), timeout=30).returncode == 0:
            removed.append('worktree')
        else:
            return {'removed': [], 'reason': 'worktree-preserved'}
    if not dry_run:
        checked = writer._git(repo, 'worktree', 'list', '--porcelain')
        if checked.returncode or 'branch refs/heads/' + branch + '\n' in checked.stdout:
            return {'removed': removed, 'reason': 'checked-out-branch'}
    remote = writer._git(repo, 'ls-remote', '--heads', 'origin', 'refs/heads/' + branch, timeout=20)
    if remote.returncode:
        return {'removed': removed, 'reason': 'remote-read-failed'}
    fields = remote.stdout.split()
    if fields:
        if len(fields) != 2 or fields[0] != tip or not c3_allows(payload):
            return {'removed': removed, 'reason': 'remote-changed-or-active'}
        if dry_run or writer._git(repo, 'push', 'origin',
                '--force-with-lease=refs/heads/' + branch + ':' + tip,
                ':refs/heads/' + branch, timeout=30).returncode == 0:
            removed.append('remote-branch')
        else:
            return {'removed': removed, 'reason': 'remote-delete-refused'}
    if not c3_allows(payload):
        return {'removed': removed, 'reason': 'active-at-local-delete'}
    if has_local:
        if not dry_run:
            checked = writer._git(repo, 'worktree', 'list', '--porcelain')
            if checked.returncode or 'branch refs/heads/' + branch + '\n' in checked.stdout:
                return {'removed': removed, 'reason': 'checked-out-at-delete'}
        # Ancestry was proved against fetched canonical, not a stale local HEAD.
        # Compare-and-delete cannot remove a concurrently changed branch tip.
        if dry_run or writer._git(repo, 'update-ref', '-d', 'refs/heads/' + branch, tip).returncode == 0:
            removed.append('local-branch')
        else:
            return {'removed': removed, 'reason': 'local-delete-refused'}
    return {'removed': removed}


def sweep(*, dry_run=False, batch_limit=25):
    results = []
    repos = set()
    deadline = time.monotonic() + 60
    paths = sorted(writer.STATE_ROOT.glob('*/tasks/*.json'))
    # Rotate bounded scans: a preserved dirty task cannot starve later records.
    if paths:
        offset = (int(time.time()) // 3600 * batch_limit) % len(paths)
        paths = paths[offset:] + paths[:offset]
    for path in paths:
        if time.monotonic() >= deadline:
            break
        try:
            payload = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if payload.get('status') != 'merged' or 'local-branch' in payload.get('cleanup', []):
            continue
        if len(results) >= batch_limit:
            break
        with writer.RepoLock(str(payload.get('repo') or '')):
            try:
                result = collect_record(payload, dry_run=dry_run)
            except (OSError, subprocess.TimeoutExpired) as exc:
                result = {'removed': [], 'reason': 'gc-operation-failed:' + type(exc).__name__}
            if result['removed'] and not dry_run:
                payload['cleanup'] = sorted(set(payload.get('cleanup', [])) | set(result['removed']))
                writer._atomic_json(path, payload)
                repos.add(Path(payload['repo_path']))
        results.append({'task_id': payload.get('task_id'), **result})
    for repo in repos:
        # Git's default grace periods preserve recent/reflog recovery objects.
        try:
            writer._git(repo, '-c', 'gc.autoDetach=false', 'gc', '--auto', timeout=30)
        except subprocess.TimeoutExpired:
            pass  # Ordinary grace-period maintenance can resume on a later run.
    return {'checked': len(results), 'results': results}


def periodic():
    stamp = writer.STATE_ROOT / 'gc-last-run'
    if stamp.exists() and time.time() - stamp.stat().st_mtime < 3600:
        return {'status': 'not-due'}
    result = sweep()
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.touch()
    return result
