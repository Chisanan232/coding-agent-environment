#!/usr/bin/env python3
"""Exercise shared receipt contention and interruption with real subprocesses."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / '.claude/skills/engineering-runtime/scripts/closeout.py'
spec = importlib.util.spec_from_file_location('closeout', HELPER)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Closeout(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name).resolve()
        self.state = self.home / 'state'
        self.binding = {'repository': 'https://github.com/example/project.git', 'pr': 7,
                        'head_sha': 'a'*40, 'merge_sha': 'b'*40, 'ticket': 'TEST-1',
                        'git_common_dir': str(self.home/'repo/.git'),
                        'worktree': str(self.home/'owned-worktree'), 'branch': 'full/TEST-1/fix/repair',
                        'base_branch': 'main', 'remote': 'origin'}
        self.file = self.home/'binding.json'
        self.write_binding()

    def write_binding(self):
        self.file.write_text(json.dumps(self.binding))

    def command(self, mode='hold'):
        return ['python3', str(HELPER), mode, '--binding', str(self.file), '--state-dir', str(self.state)]

    def run_helper(self, requests=(), mode='hold'):
        return subprocess.run(self.command(mode), input=''.join(json.dumps(r)+'\n' for r in requests),
                              text=True, capture_output=True, timeout=5)

    def holder(self):
        process = subprocess.Popen(self.command(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True)
        ready = json.loads(process.stdout.readline())
        self.assertEqual(ready['status'], 'locked')
        def close():
            if process.poll() is None:
                process.stdin.close()
                process.wait(timeout=5)
            for stream in (process.stdin,process.stdout,process.stderr):
                if not stream.closed: stream.close()
        self.addCleanup(close)
        return process

    def send(self, process, request):
        process.stdin.write(json.dumps(request)+'\n'); process.stdin.flush()
        return json.loads(process.stdout.readline())

    def test_two_hosts_contend_and_crash_releases_kernel_lock(self):
        claude = self.holder()
        codex = self.run_helper([{'op':'mark','step':'ticket_closed','evidence':'must not record'}])
        self.assertEqual(codex.returncode, 75)
        self.assertNotIn('ticket_closed', self.send(claude, {'op':'status'})['record']['steps'])
        self.send(claude, {'op':'mark','step':'ticket_closed','evidence':'observed tracker closed'})
        claude.kill(); claude.wait(timeout=5)  # Kill only this disposable test holder.
        retry = self.run_helper([{'op':'status'}])
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertIn('ticket_closed', json.loads(retry.stdout.splitlines()[0])['record']['steps'])

    def test_repo_namespacing_and_git_url_normalization(self):
        self.assertEqual(self.run_helper([{'op':'mark','step':'ticket_closed','evidence':'observed'}]).returncode,0)
        self.binding['repository']='git@github.com:Example/project.git'; self.write_binding()
        record=json.loads(self.run_helper(mode='status').stdout)['record']
        self.assertIn('ticket_closed',record['steps'])
        self.binding['repository']='https://gitlab.com/example/project'; self.write_binding()
        record=json.loads(self.run_helper(mode='status').stdout)['record']
        self.assertEqual(record['steps'],{})

    def test_conditional_remote_deletion_preserves_a_concurrent_commit(self):
        repo=self.home/'git';remote=self.home/'remote.git'
        def git(*args, cwd=None, success=True):
            r=subprocess.run(['git',*args],cwd=cwd,text=True,capture_output=True)
            if success:self.assertEqual(r.returncode,0,r.stderr)
            return r
        git('init','--bare',str(remote));git('init',str(repo))
        git('config','user.name','Fixture',cwd=repo);git('config','user.email','fixture@example.invalid',cwd=repo)
        (repo/'owned.txt').write_text('one');git('add','owned.txt',cwd=repo);git('commit','-m','Fixture first',cwd=repo)
        git('branch','topic',cwd=repo);git('remote','add','origin',str(remote),cwd=repo)
        git('push','origin','topic',cwd=repo)
        observed=git('rev-parse','topic',cwd=repo).stdout.strip()
        git('switch','topic',cwd=repo);(repo/'owned.txt').write_text('two')
        git('commit','-am','Concurrent writer',cwd=repo);git('push','origin','topic',cwd=repo)
        rejection=git('push','--force-with-lease=refs/heads/topic:'+observed,'origin',':refs/heads/topic',cwd=repo,success=False)
        self.assertNotEqual(rejection.returncode,0)
        current=git('rev-parse','topic',cwd=repo).stdout.strip()
        self.assertIn(current,git('ls-remote','origin','refs/heads/topic',cwd=repo).stdout)
        git('push','--force-with-lease=refs/heads/topic:'+current,'origin',':refs/heads/topic',cwd=repo)
        self.assertEqual(git('ls-remote','origin','refs/heads/topic',cwd=repo).stdout,'')

    def test_default_https_port_contends_on_the_same_identity(self):
        holder = self.holder()
        self.binding['repository']='https://github.com:443/Example/Project.git'
        self.write_binding()
        self.assertEqual(self.run_helper().returncode,75)
        self.assertEqual(self.send(holder,{'op':'release'})['status'],'released')

    def test_unicode_evidence_cannot_corrupt_durable_reader_bounds(self):
        requests=[{'op':'mark','step':step,'evidence':'漢'*1000} for step in module.STEPS]
        self.assertEqual(self.run_helper(requests).returncode,0)
        receipt=next(self.state.glob('*.json')); before=receipt.read_bytes()
        self.assertLessEqual(len(before),65536)
        self.assertEqual(self.run_helper(mode='status').returncode,0)
        self.assertEqual(self.run_helper([{'op':'mark','step':'ticket_closed','evidence':'漢'*4096}]).returncode,2)
        self.assertEqual(receipt.read_bytes(),before)

    def test_empty_and_repeated_scoped_selection_refuse_before_any_plan(self):
        env=dict(os.environ,CODING_AGENT_SYNC_HOME=str(self.home))
        for flags in [ ['--shared-skills='],['--global','--shared-skills='],
                       ['--claude-skills','--shared-skills=engineering-runtime','--shared-skills=post-merge-close'] ]:
            result=subprocess.run(['bash',str(ROOT/'scripts/profile-install.sh'),*flags,'--dry-run'],env=env,text=True,capture_output=True)
            self.assertEqual(result.returncode,2)
            self.assertNotIn('DIFFERS',result.stdout)
            self.assertFalse((self.home/'.claude').exists())
            self.assertFalse((self.home/'.codex').exists())

    def test_conflicting_head_and_corrupt_receipt_refuse_without_overwrite(self):
        self.run_helper()
        receipt=next(self.state.glob('*.json')); before=receipt.read_bytes()
        self.binding['head_sha']='c'*40; self.write_binding()
        self.assertEqual(self.run_helper().returncode,2)
        self.assertEqual(receipt.read_bytes(),before)
        receipt.write_text('{broken')
        self.assertEqual(self.run_helper().returncode,2)
        self.assertEqual(receipt.read_text(),'{broken')

    def test_complete_and_repeat_keeps_terminal_receipt(self):
        requests=[{'op':'mark','step':step,'evidence':'fresh observation: '+step} for step in module.STEPS]
        requests += [{'op':'mark_finalization','step':step,'evidence':'verified finalization: '+step} for step in module.FINALIZATION]
        requests += [{'op':'finish'},{'op':'release'}]
        self.assertEqual(self.run_helper(requests).returncode,0)
        receipt=next(self.state.glob('*.json')); before=receipt.read_bytes()
        self.assertTrue(json.loads(before)['complete'])
        self.assertEqual(self.run_helper(requests).returncode,0)
        self.assertEqual(receipt.read_bytes(),before)
        self.assertEqual(self.run_helper([{'op':'finish'}]).returncode,0)

    def old_completed_record(self):
        self.run_helper()
        receipt = next(self.state.glob('*.json'))
        record = json.loads(receipt.read_text())
        record.pop('finalization')
        record['schema'] = 1
        record['steps'] = {step: {'verified': True, 'evidence': 'legacy evidence: '+step} for step in module.STEPS}
        record['complete'] = True
        receipt.write_text(json.dumps(record))
        receipt.chmod(0o600)
        return receipt, record

    def test_every_missing_finalization_outcome_prevents_completion(self):
        for missing in module.FINALIZATION:
            with self.subTest(missing=missing):
                requests = [{'op':'mark','step':step,'evidence':'verified'} for step in module.STEPS]
                requests += [{'op':'mark_finalization','step':step,'evidence':'verified'} for step in module.FINALIZATION if step != missing]
                requests += [{'op':'finish'}]
                # Use separate repository identity for each independent control.
                self.binding['repository'] = 'https://github.com/example/'+missing
                self.write_binding()
                self.assertEqual(self.run_helper(requests).returncode, 2)
                status = json.loads(self.run_helper(mode='status').stdout)
                self.assertFalse(status['effective_complete'])
                self.assertIn(missing, status['pending'])

    def test_legacy_status_and_hold_preserve_historical_bytes(self):
        receipt, _ = self.old_completed_record()
        before = receipt.read_bytes()
        for mode in ('status', 'hold'):
            result = self.run_helper(mode=mode)
            self.assertEqual(result.returncode, 0)
            report = json.loads(result.stdout.splitlines()[0])
            self.assertTrue(report['record']['complete'])
            self.assertFalse(report['effective_complete'])
            self.assertEqual(report['finalization_status'], 'finalization_unverified')
            self.assertEqual(receipt.read_bytes(), before)
        self.assertEqual(self.run_helper([{'op':'finish'}]).returncode, 2)
        self.assertEqual(receipt.read_bytes(), before)

    def test_legacy_finalization_reconciliation_preserves_verified_outcomes(self):
        receipt, old = self.old_completed_record()
        holder = self.holder()
        first = sorted(module.FINALIZATION)[0]
        self.send(holder, {'op':'mark_finalization','step':first,'evidence':'observed durable result'})
        holder.kill(); holder.wait(timeout=5)
        migrated = json.loads(receipt.read_text())
        self.assertEqual(migrated['schema'], 2)
        self.assertFalse(migrated['complete'])
        self.assertEqual(migrated['steps'], old['steps'])
        self.assertEqual(migrated['binding'], old['binding'])
        requests = [{'op':'mark_finalization','step':step,'evidence':'verified scoped finalization'} for step in module.FINALIZATION if step != first]
        requests += [{'op':'finish'}]
        self.assertEqual(self.run_helper(requests).returncode, 0)
        self.assertTrue(json.loads(self.run_helper(mode='status').stdout)['effective_complete'])
        before = receipt.read_bytes()
        self.assertEqual(self.run_helper([{'op':'mark_finalization','step':first,'evidence':'different retry assertion'}]).returncode, 0)
        self.assertEqual(receipt.read_bytes(), before)

    def test_invalid_finalization_and_false_terminal_refuse_unchanged(self):
        self.run_helper()
        receipt = next(self.state.glob('*.json'))
        before = receipt.read_bytes()
        for request in [{'op':'mark_finalization','step':'unknown','evidence':'verified'},
                        {'op':'mark_finalization','step':'notes_cleared','evidence':''},
                        {'op':'mark_finalization','step':'notes_cleared','evidence':'漢'*4096}]:
            self.assertEqual(self.run_helper([request]).returncode, 2)
            self.assertEqual(receipt.read_bytes(), before)
        record = json.loads(before)
        record['complete'] = True
        record['steps'] = {step:{'verified':True,'evidence':'observed'} for step in module.STEPS}
        receipt.write_text(json.dumps(record))
        corrupt = receipt.read_bytes()
        self.assertEqual(self.run_helper(mode='status').returncode, 2)
        self.assertEqual(receipt.read_bytes(), corrupt)

    def test_pending_and_invalid_requests_are_not_completion(self):
        result=self.run_helper([{'op':'finish'}])
        self.assertEqual(result.returncode,2)
        self.assertFalse(json.loads(next(self.state.glob('*.json')).read_text())['complete'])
        for request in [{'op':'mark','step':'unknown','evidence':'something'},
                        {'op':'mark','step':'ticket_closed','evidence':''},{'op':'delete','path':'/'},[]]:
            self.assertEqual(self.run_helper([request]).returncode,2)

    def test_symlink_state_and_private_permissions_refuse(self):
        outside=self.home/'outside';outside.mkdir()
        self.state.symlink_to(outside,target_is_directory=True)
        self.assertEqual(self.run_helper().returncode,2)
        self.assertEqual(list(outside.iterdir()),[])
        self.state.unlink();self.state.mkdir(mode=0o755)
        self.assertEqual(self.run_helper().returncode,2)
        self.assertEqual(list(self.state.iterdir()),[])

    def test_identity_validation_does_not_print_credentials(self):
        self.binding['repository']='https://opaque-secret@example.com/team/project';self.write_binding()
        result=self.run_helper()
        self.assertEqual(result.returncode,2)
        self.assertNotIn('opaque-secret',result.stdout+result.stderr)
        self.assertFalse(self.state.exists())

    def test_marker_reassignment_remains_untouched(self):
        context=self.home/'repo/.codex';context.mkdir(parents=True)
        markers={context/name:b'OTHER-SESSION' for name in ('.current-ticket','.current-worktree','.current-release')}
        for path,value in markers.items():path.write_bytes(value)
        self.run_helper([{'op':'mark','step':'ticket_closed','evidence':'ticket closed'}])
        self.assertEqual(markers,{p:p.read_bytes() for p in markers})

    def test_scoped_install_preserves_unrelated_managed_and_private_state(self):
        env=dict(os.environ,CODING_AGENT_SYNC_HOME=str(self.home))
        for host,flag in [('claude','--claude-skills'),('codex','--global')]:
            protected=[]
            for rel in ['config.toml','hooks.json','settings.json','auth.json','AGENTS.md','CLAUDE.md','skills/release-watch/SKILL.md']:
                path=self.home/('.'+host)/rel;path.parent.mkdir(parents=True,exist_ok=True)
                path.write_bytes(b'opaque private or customized bytes');protected.append(path)
            cmd=['bash',str(ROOT/'scripts/profile-install.sh'),flag,
                 '--shared-skills=engineering-runtime,post-merge-close']
            report=subprocess.run(cmd+['--dry-run'],env=env,text=True,capture_output=True)
            self.assertEqual(report.returncode,0,report.stderr)
            self.assertFalse((self.home/('.'+host)/'skills/post-merge-close').exists())
            installed=subprocess.run(cmd,env=env,text=True,capture_output=True)
            self.assertEqual(installed.returncode,0,installed.stderr)
            for p in protected:self.assertEqual(p.read_bytes(),b'opaque private or customized bytes')
            before={p:p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
            again=subprocess.run(cmd,env=env,text=True,capture_output=True)
            self.assertEqual(again.returncode,0,again.stderr)
            self.assertEqual(before,{p:p.read_bytes() for p in self.home.rglob('*') if p.is_file()})
            check=subprocess.run(cmd+['--check'],env=env,text=True,capture_output=True)
            self.assertEqual(check.returncode,0,check.stderr)
            backups=list((self.home/('.'+host)/'backups/coding-agent-environment').glob('*'))
            self.assertEqual(len(backups),1)
            # Restore only the selected changed path; unrelated runtime bytes survive.
            target=self.home/('.'+host)/'skills/engineering-runtime/SKILL.md'
            target.write_bytes(b'local previous working helper')
            subprocess.run(cmd,env=env,text=True,capture_output=True,check=True)
            saved=sorted((self.home/('.'+host)/'backups/coding-agent-environment').glob('*'))[-1]/target.relative_to(self.home)
            self.assertEqual(saved.read_bytes(),b'local previous working helper')
            target.write_bytes(saved.read_bytes())
            self.assertEqual(target.read_bytes(),b'local previous working helper')
            for p in protected:self.assertEqual(p.read_bytes(),b'opaque private or customized bytes')


if __name__=='__main__':unittest.main()
