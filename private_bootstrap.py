"""Task-scoped PRIVATE bootstrap through the existing cutover and Git owners.

Authorization is private local task state, never a registry activation receipt.
All authoritative checks are repeated against the installed owner and GitHub.
"""
from contextlib import contextmanager
import hashlib
import importlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile


def authorization_path(task_path):
    return task_path.with_suffix('.bootstrap')


def _private_json(path):
    # Open without following a substituted symlink and inspect the opened file.
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError('private bootstrap custody required')
        raw = stream.read()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def _cutover_owner():
    root = Path.home() / 'MegaVault'
    sys.path.insert(0, str(root))
    try:
        return importlib.import_module('ai.repository_cutover')
    finally:
        sys.path.pop(0)


def _api(owner, endpoint):
    return owner._github_json(endpoint)


def _bindings(owner, task, auth, plan):
    target = plan['after']
    repo = task['repo']
    root = Path(task['repo_path']).resolve(strict=True)
    if (auth['task_id'] != task['task_id'] or auth['pr_number'] != task['pr_number']
            or auth['repo'] != repo or auth['branch'] != task['branch']
            or not task['branch'].startswith('task/')
            or task['canonical_branch'] != target['branch']
            or plan['before']['canonical'] != 1 or plan['before']['status'] != 'active'
            or auth['base'] != target['head']
            or Path(target['worktree_path']).resolve(strict=True) != root
            or owner._normalize_repo_url(target['remote_url']) != 'https://github.com/' + repo.lower()
            or owner._normalize_repo_url(plan['before']['remote_url']) == owner._normalize_repo_url(target['remote_url'])):
        raise ValueError('private bootstrap binding changed')
    if not re.fullmatch(r'[0-9a-f]{40}', auth['head']):
        raise ValueError('private bootstrap exact head required')
    if owner._ok(root, 'config', '--bool', '--get', 'megavault.redactedFastForward') != 'true':
        raise ValueError('private bootstrap redacted policy required')
    if owner._ok(root, 'config', '--get', 'megavault.redactedRepositoryId') != str(target['github_id']):
        raise ValueError('private bootstrap repository ID changed')
    push_urls = owner._ok(root, 'remote', 'get-url', '--push', '--all', 'origin').splitlines()
    if len(push_urls) != 1 or owner._normalize_repo_url(push_urls[0]) != owner._normalize_repo_url(target['remote_url']):
        raise ValueError('private bootstrap push origin changed')
    for checkout in (root, Path(task['worktree']).resolve(strict=True)):
        if owner._normalize_repo_url(owner._ok(checkout, 'remote', 'get-url', 'origin')) != owner._normalize_repo_url(target['remote_url']):
            raise ValueError('private bootstrap origin changed')
    if owner._ok(Path(task['worktree']), 'rev-parse', 'HEAD') != auth['head']:
        raise ValueError('private bootstrap task head changed')
    if owner._ok(Path(task['worktree']), 'branch', '--show-current') != task['branch']:
        raise ValueError('private bootstrap task branch changed')
    # The owner's exact object/ref inventory must cover the approved task too.
    objects = set(plan['clean_object_ids'])
    reachable = owner._ok(root, 'rev-list', '--objects', auth['head']).splitlines()
    if not reachable or any(line.split()[0] not in objects for line in reachable):
        raise ValueError('private bootstrap task object set unreviewed')
    if target['advertised_refs'].get('refs/heads/' + task['branch']) != auth['head']:
        raise ValueError('private bootstrap task ref unreviewed')


