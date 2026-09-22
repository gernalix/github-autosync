#!/usr/bin/env python3
"""Idempotently configure the Fedora GitHub Reconcile Push monitor."""
from __future__ import annotations

import json
import secrets
import subprocess
from pathlib import Path


SSH = Path("/home/daniele/projects/vm_oracle/scripts/oracle_ssh.sh")
KUMA_BASE = "https://kuma.danielegalati.com"
SECRET_SERVICE_ATTRIBUTES = (
    "application",
    "github-autosync",
    "credential",
    "github-reconcile-push-url",
)


def configure() -> dict[str, object]:
    proposed = secrets.token_urlsafe(24)
    remote_script = """
import datetime, json, os, sqlite3, subprocess
db = '/opt/uptime-kuma/data/kuma.db'
compose = '/opt/uptime-kuma/docker-compose.yml'
name = 'Fedora GitHub Reconcile'
proposed = %s
conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row
rows = conn.execute('SELECT id,push_token,type,active,interval,retry_interval,maxretries FROM monitor WHERE name=?', (name,)).fetchall()
if len(rows) > 1:
    raise RuntimeError('duplicate_monitor_name')
existing = rows[0] if rows else None
token = str(existing['push_token']) if existing and existing['push_token'] else proposed
change = not existing or existing['type'] != 'push' or existing['active'] != 1 or existing['interval'] != 180 or existing['retry_interval'] != 60 or existing['maxretries'] != 0 or not existing['push_token']
if change:
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%%Y%%m%%dT%%H%%M%%SZ')
    backup = '/opt/uptime-kuma/data/kuma.db.before-github-reconcile-' + stamp
    backup_conn = sqlite3.connect(backup)
    conn.backup(backup_conn)
    backup_conn.close()
    os.chmod(backup, 0o600)
    conn.close()
    subprocess.run(['docker','compose','-f',compose,'stop','uptime-kuma'],check=True,stdout=subprocess.DEVNULL)
    try:
        conn = sqlite3.connect(db)
        conn.execute('BEGIN IMMEDIATE')
        if existing:
            conn.execute('UPDATE monitor SET type=?,active=1,interval=180,retry_interval=60,maxretries=0,push_token=? WHERE id=?', ('push',token,existing['id']))
            monitor_id = existing['id']
        else:
            owner = conn.execute("SELECT user_id FROM monitor WHERE type='push' AND user_id IS NOT NULL LIMIT 1").fetchone()
            if not owner:
                raise RuntimeError('push_monitor_owner_missing')
            cur = conn.execute('INSERT INTO monitor(name,type,active,interval,retry_interval,maxretries,push_token,user_id) VALUES(?,?,?,?,?,?,?,?)', (name,'push',1,180,60,0,token,owner[0]))
            monitor_id = cur.lastrowid
        if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise RuntimeError('integrity_check_failed')
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
        subprocess.run(['docker','compose','-f',compose,'start','uptime-kuma'],check=True,stdout=subprocess.DEVNULL)
else:
    monitor_id = existing['id']
    conn.close()
print(json.dumps({'id':monitor_id,'token':token,'updated':bool(change)}))
""" % json.dumps(proposed)
    result = subprocess.run([str(SSH), "sudo -n python3 -"], input=remote_script,
                            text=True, capture_output=True, timeout=90)
    if result.returncode:
        raise RuntimeError("kuma_configuration_failed:" + result.stderr.strip().splitlines()[-1][:160])
    data = json.loads(result.stdout.strip().splitlines()[-1])
    token = str(data.pop("token"))
    if not token or "/" in token:
        raise RuntimeError("invalid_kuma_token")
    push_url = f"{KUMA_BASE}/api/push/{token}"
    try:
        secret_service = subprocess.run(
            [
                "secret-tool",
                "store",
                "--label=GitHub Autosync Kuma push URL",
                *SECRET_SERVICE_ATTRIBUTES,
            ],
            input=push_url + "\n",
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("secret-tool is not installed; install Fedora libsecret") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"secret_service_store_failed:{exc.__class__.__name__}") from exc
    if secret_service.returncode:
        raise RuntimeError("secret_service_store_failed")
    return data


if __name__ == "__main__":
    print(json.dumps(configure(), sort_keys=True))
