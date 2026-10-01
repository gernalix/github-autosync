"""Terminal-aware Git maintenance in the existing integrator, not a lifecycle."""
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import re
import sqlite3
import subprocess
import time

import repo_single_writer as writer

C3_DB = Path.home() / '.local/state/c3-control/roadmap.sqlite'
C3_REPO = Path.home() / 'projects/codex-roadmap'
RETIREMENT_MARKER = Path.home() / '.local/state/c3-control/c2-retired.json'


def c3_allows(payload):
    prompt = str(payload.get('roadmap_prompt_id') or '')
    if not prompt and re.fullmatch(r'\d{6}', str(payload.get('task_id') or '')):
        prompt = str(payload['task_id'])
    if not C3_DB.exists():
        return not prompt
    try:
        with closing(sqlite3.connect(C3_DB.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            path = str(payload.get('worktree') or '')
            if path and db.execute("""SELECT 1 FROM work_item_execution_specs s
                JOIN work_items w ON w.work_item_id=s.work_item_id
                WHERE s.worktree=? AND w.status NOT IN ('completed','cancelled','superseded') LIMIT 1""", (path,)).fetchone():
                return False
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
        if time.monotonic() >= deadline:
            break
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
    try:
        result['legacy_c3'] = sweep_legacy_c3()
        result['orphan_c3'] = sweep_orphan_c3()
    except (OSError, sqlite3.Error, subprocess.TimeoutExpired) as exc:
        result['legacy_c3'] = {'reason': 'gc-operation-failed:' + type(exc).__name__}
    stamp.parent.mkdir(parents=True, exist_ok=True)
    stamp.touch()
    return result


def _legacy_path(path):
    """Only retired C2 namespaces; app-managed and arbitrary user trees excluded."""
    home = Path.home()
    try:
        relative = path.relative_to(home / '.local/share')
        return relative.parts[0].startswith('c2-')
    except ValueError:
        return path.parent == Path('/tmp') and (
            path.name.startswith('c2-') or path.name.startswith('codex-roadmap-'))


def _legacy_allowed(branch, path, tip):
    try:
        return _legacy_proof(branch, path, tip)
    except (ValueError, OSError, sqlite3.Error):
        return False


def _legacy_proof(branch, path, tip):
    if branch.startswith(('checkpoint/', 'archive/', 'recovery/')):
        return False
    with closing(sqlite3.connect(C3_DB.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        if not db.execute("SELECT 1 FROM meta WHERE key='pre_migration_execution_retired'").fetchone():
            return False
        for item in db.execute('SELECT work_item_id,prompt_id,status FROM work_items'):
            prompt_match = item[1] and re.search(r'(?<!\d)' + re.escape(item[1]) + r'(?!\d)', branch + '/' + path.name)
            short = re.search(r'wi-([0-9a-f]{4,32})(?:[^0-9a-f]|$)', branch)
            wi_match = short and item[0].startswith('wi:' + short[1])
            if (prompt_match or wi_match) and item[2] not in ('completed','cancelled','superseded'):
                return False
        if db.execute('SELECT 1 FROM work_item_checkpoints WHERE source_commit=? AND next_action IS NOT NULL LIMIT 1', (tip,)).fetchone():
            return False
    return c3_allows({'worktree': str(path), 'branch': branch})


def _terminal_branch(branch, tip):
    """Orphan refs need positive terminal ownership, not a legacy name."""
    if any(word in branch.lower() for word in ('checkpoint', 'archive', 'recovery')):
        return False
    try:
        with closing(sqlite3.connect(C3_DB.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            short = re.search(r'wi-([0-9a-f]{4,32})(?:[^0-9a-f]|$)', branch)
            matches = [row for row in db.execute('SELECT work_item_id,prompt_id,status FROM work_items')
                       if (row[1] and re.search(r'(?<!\d)' + re.escape(row[1]) + r'(?!\d)', branch))
                       or (short and row[0].startswith('wi:' + short[1]))]
            if not matches or any(row[2] not in ('completed','cancelled','superseded') for row in matches):
                return False
            for item, _, _ in matches:
                if db.execute('SELECT 1 FROM work_item_checkpoints WHERE work_item_id=? AND next_action IS NOT NULL LIMIT 1', (item,)).fetchone():
                    return False
        return _legacy_allowed(branch, Path('/nonexistent-gc-target'), tip)
    except (ValueError, OSError, sqlite3.Error):
        return False


def sweep_orphan_c3(*, dry_run=False, batch_limit=25):
    if not C3_DB.is_file() or not C3_REPO.is_dir():
        return {'checked': 0, 'reason': 'authority-unavailable'}
    results = []
    deadline = time.monotonic() + 60
    with writer.RepoLock(writer.ROADMAP_REPOSITORY):
        listing = writer._git(C3_REPO, 'worktree', 'list', '--porcelain')
        refs = writer._git(C3_REPO, 'for-each-ref', '--format=%(refname) %(objectname)', 'refs/heads')
        remote = writer._git(C3_REPO, 'ls-remote', '--heads', 'origin', timeout=20)
        if listing.returncode or refs.returncode or remote.returncode:
            return {'checked': 0, 'reason': 'inventory-unavailable'}
        checked_out = {line.removeprefix('branch refs/heads/') for line in listing.stdout.splitlines() if line.startswith('branch refs/heads/')}
        local = {line.split()[0].removeprefix('refs/heads/'): line.split()[1] for line in refs.stdout.splitlines()}
        remote_refs = {line.split()[1].removeprefix('refs/heads/'): line.split()[0] for line in remote.stdout.splitlines()}
        owned = set()
        for record in writer.STATE_ROOT.glob('*/tasks/*.json'):
            try:
                payload = json.loads(record.read_text())
            except (OSError, ValueError):
                return {'checked': 0, 'reason': 'unreadable-ownership'}
            if payload.get('repo') == writer.ROADMAP_REPOSITORY and payload.get('status') != 'merged':
                owned.add(payload.get('branch'))
        candidates = sorted((set(local) | set(remote_refs)) - checked_out - owned - {'main','master'})
        if candidates:
            offset = (int(time.time()) // 3600 * batch_limit) % len(candidates)
            candidates = candidates[offset:] + candidates[:offset]
        for branch in candidates[:batch_limit]:
            if time.monotonic() >= deadline:
                break
            tip = local.get(branch) or remote_refs[branch]
            result = {'branch': branch, 'removed': []}
            results.append(result)
            if remote_refs.get(branch, tip) != tip or not _terminal_branch(branch, tip):
                result['reason'] = 'unknown-nonterminal-or-changed'
                continue
            if writer._git(C3_REPO, 'merge-base', '--is-ancestor', tip, 'refs/remotes/origin/main').returncode:
                result['reason'] = 'unintegrated-tip'
                continue
            current = writer._git(C3_REPO, 'worktree', 'list', '--porcelain')
            if current.returncode or 'branch refs/heads/' + branch + '\n' in current.stdout or not _terminal_branch(branch, tip):
                result['reason'] = 'referenced-at-delete'
                continue
            if branch in remote_refs:
                if not dry_run and writer._git(C3_REPO, 'push', 'origin', '--force-with-lease=refs/heads/' + branch + ':' + tip,
                                               ':refs/heads/' + branch, timeout=30).returncode:
                    result['reason'] = 'remote-delete-refused'
                    continue
                result['removed'].append('remote-branch')
            current = writer._git(C3_REPO, 'worktree', 'list', '--porcelain')
            if current.returncode or 'branch refs/heads/' + branch + '\n' in current.stdout or not _terminal_branch(branch, tip):
                result['reason'] = 'referenced-at-local-delete'
                continue
            if branch in local and (dry_run or writer._git(C3_REPO, 'update-ref', '-d', 'refs/heads/' + branch, tip).returncode == 0):
                result['removed'].append('local-branch')
    return {'checked': len(results), 'results': results}


def sweep_legacy_c3(*, dry_run=False, batch_limit=25):
    if not C3_DB.is_file() or not RETIREMENT_MARKER.is_file() or not C3_REPO.is_dir():
        return {'checked': 0, 'reason': 'retirement-evidence-unavailable'}
    results = []
    deadline = time.monotonic() + 60
    try:
        with closing(sqlite3.connect(C3_DB.resolve().as_uri() + '?mode=ro', uri=True)) as db:
            retirement = db.execute("SELECT value FROM meta WHERE key='pre_migration_execution_retired'").fetchone()
        if not retirement:
            return {'checked': 0, 'reason': 'retirement-evidence-unavailable'}
        cutoff = datetime.fromisoformat(retirement[0].replace('Z', '+00:00')).timestamp()
    except (ValueError, OSError, sqlite3.Error):
        return {'checked': 0, 'reason': 'retirement-evidence-invalid'}
    with writer.RepoLock(writer.ROADMAP_REPOSITORY):
        listing = writer._git(C3_REPO, 'worktree', 'list', '--porcelain')
        if listing.returncode:
            return {'checked': 0, 'reason': 'worktree-inventory-unavailable'}
        candidates = []
        for block in listing.stdout.strip().split('\n\n'):
            fields = dict(line.split(' ', 1) for line in block.splitlines() if ' ' in line)
            path = Path(fields.get('worktree', '')).resolve()
            if path != C3_REPO.resolve() and _legacy_path(path):
                candidates.append((path, fields.get('branch', '').removeprefix('refs/heads/'), fields.get('HEAD', '')))
        if candidates:
            offset = (int(time.time()) // 3600 * batch_limit) % len(candidates)
            candidates = candidates[offset:] + candidates[:offset]
        for path, branch, tip in candidates[:batch_limit]:
            if time.monotonic() >= deadline:
                break
            result = {'worktree': str(path), 'removed': []}
            results.append(result)
            if not path.is_dir():
                result['reason'] = 'missing-worktree-preserved'
                continue
            if not branch or not _legacy_allowed(branch, path, tip):
                result['reason'] = 'nonterminal-active-or-recovery'
                continue
            if writer._git(C3_REPO, 'merge-base', '--is-ancestor', tip, 'refs/remotes/origin/main').returncode:
                result['reason'] = 'unintegrated-tip'
                continue
            dirty = writer._git(path, 'status', '--porcelain', '--untracked-files=all')
            metadata = writer._git(path, 'rev-parse', '--absolute-git-dir')
            if dirty.returncode or dirty.stdout.strip() or writer._operation_in_progress(path):
                result['reason'] = 'dirty-or-conflicted'
                continue
            # status refreshes the metadata directory mtime. The gitdir link
            # identifies worktree creation without changing on ordinary reads.
            allocation = Path(metadata.stdout.strip()) / 'gitdir'
            if metadata.returncode or not allocation.is_file() or allocation.stat().st_mtime >= cutoff:
                result['reason'] = 'post-retirement-or-unknown'
                continue
            if not _legacy_allowed(branch, path, tip):
                result['reason'] = 'active-at-delete'
                continue
            if not dry_run and writer._git(C3_REPO, 'worktree', 'remove', str(path), timeout=30).returncode:
                result['reason'] = 'worktree-preserved'
                continue
            result['removed'].append('worktree')
            # Remote cleanup is limited to this proven integrated exact tip.
            remote = writer._git(C3_REPO, 'ls-remote', '--heads', 'origin', 'refs/heads/' + branch, timeout=20)
            if remote.returncode:
                result['reason'] = 'remote-read-failed'
                continue
            fields = remote.stdout.split()
            if fields:
                if len(fields) != 2 or fields[0] != tip or not _legacy_allowed(branch, path, tip):
                    result['reason'] = 'remote-changed'
                    continue
                if dry_run or writer._git(C3_REPO, 'push', 'origin', '--force-with-lease=refs/heads/' + branch + ':' + tip,
                                         ':refs/heads/' + branch, timeout=30).returncode == 0:
                    result['removed'].append('remote-branch')
                else:
                    result['reason'] = 'remote-delete-refused'
                    continue
            checked = writer._git(C3_REPO, 'worktree', 'list', '--porcelain')
            if not dry_run and (checked.returncode or 'branch refs/heads/' + branch + '\n' in checked.stdout):
                result['reason'] = 'checked-out-at-delete'
                continue
            if not _legacy_allowed(branch, path, tip):
                result['reason'] = 'active-at-local-delete'
                continue
            if dry_run or writer._git(C3_REPO, 'update-ref', '-d', 'refs/heads/' + branch, tip).returncode == 0:
                result['removed'].append('local-branch')
    return {'checked': len(results), 'results': results}