def _github_checks(owner, task, auth, plan):
    repo, target = task['repo'], plan['after']
    metadata = _api(owner, 'repos/' + repo)
    if (metadata['id'] != target['github_id'] or metadata['full_name'].lower() != repo.lower()
            or metadata['private'] is not True or metadata['fork'] or metadata['is_template']
            or metadata['default_branch'] != target['branch']):
        raise ValueError('private bootstrap target must remain PRIVATE')
    pr = _api(owner, f"repos/{repo}/pulls/{task['pr_number']}")
    if (pr['state'] != 'open' or pr['draft'] or pr['merged'] or pr['mergeable'] is not True
            or not pr['title'].startswith(owner.PR_PREFIX)
            or owner._dependency_reason(repo, pr.get('body') or '')
            or pr['head']['sha'] != auth['head'] or pr['head']['ref'] != task['branch']
            or pr['base']['ref'] != target['branch']
            or any(pr[side]['repo']['id'] != target['github_id'] for side in ('base', 'head'))):
        raise ValueError('private bootstrap PR provenance changed')
    if _api(owner, f"repos/{repo}/branches/{target['branch']}")['commit']['sha'] != auth['base']:
        raise ValueError('private bootstrap main advanced')
    run = _api(owner, f"repos/{repo}/actions/runs/{auth['ci_run_id']}")
    if (run['id'] != auth['ci_run_id'] or run['head_sha'] != auth['head']
            or run['repository']['id'] != target['github_id']
            or run['head_repository']['id'] != target['github_id']
            or run['event'] not in ('push', 'workflow_dispatch') or run['path'] != auth['ci_workflow']
            or run['head_branch'] != task['branch']
            or run['status'] != 'completed' or run['conclusion'] != 'success'):
        raise ValueError('private bootstrap exact-head CI required')
    checks = _api(owner, f"repos/{repo}/commits/{auth['head']}/check-runs?per_page=100")
    runs = checks['check_runs']
    if (not runs or checks['total_count'] != len(runs)
            or any(check['head_sha'] != auth['head'] or check['status'] != 'completed'
                   or check['conclusion'] not in ('success', 'neutral', 'skipped') for check in runs)
            or not any(check['check_suite']['id'] == run['check_suite_id']
                       and check['app']['slug'] == 'github-actions' and check['conclusion'] == 'success' for check in runs)):
        raise ValueError('private bootstrap check provenance incomplete')
    statuses = _api(owner, f"repos/{repo}/commits/{auth['head']}/status")
    if statuses['sha'] != auth['head'] or (statuses['total_count'] and statuses['state'] != 'success'):
        raise ValueError('private bootstrap commit statuses not successful')
    workflow = _api(owner, f"repos/{repo}/actions/workflows/{run['workflow_id']}")
    if workflow['path'] != auth['ci_workflow'] or workflow['state'] != 'active':
        raise ValueError('private bootstrap approved workflow changed')


@contextmanager
def verified(owner, task, auth):
    """Fence the fresh canonical CAS check and FF with the existing writer lock."""
    plan, digest = _private_json(Path(auth['plan_path']))
    if digest != auth['plan_digest']:
        raise ValueError('private bootstrap plan changed')
    _bindings(owner, task, auth, plan)
    cutover = _cutover_owner()
    database = Path.home() / 'MegaVault/megavault.sqlite'
    with cutover.store.writer(database) as connection:
        registered = connection.execute(
            'SELECT remote_url FROM repositories WHERE canonical=1 AND remote_url IS NOT NULL').fetchall()
        if any(owner._normalize_repo_url(row[0]) == owner._normalize_repo_url(plan['after']['remote_url'])
               for row in registered):
            raise ValueError('private bootstrap target is already canonical')
        # Read-only owner API; the writer is used solely as a CAS fence.
        result = cutover.execute(database, plan, apply=False)
        if result.get('status') != 'PREPARED' or result.get('writes') != 0 or result.get('target_private') is not True:
            raise ValueError('private bootstrap owner did not approve current plan')
        _github_checks(owner, task, auth, plan)
        def revalidate():
            if _private_json(Path(auth['plan_path']))[1] != auth['plan_digest']:
                raise ValueError('private bootstrap plan changed')
            _bindings(owner, task, auth, plan)
            current = cutover.execute(database, plan, apply=False)
            if current != result:
                raise ValueError('private bootstrap owner state changed')
            _github_checks(owner, task, auth, plan)
        yield plan, revalidate


def authorize(owner, repo, task_id, plan_path, head, ci_run_id, ci_workflow):
    with owner.RepoLock(owner._repo_slug(repo)):
        task_path = owner._task_record(repo, task_id)
        task = json.loads(task_path.read_text())
        plan_path = plan_path.expanduser().resolve(strict=True)
        plan, digest = _private_json(plan_path)
        if not re.fullmatch(r'\.github/workflows/[^/]+\.ya?ml', ci_workflow) or ci_run_id <= 0:
            raise ValueError('private bootstrap approved CI binding required')
        auth = dict(task_id=task_id, pr_number=task['pr_number'], repo=task['repo'],
                    branch=task['branch'], base=plan['after']['head'], head=head,
                    plan_path=str(plan_path), plan_digest=digest,
                    ci_run_id=ci_run_id, ci_workflow=ci_workflow)
        with verified(owner, task, auth):
            pass
        path = authorization_path(task_path)
        temporary = path.with_suffix('.bootstrap.tmp')
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'w') as stream:
            json.dump(auth, stream, sort_keys=True)
            stream.write('\n')
        temporary.replace(path)
    return {'status': 'authorized', 'scope': 'private-bootstrap-only'}


