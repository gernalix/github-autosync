import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import autosync_core
import github_credentials as credentials
import repo_single_writer


class GitHubCredentialBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.bws=self.root/'bws-credential'
        self.bws.write_text('''#!/usr/bin/env python3
import os,pathlib,sys
assert not any(k in os.environ for k in ['GH_TOKEN','GITHUB_TOKEN','BWS_ACCESS_TOKEN','GH_DEBUG'])
args=sys.argv[1:]
assert args[:5]==['--ref','FEDORA_GITHUB_ENV','--var','GH_TOKEN=GITHUB_TOKEN','--']
p=pathlib.Path(__file__).with_name('count');n=int(p.read_text())+1 if p.exists() else 1;p.write_text(str(n))
cmd=args[5:]
os.execvpe(cmd[0],cmd,dict(os.environ,GH_TOKEN='inert-'+str(n)))
''');self.bws.chmod(0o755)
        self.gh=self.root/'gh'
        self.gh.write_text('''#!/usr/bin/env python3
import os,pathlib,sys
p=pathlib.Path(__file__).with_name('count');n=int(p.read_text())
assert os.environ.get('GH_TOKEN')=='inert-'+str(n)
assert not any(k in os.environ for k in ['GITHUB_TOKEN','BWS_ACCESS_TOKEN','GH_DEBUG'])
pathlib.Path(__file__).with_name('gh-called').write_text('called')
if sys.argv[1:]==['auth','git-credential','get']:
 text=''
 for line in sys.stdin:
  if not line.strip():break
  text+=line
 assert 'host=github.com' in text
 print('username=inert-user\\npassword='+os.environ['GH_TOKEN']+'\\n')
else:print('accepted-'+str(n))
''');self.gh.chmod(0o755)
        runtime=[str(self.bws),'--ref','FEDORA_GITHUB_ENV','--var','GH_TOKEN=GITHUB_TOKEN','--']
        p=patch.object(credentials,'runtime_command',return_value=runtime);p.start();self.addCleanup(p.stop)
        askpass=self.root/'askpass'
        askpass.write_text('#!/bin/sh\nprintf called > "'+str(self.root/'askpass-called')+'"\nexit 1\n');askpass.chmod(0o755)
        p=patch.dict(os.environ,{'PATH':str(self.root)+':'+os.environ['PATH'],'GH_TOKEN':'inert-old','GITHUB_TOKEN':'inert-old','BWS_ACCESS_TOKEN':'inert-bootstrap','GH_DEBUG':'api','GIT_ASKPASS':str(askpass)});p.start();self.addCleanup(p.stop)

    def test_both_clients_fetch_fresh_values_without_parent_or_debug_leak(self):
        for i,run in enumerate([autosync_core.run,repo_single_writer.run],1):
            result=run(['gh','api','repos/fixture'])
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(result.stdout.strip(),'accepted-'+str(i))
        self.assertEqual(os.environ['GH_TOKEN'],'inert-old')
        self.assertNotIn('inert-',result.stdout+result.stderr)

    def test_vault_failure_does_not_execute_native_gh(self):
        self.bws.write_text('#!/bin/sh\nexit 37\n')
        for run in [autosync_core.run,repo_single_writer.run]:
            result=run(['gh','api','repos/fixture'])
            self.assertEqual(result.returncode,37)
        self.assertFalse((self.root/'gh-called').exists())

    def test_non_github_commands_preserve_explicit_environment(self):
        result=autosync_core.run(['/bin/sh','-c','printf %s "$MARKER"'],env={'MARKER':'non-secret-fixture'})
        self.assertEqual(result.stdout,'non-secret-fixture')
        self.assertFalse((self.root/'count').exists())

    def test_git_private_credential_protocol_uses_vault_helper(self):
        env=dict(os.environ,GIT_CONFIG_GLOBAL=os.devnull,GIT_CONFIG_SYSTEM=os.devnull,GIT_TERMINAL_PROMPT='0')
        result=subprocess.run(['git','-c','credential.helper=','-c','credential.helper='+credentials.git_credential_helper(),'credential','fill'],input='protocol=https\nhost=github.com\n\n',timeout=15,capture_output=True,text=True,env=env)
        self.assertEqual(result.returncode,0,result.stderr)
        fields=dict(line.split('=',1) for line in result.stdout.splitlines() if '=' in line)
        self.assertEqual(fields['password'],'inert-1')
        self.assertFalse(result.stderr)

    def test_git_does_not_use_other_helpers_on_vault_failure(self):
        self.bws.write_text('#!/bin/sh\nexit 37\n')
        env=dict(os.environ,GIT_CONFIG_GLOBAL=os.devnull,GIT_CONFIG_SYSTEM=os.devnull,GIT_TERMINAL_PROMPT='0')
        result=subprocess.run(['git','-c','credential.helper=','-c','credential.helper='+credentials.git_credential_helper(),'credential','fill'],input='protocol=https\nhost=github.com\n\n',timeout=15,capture_output=True,text=True,env=env)
        self.assertNotEqual(result.returncode,0)
        self.assertFalse((self.root/'gh-called').exists())
        self.assertNotIn('password=',result.stdout)
        self.assertFalse((self.root/'askpass-called').exists())


if __name__=='__main__':unittest.main()
