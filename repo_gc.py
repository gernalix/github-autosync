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
PURIFY_GC_CAPABILITY = 'reviewed-archive-v1'
RETIREMENT_MARKER = Path.home() / '.local/state/c3-control/c2-retired.json'


def c3_allows(payload):
    # Frozen historical workers/worktrees are retained. Ordinary Git GC needs no
    # roadmap database, lifecycle lease or C3 service.
    if payload.get('roadmap_prompt_id'):
        return False
    if re.fullmatch(r'\d{6}', str(payload.get('task_id') or '')):
        merged_proof=(payload.get('status')=='merged' and payload.get('pr_number') and
                      payload.get('integration_state')=='merged' and
                      re.fullmatch(r'[0-9a-f]{40}',str(payload.get('integrated_head') or '')) and
                      re.fullmatch(r'[0-9a-f]{40}',str(payload.get('merge_sha') or '')))
        retired_proof=(payload.get('status')=='superseded' and payload.get('terminal_evidence') and not payload.get('pr_number'))
        if not merged_proof and not retired_proof:
            return False
    path = str(payload.get('worktree') or '')
    return not any(part in path for part in ('c2-supervisor', 'c3-symphony', 'c3-control'))
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


def preserved_patch_equivalent(payload, tip, base):
    """Recognize an exact, privately archived tip after its patches were rebased.

    This does not turn an active task into a terminal one or discard originals.
    A changed tip, missing bundle, non-equivalent patch or recreated worktree
    invalidates the evidence.
    """
    import hashlib
    if payload.get('status') != 'merged':
        return False
    repo=Path(payload['repo_path']);source=Path(payload['worktree'])
    if source.exists():return False
    root=writer.STATE_ROOT/'preserved-worktrees'/payload['repo'].replace('/','_')
    for path in root.glob('*/review.json'):
        try:
            raw=path.read_bytes();review=json.loads(raw)
            if path.parent.name != hashlib.sha256(raw).hexdigest():continue
            if (review.get('worktree') != str(source) or review.get('expected_head') != tip
                    or review.get('disposition') != 'patch-equivalent'):continue
            receipt=json.loads((path.parent/'receipt.json').read_text())
            if (receipt.get('status') != 'archived' or receipt.get('head') != tip
                    or receipt.get('source') != str(source)
                    or receipt.get('original') != str(path.parent/'original')
                    or not (path.parent/'original').is_dir()):continue
            if writer._git(repo,'merge-base','--is-ancestor',review['replacement_commit'],base).returncode:continue
            patches=writer._git(repo,'cherry',base,tip)
            if patches.returncode or not patches.stdout.strip() or any(not line.startswith('- ') for line in patches.stdout.splitlines()):continue
            bundle=path.parent/'commits.bundle'
            if writer._git(repo,'bundle','verify',str(bundle)).returncode:continue
            listed=writer._git(repo,'bundle','list-heads',str(bundle),'HEAD')
            if listed.returncode or listed.stdout.split() != [tip,'HEAD']:continue
            return True
        except (OSError,ValueError,KeyError):
            continue
    return False