# Command-local hook; other installed hooks are retained through symlinks.
# Git's receive-pack compares the advertised old OID atomically at update time.
_PRE_PUSH = """#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys
approval = json.loads(pathlib.Path(__file__).with_name('expected.json').read_text())
raw = sys.stdin.buffer.read()
lines = raw.decode().splitlines()
if len(lines) != 1:
    sys.exit(1)
fields = lines[0].split()
if (len(fields) != 4 or fields[1:] != [approval['head'], approval['ref'], approval['base']]
        or len(sys.argv) != 3 or sys.argv[2] != approval['remote']):
    sys.exit(1)
previous = approval['previous']
if previous and os.access(previous, os.X_OK):
    sys.exit(subprocess.run([previous, *sys.argv[1:]], input=raw).returncode)
"""


def _push_exact_base(owner, root, branch, base, head, expected_remote):
    """Nonforced FF with an expected-old-OID gate, only inside integration."""
    if owner._git(root, 'merge-base', '--is-ancestor', base, head).returncode:
        raise ValueError('private bootstrap is not a local fast-forward')
    push_urls = owner._ok(root, 'remote', 'get-url', '--push', '--all', 'origin').splitlines()
    if (len(push_urls) != 1 or owner._normalize_repo_url(push_urls[0])
            != owner._normalize_repo_url(expected_remote)):
        raise ValueError('private bootstrap push destination changed')
    hooks = Path(owner._ok(root, 'rev-parse', '--git-path', 'hooks'))
    hooks = (root / hooks).resolve() if not hooks.is_absolute() else hooks.resolve()
    with tempfile.TemporaryDirectory(prefix='private-bootstrap-') as directory:
        temporary = Path(directory)
        if hooks.is_dir():
            for entry in hooks.iterdir():
                if entry.name != 'pre-push':
                    (temporary / entry.name).symlink_to(entry)
        previous = hooks / 'pre-push'
        (temporary / 'expected.json').write_text(json.dumps(dict(
            head=head, base=base, ref='refs/heads/' + branch, remote=push_urls[0],
            previous=str(previous) if previous.is_file() else None)))
        hook = temporary / 'pre-push'
        hook.write_text(_PRE_PUSH); hook.chmod(0o700)
        result = owner._git(root, '-c', 'core.hooksPath=' + directory,
                            '-c', 'push.followTags=false',
                            '-c', 'credential.helper=', '-c', 'credential.helper=!gh auth git-credential',
                            'push', '--porcelain', '--atomic', 'origin', head + ':refs/heads/' + branch)
        if result.returncode:
            raise ValueError('private bootstrap push rejected or unconfirmed; read back before retry')


def integrate(owner, repo, number, branch, head, head_branch, repository_id):
    found = owner._task_by_pr(repo, number)
    if not found or not authorization_path(found[0]).exists():
        return dict(repo=repo, number=number, status='deferred', reason='canonical-cutover-not-activated')
    try:
        auth, _ = _private_json(authorization_path(found[0]))
        task = found[1]
        if auth['head'] != head or auth['branch'] != head_branch:
            raise ValueError('private bootstrap approval head changed')
        # History verification stays in the existing redacted FF owner. Its last
        # callback holds the canonical owner's fence through the nonforced write.
        with verified(owner, task, auth) as (plan, revalidate):
            if plan['after']['github_id'] != repository_id or plan['after']['branch'] != branch:
                raise ValueError('private bootstrap target changed')
            def recheck(base):
                if base != auth['base'] or _private_json(Path(auth['plan_path']))[1] != auth['plan_digest']:
                    raise ValueError('private bootstrap baseline changed')
                revalidate()
            result = owner._redacted_fast_forward_pr(repo, number, branch, head, head_branch,
                         expected_repository_id=repository_id, before_update=recheck,
                         update_ref=lambda: _push_exact_base(owner, Path(task['repo_path']), branch, auth['base'], head, plan['after']['remote_url']))
        result['private_bootstrap'] = True
        return result
    except Exception:
        return dict(repo=repo, number=number, status='deferred', reason='private-bootstrap-verification-failed')
