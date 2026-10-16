import json
import subprocess
import unittest
from unittest.mock import patch
import repo_single_writer as owner

class RedactedIntegrationTests(unittest.TestCase):
    def exercise(self, *, identity='redacted@example.invalid',status='ahead',merged=True,race=False):
        calls=[]
        commits=[[{'commit':{'author':{'name':'Redacted maintainer','email':identity},'committer':{'name':'Redacted maintainer','email':identity}}}]]
        branch_calls=0
        def run(args,**kwargs):
            nonlocal branch_calls
            calls.append(args)
            ep=args[2]
            if ep=='repos/'+'example/project':data={'id':123}
            elif '/branches/' in ep:
                branch_calls+=1;data={'commit':{'sha':'base' if branch_calls==1 else 'head'}}
            elif '/compare/' in ep:data={'status':status,'behind_by':1 if status=='diverged' else 0}
            elif '/commits?' in ep:data=commits
            elif '/git/refs/' in ep:
                if race:return subprocess.CompletedProcess(args,1,'','')
                data={}
            elif '/pulls/' in ep:data={'merged':merged}
            else:raise AssertionError(ep)
            return subprocess.CompletedProcess(args,0,json.dumps(data),'')
        with patch.object(owner,'run',side_effect=run):
            result=owner._redacted_fast_forward_pr('example/project',1,'main','head','task/test',expected_repository_id=123)
        return result,calls

    def test_tested_bytes_fast_forward_without_merge_api(self):
        r,calls=self.exercise();self.assertEqual(r['status'],'merged');self.assertEqual(r['sha'],'head')
        writes=[a for a in calls if 'PATCH' in a];self.assertEqual(len(writes),1)
        self.assertIn('force=false',writes[0]);self.assertFalse(any('/merge' in a[2] for a in calls))

    def test_contact_metadata_and_divergence_never_write(self):
        for kwargs in [{'identity':'contact@example.com'},{'status':'diverged'}]:
            r,calls=self.exercise(**kwargs);self.assertEqual(r['status'],'deferred');self.assertFalse(any('PATCH' in a for a in calls))

    def test_private_policy_stays_inactive_before_registry_cutover(self):
        from pathlib import Path
        from unittest.mock import Mock
        def config(root,*args):
            return subprocess.CompletedProcess([],0,'true' if 'megavault.redactedFastForward' in args else '123','')
        conn=Mock();conn.execute.return_value.fetchall.return_value=[]
        with patch.object(owner,'resolve_repo_path',return_value=Path('/new')),patch.object(owner,'_task_by_pr',return_value=None),patch.object(owner,'_git',side_effect=config),patch.object(owner.sqlite3,'connect',return_value=conn):
            self.assertEqual(owner._redacted_source_policy('example/project',1),{'github_id':123,'active':False})

    def test_race_failure_or_unconfirmed_pr_never_claims_merge(self):
        for kwargs in [{'race':True},{'merged':False}]:
            r,_=self.exercise(**kwargs);self.assertEqual(r['status'],'deferred')

if __name__=='__main__':unittest.main()