def preserved_integrated_history(payload, tip, base):
    """Exact archived terminal history authorizes ref GC, never lifecycle writes."""
    import hashlib
    terminal=(payload.get('status')=='merged' or
              (payload.get('status')=='superseded' and payload.get('terminal_evidence')))
    if not terminal or Path(str(payload.get('worktree') or '/')).exists():
        return False
    repo=Path(payload['repo_path'])
    if writer._git(repo,'merge-base','--is-ancestor',tip,base).returncode:
        return False
    refs=writer.STATE_ROOT/'preserved-refs'/payload['repo'].replace('/','_')
    for path in refs.glob('*/review.json'):
        try:
            raw=path.read_bytes();review=json.loads(raw)
            if path.parent.name!=hashlib.sha256(raw).hexdigest():continue
            if (review.get('head')!=tip or review.get('branch')!=payload.get('branch')
                    or review.get('source')!=payload.get('worktree')
                    or review.get('disposition')!='integrated-ref'):continue
            receipt=json.loads((path.parent/'receipt.json').read_text())
            if receipt.get('status')!='verified' or receipt.get('review_sha256')!=path.parent.name:continue
            bundle=path.parent/'commits.bundle'
            if writer._git(repo,'bundle','verify',str(bundle)).returncode:continue
            heads=writer._git(repo,'bundle','list-heads',str(bundle),'refs/heads/'+payload['branch'])
            if heads.returncode==0 and heads.stdout.split()==[tip,'refs/heads/'+payload['branch']]:return True
        except (OSError,ValueError,KeyError):continue
    root=writer.STATE_ROOT/'preserved-worktrees'/payload['repo'].replace('/','_')
    for path in root.glob('*/review.json'):
        try:
            raw=path.read_bytes();review=json.loads(raw)
            if path.parent.name!=hashlib.sha256(raw).hexdigest():continue
            if (review.get('disposition')!='integrated' or review.get('expected_head')!=tip
                    or review.get('worktree')!=payload.get('worktree')):continue
            receipt=json.loads((path.parent/'receipt.json').read_text())
            if (receipt.get('status')!='archived' or receipt.get('head')!=tip
                    or receipt.get('source')!=payload.get('worktree')
                    or receipt.get('original')!=str(path.parent/'original')
                    or not (path.parent/'original').is_dir()):continue
            bundle=path.parent/'commits.bundle'
            if writer._git(repo,'bundle','verify',str(bundle)).returncode:continue
            listed=writer._git(repo,'bundle','list-heads',str(bundle),'HEAD')
            if listed.returncode==0 and listed.stdout.split()==[tip,'HEAD']:return True
        except (OSError,ValueError,KeyError):continue
    return False


def preserve_integrated_ref(payload, tip, base, chat_id, *, scope_guard):
    """Caller holds RepoLock; preserve an integrated ref whose checkout is absent."""
    import hashlib,os,uuid
    uuid.UUID(chat_id)
    repo=Path(payload['repo_path']);branch=payload['branch']
    terminal=(payload.get('status')=='merged' or
              (payload.get('status')=='superseded' and payload.get('terminal_evidence')))
    if not terminal or not branch.startswith('task/') or Path(payload['worktree']).exists():
        return {'status':'preserved','reason':'not-an-absent-terminal-checkout'}
    if scope_guard is None:raise ValueError('scope guard required for integrated ref preservation')
    scope_guard(repo,payload['repo'])
    if preserved_integrated_history(payload,tip,base):return {'status':'already-preserved'}
    if writer._git(repo,'merge-base','--is-ancestor',tip,base).returncode:
        raise ValueError('ref has unintegrated commits')
    if writer._ok(repo,'rev-parse','refs/heads/'+branch)!=tip:
        raise ValueError('ref changed before preservation')
    listing=writer._ok(repo,'worktree','list','--porcelain')
    if 'branch refs/heads/'+branch+'\n' in listing+'\n':
        raise ValueError('another checkout still uses the ref')
    review={'disposition':'integrated-ref','repo':payload['repo'],'branch':branch,'head':tip,
            'canonical_sha':writer._ok(repo,'rev-parse',base),'source':payload['worktree'],
            'chat_id':chat_id,'source_state':'checkout already absent; no original files removed'}
    raw=json.dumps(review,sort_keys=True).encode();digest=hashlib.sha256(raw).hexdigest()
    root=writer.STATE_ROOT/'preserved-refs'/payload['repo'].replace('/','_')/digest
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    if root.stat().st_mode&0o077:raise ValueError('private ref preservation directory required')
    bundle=root/'commits.bundle'
    if not bundle.exists():writer._ok(repo,'bundle','create',str(bundle),'refs/heads/'+branch)
    os.chmod(bundle,0o600)
    writer._ok(repo,'bundle','verify',str(bundle))
    if writer._ok(repo,'bundle','list-heads',str(bundle),'refs/heads/'+branch).split()!=[tip,'refs/heads/'+branch]:
        raise ValueError('preserved ref bundle head differs')
    def save(name,data):
        with (root/name).open('wb') as stream:
            os.chmod(root/name,0o600);stream.write(data);stream.flush();os.fsync(stream.fileno())
    save('review.json',raw)
    receipt={'status':'verified','review_sha256':digest,'head':tip,'preservation':str(root)}
    save('receipt.json',json.dumps(receipt,sort_keys=True).encode())
    scope_guard(repo,payload['repo'])
    if not preserved_integrated_history(payload,tip,base):raise ValueError('ref preservation readback failed')
    return receipt


def collect_record(payload, *, dry_run=False, scope_guard=None):
    if payload.get('status') not in {'merged','superseded'} or (payload.get('status')=='superseded' and not payload.get('terminal_evidence')):
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
    allowed=lambda: (c3_allows(payload) or preserved_integrated_history(payload,tip,base)
                     or preserved_patch_equivalent(payload,tip,base))
    if not allowed():return {'removed':[],'reason':'nonterminal-active-or-recovery'}
    preserved=preserved_patch_equivalent(payload,tip,base)
    if expected and tip != expected and not preserved:
        return {'removed': [], 'reason': 'changed-after-merge'}
    if writer._git(repo, 'merge-base', '--is-ancestor', tip, base).returncode and not preserved:
        return {'removed': [], 'reason': 'unintegrated-tip'}
    if path.exists():
        dirty = writer._git(path, 'status', '--porcelain', '--untracked-files=all')
        actual = writer._git(path, 'rev-parse', 'HEAD')
        if (dirty.returncode or dirty.stdout.strip() or actual.returncode
                or actual.stdout.strip() != tip or writer._operation_in_progress(path)):
            return {'removed': [], 'reason': 'dirty-changed-or-conflicted'}
    if path.exists():
        ignored = writer._git(path, 'ls-files', '--others', '--ignored', '--exclude-standard', '-z')
        locked = writer._git(path, 'rev-parse', '--git-path', 'locked')
        if ignored.returncode or ignored.stdout:
            return {'removed': [], 'reason': 'ignored-artifacts-preserved'}
        if locked.returncode:
            return {'removed': [], 'reason': 'worktree-lock-unverifiable'}
        lock_path = Path(locked.stdout.strip())
        if not lock_path.is_absolute():
            lock_path = path / lock_path
        if lock_path.exists():
            return {'removed': [], 'reason': 'locked-worktree'}
    # Another record or checked-out worktree can still reference this branch.
    for record in writer.STATE_ROOT.glob('*/tasks/*.json'):
        try:
            other = json.loads(record.read_text())
        except (OSError, ValueError):
            return {'removed': [], 'reason': 'unreadable-ownership'}
        if (other.get('repo') == payload.get('repo') and other.get('branch') == branch
                and other.get('status') not in {'merged','superseded'}):
            return {'removed': [], 'reason': 'referenced-branch'}
    def authorize():
        if not dry_run and scope_guard is not None: scope_guard(repo, payload.get('repo'))
    authorize()
    removed = []
    if path.exists():
        if not allowed():
            return {'removed': [], 'reason': 'active-at-delete'}
        authorize()
        if dry_run:
            removed.append('worktree')
        elif (writer._git(path, 'ls-files', '--others', '--ignored', '--exclude-standard', '-z').returncode != 0
              or writer._git(path, 'ls-files', '--others', '--ignored', '--exclude-standard', '-z').stdout):
            return {'removed': [], 'reason': 'ignored-artifacts-at-delete'}
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
        # A rebased task can have its reviewed original locally and the exact
        # integrated head remotely. Both need independent positive evidence.
        remote_tip=fields[0]
        integrated_remote=(preserved and remote_tip == expected and
                           writer._git(repo,'merge-base','--is-ancestor',remote_tip,base).returncode == 0)
        if len(fields) != 2 or (remote_tip != tip and not integrated_remote) or not allowed():
            return {'removed': removed, 'reason': 'remote-changed-or-active'}
        authorize()
        if dry_run or writer._git(repo, 'push', 'origin',
                '--force-with-lease=refs/heads/' + branch + ':' + remote_tip,
                ':refs/heads/' + branch, timeout=30).returncode == 0:
            removed.append('remote-branch')
        else:
            return {'removed': removed, 'reason': 'remote-delete-refused'}
    if not allowed():
        return {'removed': removed, 'reason': 'active-at-local-delete'}
    if has_local:
        if not dry_run:
            checked = writer._git(repo, 'worktree', 'list', '--porcelain')
            if checked.returncode or 'branch refs/heads/' + branch + '\n' in checked.stdout:
                return {'removed': removed, 'reason': 'checked-out-at-delete'}
        # Ancestry was proved against fetched canonical, not a stale local HEAD.
        # Compare-and-delete cannot remove a concurrently changed branch tip.
        authorize()
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
    return {"reason": "C3 archive frozen; historical worktrees preserved"}
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
    return {"reason": "C3 archive frozen; historical worktrees preserved"}
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


def worktree_snapshot(path):
    import hashlib,os,stat
    path=Path(path)
    result={}
    for directory,dirs,files in os.walk(path,followlinks=False):
        if Path(directory)==path:
            dirs[:]=[n for n in dirs if n!='.git'];files=[n for n in files if n!='.git']
        for name in sorted(dirs+files):
            file=Path(directory)/name;rel=file.relative_to(path).as_posix();st=file.lstat()
            item={'mode':stat.S_IMODE(st.st_mode)}
            if stat.S_ISLNK(st.st_mode):item.update(kind='link',target=os.readlink(file))
            elif stat.S_ISDIR(st.st_mode):item.update(kind='directory')
            elif stat.S_ISREG(st.st_mode):
                with file.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
                item.update(kind='file',bytes=st.st_size,sha256=digest)
            else:raise ValueError('live/special file prevents archival')
            result[rel]=item
    return result


def archive_reviewed_worktree(repo, review_file, *, scope_guard=None, allow_integrated_history=False, allow_reviewed_terminal_history=False):
    """Privately preserve a reviewed, inactive worktree before retiring its checkout.

    The review is an explicit input, not lifecycle authority. Current Git and
    existing task records are rechecked under the owner lock. C3 stays frozen.
    """
    import hashlib
    import os
    import stat
    import tarfile

    repo=Path(repo).resolve();review_file=Path(review_file)
    raw=review_file.read_bytes();review=json.loads(raw)
    required={'worktree','expected_head','canonical_sha','status_sha256','disposition','replacement_commit','chat_id','inactive_confirmation','worktree_fingerprint','index_sha256'}
    if set(review)!=required or not all(isinstance(v,str) and v.strip() for v in review.values()):
        raise ValueError('complete explicit archival review required')
    import uuid
    uuid.UUID(review['chat_id'])
    if review['disposition'] not in {'integrated','patch-equivalent','superseded'}:
        raise ValueError('unsupported archival disposition')
    slug=writer._repo_slug(repo);path=Path(review['worktree']).resolve()
    if path==repo or path in repo.parents or repo in path.parents:
        raise ValueError('canonical or nested checkout cannot be archived')
    root=writer.STATE_ROOT/'preserved-worktrees'/slug.replace('/','_')/hashlib.sha256(raw).hexdigest()
    def private(name,data):
        target=root/name
        with target.open('wb') as stream:
            os.chmod(target,0o600);stream.write(data);stream.flush();os.fsync(stream.fileno())
    if path in root.parents or repo in root.parents:raise ValueError('preservation must be outside checkouts')
    snapshot=lambda:worktree_snapshot(path)
    with writer.RepoLock(slug):
        if scope_guard is not None: scope_guard(repo, slug)
        receipt=root/'receipt.json'
        if receipt.exists():
            saved=json.loads(receipt.read_text())
            if saved.get('status')=='archived' and not path.exists():return saved
        listing=writer._ok(repo,'worktree','list','--porcelain','-z')
        rows=[dict(line.partition(' ')[::2] for line in block.split('\0') if line) for block in listing.split('\0\0')]
        row=next((row for row in rows if row.get('worktree')==str(path)),None)
        if not row or 'locked' in row or _legacy_path(path):raise ValueError('unregistered, locked or frozen worktree')
        matched_owners=0
        for record in writer._task_dir_for_slug(slug).glob('*.json'):
            task=json.loads(record.read_text())
            if Path(str(task.get('worktree') or '/')).resolve()!=path:continue
            matched_owners+=1
            terminal=(task.get('status')=='merged' or
                      (task.get('status')=='superseded' and task.get('terminal_evidence')))
            reviewed_explicit=(allow_reviewed_terminal_history and scope_guard is not None and terminal)
            reviewed_history=(allow_integrated_history and review['disposition']=='integrated' and terminal)
            if (task.get('roadmap_prompt_id') and not (reviewed_history or reviewed_explicit)) or task.get('status') not in {'merged','superseded'}:
                raise ValueError('active or frozen owner forbids archival')
            if reviewed_history and (writer._git(path,'status','--porcelain=v1','--untracked-files=all').stdout.strip()
                                     or writer._operation_in_progress(path)):
                raise ValueError('integrated automatic archival requires a clean terminal checkout')
        if (allow_integrated_history or allow_reviewed_terminal_history) and matched_owners!=1:
            raise ValueError('automatic archival requires exactly one terminal owner')
        if writer._operation_in_progress(path):raise ValueError('unfinished Git operation')
        lock=Path(writer._ok(path,'rev-parse','--git-path','index.lock'))
        if not lock.is_absolute():lock=path/lock
        if lock.exists():raise ValueError('index is locked')
        if writer._git_common_dir(repo).resolve()!=writer._git_common_dir(path).resolve():raise ValueError('repository identity mismatch')
        head=writer._ok(path,'rev-parse','HEAD');status=writer._git(path,'status','--porcelain=v1','-z','--untracked-files=all').stdout
        if head!=review['expected_head'] or hashlib.sha256(status.encode()).hexdigest()!=review['status_sha256']:
            raise ValueError('reviewed checkout changed')
        canonical=writer._canonical_branch(repo)
        live=writer._ok(repo,'ls-remote','origin','refs/heads/'+canonical).split()[0]
        if live!=review['canonical_sha']:raise ValueError('canonical remote changed since review')
        if writer._git(repo,'merge-base','--is-ancestor',review['replacement_commit'],live).returncode:
            raise ValueError('replacement has not been integrated')
        if review['disposition']=='integrated' and writer._git(repo,'merge-base','--is-ancestor',head,live).returncode:
            raise ValueError('unintegrated checkout')
        if review['disposition']=='patch-equivalent':
            changes=writer._ok(path,'cherry',live,head)
            if any(line.startswith('+') for line in changes.splitlines()):raise ValueError('non-equivalent commits remain')
        if scope_guard is not None: scope_guard(repo, slug)
        root.mkdir(parents=True,exist_ok=True,mode=0o700)
        if root.stat().st_mode&0o077:raise ValueError('private preservation directory required')
        index_patch=writer._git(path,'diff','--cached','--binary','--full-index','HEAD')
        working_patch=writer._git(path,'diff','--binary','--full-index','HEAD')
        if index_patch.returncode or working_patch.returncode:raise ValueError('cannot preserve index/working patch')
        if hashlib.sha256(index_patch.stdout.encode()).hexdigest()!=review['index_sha256']:raise ValueError('reviewed index changed')
        expected=snapshot()
        if hashlib.sha256(json.dumps(expected,sort_keys=True).encode()).hexdigest()!=review['worktree_fingerprint']:
            raise ValueError('reviewed file contents changed')
        import shutil
        if shutil.disk_usage(root).free < sum(x.get('bytes',0) for x in expected.values())+64*1024*1024:
            raise ValueError('insufficient preservation space')
        private('review.json',raw)
        private('index.patch',index_patch.stdout.encode())
        private('working.patch',working_patch.stdout.encode())
        bundle=root/'commits.bundle'
        if not bundle.exists():writer._ok(path,'bundle','create',str(bundle),'HEAD')
        os.chmod(bundle,0o600)
        writer._ok(repo,'bundle','verify',str(bundle))
        if writer._ok(repo,'bundle','list-heads',str(bundle),'HEAD').split()[0]!=head:raise ValueError('bundle head differs')
        archive=root/'contents.tar.gz'
        if not archive.exists():
            partial=root/'contents.partial';os.close(os.open(partial,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600))
            with tarfile.open(partial,'w:gz',compresslevel=1) as tar:
                for name in sorted(expected):
                    info=tar.gettarinfo(path/name,arcname=name)
                    if expected[name]['kind']=='file':
                        info.type=tarfile.REGTYPE;info.linkname='';info.size=expected[name]['bytes']
                        with (path/name).open('rb') as stream:tar.addfile(info,stream)
                    else:tar.addfile(info)
            with partial.open('rb') as stream:os.fsync(stream.fileno())
            os.replace(partial,archive)
        found={}
        with tarfile.open(archive,'r:gz') as tar:
            for member in tar:
                item={'mode':member.mode}
                if member.isdir():item.update(kind='directory')
                elif member.issym():item.update(kind='link',target=member.linkname)
                elif member.isfile():
                    with tar.extractfile(member) as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
                    item.update(kind='file',bytes=member.size,sha256=digest)
                else:raise ValueError('unsupported archived entry')
                if member.name in found:raise ValueError('duplicate archived entry')
                found[member.name]=item
        if found!=expected or snapshot()!=expected:raise ValueError('archive readback or source stability failed')
        if writer._ok(path,'rev-parse','HEAD')!=head or writer._git(path,'status','--porcelain=v1','-z','--untracked-files=all').stdout!=status or writer._git(path,'diff','--cached','--binary','--full-index','HEAD').stdout!=index_patch.stdout:
            raise ValueError('Git state changed during preservation')
        result={'status':'verified','source':str(path),'head':head,'preservation':str(root),
                'files':len(expected),'source_bytes':sum(x.get('bytes',0) for x in expected.values())}
        private('manifest.json',json.dumps(expected,sort_keys=True).encode())
        private('receipt.json',json.dumps(result,sort_keys=True).encode())
        # Keep the entire original directory as well as the readback-verified
        # archive. Even a late write through an existing descriptor remains safe.
        original=root/'original'
        if original.exists():raise ValueError('preserved original destination occupied')
        if scope_guard is not None: scope_guard(repo, slug)
        try:
            os.rename(path,original)
        except OSError as error:
            import errno,tempfile
            if error.errno!=errno.EXDEV:raise
            # Preserve the actual source by same-filesystem rename as well as
            # a durable verified copy. Never copy-and-delete an open checkout:
            # late writes through existing descriptors remain recoverable.
            retained_root=Path(tempfile.mkdtemp(prefix='.codex-preserved-',dir=path.parent))
            retained=retained_root/'original'
            result['retained_source']=str(retained)
            private('receipt.json',json.dumps(result,sort_keys=True).encode())
            os.rename(path,retained)
            try:
                staging=Path(tempfile.mkdtemp(prefix='original-copy-',dir=root))/'original'
                shutil.copytree(retained,staging,symlinks=True)
                if worktree_snapshot(retained)!=expected or worktree_snapshot(staging)!=expected:
                    raise ValueError('cross-device original preservation readback changed')
                os.rename(staging,original)
            except BaseException:
                if not path.exists() and retained.exists():os.rename(retained,path)
                raise
        result['original']=str(original)
        private('receipt.json',json.dumps(result,sort_keys=True).encode())
        if path.exists():raise ValueError('source path recreated; all files preserved, metadata retained')
        # Only the now-missing registration is removed; no data directory is deleted.
        if scope_guard is not None: scope_guard(repo, slug)
        writer._ok(repo,'worktree','remove','--force',str(path))
        result['status']='archived';private('receipt.json',json.dumps(result,sort_keys=True).encode())
        return result
