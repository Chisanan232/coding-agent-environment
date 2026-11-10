#!/usr/bin/env python3
"""Pending-ASK ledger: records requests, never approves, resumes once and only after revalidation."""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = spec_from_file_location('ask_pending', ROOT / 'scripts/ask-pending.py')
ap = module_from_spec(spec)
spec.loader.exec_module(ap)

T0 = datetime(2026, 11, 10, 2, 0, 0, tzinfo=timezone.utc)
HEAD1, HEAD2 = 'a' * 40, 'b' * 40
MERGE = 'gh pr merge {pr} --repo o/r --merge --match-head-commit {head}'

# A disposable stand-in for owner-merge-gate.py: prints what a scenario file says, so the real gate is never touched.
FAKE_GATE = '''
import json, os, sys
scn = json.load(open(os.environ["FAKE_GATE_SCENARIO"]))
pr = sys.argv[sys.argv.index("--pr") + 1]
entry = scn[pr]
print(json.dumps(entry["out"]))
sys.exit(entry.get("rc", 0))
'''


class Env(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.tmp.name, 'asks')
        os.makedirs(self.dir, mode=0o700)
        self.gate = os.path.join(self.tmp.name, 'fake-gate.py')
        Path(self.gate).write_text(FAKE_GATE)
        self.scn = os.path.join(self.tmp.name, 'scn.json')
        os.environ['FAKE_GATE_SCENARIO'] = self.scn
        self.set_gate({})

    def tearDown(self):
        self.tmp.cleanup()

    def set_gate(self, passing):
        """passing: {pr: head} for PRs whose gate PASSes at that head and prints the pinned merge action."""
        scn = {str(pr): {'out': {'verdict': 'PASS', 'head': head, 'merge_command': MERGE.format(pr=pr, head=head),
                                 'decision': {'outcome': 'ASK'}}} for pr, head in passing.items()}
        Path(self.scn).write_text(json.dumps(scn))

    def validator(self):
        return ap.gate_validator('/dev/null', gate_cmd=[sys.executable, self.gate])

    def rec(self, pr, head=HEAD1, at=T0, **kw):
        kw.setdefault('prompt', 'none')
        return ap.record(self.dir, MERGE.format(pr=pr, head=head), f'o/r#{pr}', head, kw.pop('deps', []), 'owner_approval',
                         'sess-1', kw.pop('request_id', ''), kw.pop('grace_s', 900), kw.pop('prompt'), at)

    def resume(self, item_id, head, at):
        return ap.begin_resume(self.dir, item_id, head, self.validator(), at)

    def state(self, item_id):
        return ap.load(self.dir, item_id)['state']


class Recording(Env):
    def test_record_keeps_exact_action_target_head_dependencies_and_trigger(self):
        item, created = self.rec(7, deps=['o/r#6'], request_id='toolu_123')
        self.assertTrue(created)
        self.assertEqual(item['action'], MERGE.format(pr=7, head=HEAD1))
        self.assertEqual((item['target'], item['head'], item['dependencies']), ('o/r#7', HEAD1, ['o/r#6']))
        self.assertEqual((item['request_id'], item['resume_trigger'], item['state']), ('toolu_123', 'owner_approval', 'PENDING'))

    def test_no_request_id_is_stored_as_none_never_invented(self):
        self.assertIsNone(self.rec(7)[0]['request_id'])

    def test_files_are_private(self):
        item, _ = self.rec(7)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.dir, item['id'] + '.json')).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(self.dir, 'receipts.jsonl')).st_mode), 0o600)

    def test_bad_head_and_missing_fields_are_refused(self):
        with self.assertRaises(ap.AskError):
            ap.record(self.dir, 'x', 'o/r#1', 'not-a-sha', [], 'owner_approval', '', '', 900, 'none', T0)
        with self.assertRaises(ap.AskError):
            ap.record(self.dir, '', 'o/r#1', HEAD1, [], 'owner_approval', '', '', 900, 'none', T0)

    def test_grace_is_capped_at_fifteen_minutes(self):
        self.assertEqual(self.rec(7, grace_s=99999)[0]['grace_s'], 900)

    def test_duplicate_record_is_a_noop_and_cannot_reopen_a_consumed_item(self):
        first, _ = self.rec(7)
        self.set_gate({7: HEAD1})
        self.resume(first['id'], HEAD1, T0)
        ap.finish(self.dir, first['id'], 'executed', 'verify:VERIFIED', T0)
        again, created = self.rec(7)
        self.assertFalse(created)
        self.assertEqual(again['state'], 'CONSUMED')

    def test_many_items_can_be_pending_at_once(self):
        ids = [self.rec(pr)[0]['id'] for pr in range(1, 6)]
        self.assertEqual(len(set(ids)), 5)
        self.assertTrue(all(self.state(i) == 'PENDING' for i in ids))


class GraceAndLiveness(Env):
    def test_grace_only_says_move_on_it_changes_no_state(self):
        item, _ = self.rec(7)
        before = ap.view(item, T0 + timedelta(minutes=5))
        after = ap.view(item, T0 + timedelta(minutes=16))
        self.assertFalse(before['move_on'])
        self.assertTrue(after['move_on'])
        self.assertEqual(self.state(item['id']), 'PENDING')

    def test_a_pending_item_is_not_a_live_prompt(self):
        pending, _ = self.rec(7)
        live, _ = self.rec(8, prompt='live', request_id='toolu_9')
        self.assertFalse(ap.view(pending, T0)['live_prompt'])
        self.assertTrue(ap.view(live, T0)['live_prompt'])

    def test_restart_makes_live_prompts_unknown_and_nothing_approved(self):
        live, _ = self.rec(8, prompt='live')
        self.assertEqual(ap.restart(self.dir, 'sess-2', T0), [live['id']])
        item = ap.load(self.dir, live['id'])
        self.assertEqual((item['prompt'], item['state']), ('unknown', 'PENDING'))

    def test_restart_in_the_same_session_changes_nothing(self):
        self.rec(8, prompt='live')
        self.assertEqual(ap.restart(self.dir, 'sess-1', T0), [])

    def test_state_survives_a_fresh_process(self):
        item, _ = self.rec(7)
        out = subprocess.run([sys.executable, str(ROOT / 'scripts/ask-pending.py'), 'list', '--json'], capture_output=True, text=True,
                             env={**os.environ, 'ASK_PENDING_DIR': self.dir}, check=True).stdout
        self.assertEqual([p['id'] for p in json.loads(out)['pending']], [item['id']])


class Resume(Env):
    def test_late_approval_resumes_only_the_intended_action(self):
        a, _ = self.rec(7)
        b, _ = self.rec(8)
        self.set_gate({7: HEAD1, 8: HEAD1})
        late = T0 + timedelta(hours=3)  # far past the 15 min grace; time itself approved nothing
        self.assertEqual(self.state(a['id']), 'PENDING')
        got = self.resume(b['id'], HEAD1, late)
        self.assertEqual(got['action'], MERGE.format(pr=8, head=HEAD1))
        self.assertEqual((self.state(a['id']), self.state(b['id'])), ('PENDING', 'RESUMING'))

    def test_resume_consumes_once_and_a_duplicate_event_is_refused(self):
        a, _ = self.rec(7)
        self.set_gate({7: HEAD1})
        self.resume(a['id'], HEAD1, T0)
        with self.assertRaises(ap.AskError):  # duplicate resume while in flight
            self.resume(a['id'], HEAD1, T0)
        ap.finish(self.dir, a['id'], 'executed', 'verify:VERIFIED', T0)
        with self.assertRaises(ap.AskError):  # duplicate finish after consumption
            ap.finish(self.dir, a['id'], 'executed', 'verify:VERIFIED', T0)
        with self.assertRaises(ap.AskError):  # resume of a consumed item
            self.resume(a['id'], HEAD1, T0)
        self.assertEqual(self.state(a['id']), 'CONSUMED')

    def test_changed_head_closes_the_item_and_never_runs_the_old_action(self):
        a, _ = self.rec(7)
        self.set_gate({7: HEAD2})
        with self.assertRaisesRegex(ap.AskError, 'head changed'):
            self.resume(a['id'], HEAD2, T0)
        self.assertEqual(self.state(a['id']), 'STALE_HEAD')
        with self.assertRaises(ap.AskError):
            self.resume(a['id'], HEAD1, T0)

    def test_a_new_head_for_the_same_target_supersedes_the_old_pending_item(self):
        old, _ = self.rec(7, head=HEAD1)
        new, _ = self.rec(7, head=HEAD2)
        self.assertEqual((self.state(old['id']), self.state(new['id'])), ('STALE_HEAD', 'PENDING'))

    def test_gate_refusal_blocks_resume_and_leaves_the_item_pending(self):
        a, _ = self.rec(7)
        Path(self.scn).write_text(json.dumps({'7': {'rc': 2, 'out': {'verdict': 'WAIT', 'head': HEAD1}}}))
        with self.assertRaisesRegex(ap.AskError, 'revalidation failed'):
            self.resume(a['id'], HEAD1, T0)
        self.assertEqual(self.state(a['id']), 'PENDING')

    def test_gate_naming_a_different_action_or_head_is_refused(self):
        a, _ = self.rec(7)
        Path(self.scn).write_text(json.dumps({'7': {'out': {'verdict': 'PASS', 'head': HEAD1, 'merge_command': 'gh pr merge 7 --admin'}}}))
        with self.assertRaisesRegex(ap.AskError, 'differs from the recorded action'):
            self.resume(a['id'], HEAD1, T0)
        Path(self.scn).write_text(json.dumps({'7': {'out': {'verdict': 'PASS', 'head': HEAD2, 'merge_command': a['action']}}}))
        with self.assertRaisesRegex(ap.AskError, 'head differs'):
            self.resume(a['id'], HEAD1, T0)
        self.assertEqual(self.state(a['id']), 'PENDING')

    def test_unreadable_gate_output_fails_closed(self):
        a, _ = self.rec(7)
        Path(self.gate).write_text('print("not json")')
        with self.assertRaisesRegex(ap.AskError, 'unreadable'):
            self.resume(a['id'], HEAD1, T0)

    def test_denial_is_terminal_and_never_retried_by_the_ledger(self):
        a, _ = self.rec(7)
        self.set_gate({7: HEAD1})
        self.resume(a['id'], HEAD1, T0)
        self.assertEqual(ap.finish(self.dir, a['id'], 'denied', 'host denied', T0)['state'], 'DENIED')
        with self.assertRaises(ap.AskError):
            self.resume(a['id'], HEAD1, T0)

    def test_expired_prompt_is_terminal(self):
        a, _ = self.rec(7, prompt='live')
        self.set_gate({7: HEAD1})
        self.resume(a['id'], HEAD1, T0)
        self.assertEqual(ap.finish(self.dir, a['id'], 'expired', 'prompt expired', T0)['state'], 'EXPIRED')

    def test_executed_needs_durable_proof(self):
        a, _ = self.rec(7)
        self.set_gate({7: HEAD1})
        self.resume(a['id'], HEAD1, T0)
        with self.assertRaisesRegex(ap.AskError, 'needs --ref'):
            ap.finish(self.dir, a['id'], 'executed', '', T0)

    def test_failed_returns_to_pending_with_prompt_unknown(self):
        a, _ = self.rec(7)
        self.set_gate({7: HEAD1})
        self.resume(a['id'], HEAD1, T0)
        item = ap.finish(self.dir, a['id'], 'failed', '', T0)
        self.assertEqual((item['state'], item['prompt']), ('PENDING', 'unknown'))

    def test_finish_without_resume_is_refused(self):
        a, _ = self.rec(7)
        with self.assertRaisesRegex(ap.AskError, 'only a RESUMING item'):
            ap.finish(self.dir, a['id'], 'executed', 'x', T0)

    def test_a_crashed_resume_returns_to_pending_never_to_done(self):
        a, _ = self.rec(7)
        self.set_gate({7: HEAD1})
        self.resume(a['id'], HEAD1, T0)
        later = T0 + timedelta(seconds=ap.LEASE_S + 1)
        item = ap._recover(self.dir, ap.load(self.dir, a['id']), later)
        self.assertEqual((item['state'], item['prompt']), ('PENDING', 'unknown'))
        self.resume(a['id'], HEAD1, later)  # a fresh, revalidated resume is allowed
        self.assertEqual(self.state(a['id']), 'RESUMING')

    def test_cancel_is_terminal(self):
        a, _ = self.rec(7)
        ap.cancel(self.dir, a['id'], 'superseded by owner', T0)
        with self.assertRaises(ap.AskError):
            self.resume(a['id'], HEAD1, T0)

    def test_receipts_record_every_transition(self):
        a, _ = self.rec(7)
        self.set_gate({7: HEAD1})
        self.resume(a['id'], HEAD1, T0)
        ap.finish(self.dir, a['id'], 'executed', 'verify:VERIFIED', T0)
        events = [json.loads(l)['event'] for l in Path(self.dir, 'receipts.jsonl').read_text().splitlines()]
        self.assertEqual(events, ['recorded', 'resuming', 'finished'])


class Scenario(Env):
    """The disposable-environment demonstration from the task: two unanswered ASKs, independent work, late approval."""

    def test_two_pending_asks_outlive_the_grace_while_independent_work_advances(self):
        work_done = []
        first, _ = self.rec(7, prompt='live', request_id='toolu_A')
        second, _ = self.rec(8, prompt='none', deps=['o/r#7'])
        self.set_gate({7: HEAD1, 8: HEAD1})

        # 16 minutes pass with no answer: both items say "move on"; neither is approved, expired or resumed.
        t = T0 + timedelta(minutes=16)
        listed = [ap.view(ap.load(self.dir, i['id']), t) for i in (first, second)]
        self.assertTrue(all(v['move_on'] and v['state'] == 'PENDING' for v in listed))
        for task in ('docs', 'tests', 'review'):  # independent runnable tasks proceed
            work_done.append(task)
            t += timedelta(minutes=10)
        self.assertEqual(work_done, ['docs', 'tests', 'review'])
        self.assertEqual((self.state(first['id']), self.state(second['id'])), ('PENDING', 'PENDING'))

        # The owner answers the second request first, much later. Only that action comes back.
        got = self.resume(second['id'], HEAD1, t)
        self.assertEqual(got['action'], MERGE.format(pr=8, head=HEAD1))
        self.assertEqual(self.state(first['id']), 'PENDING')
        ap.finish(self.dir, second['id'], 'executed', 'verify:VERIFIED', t)

        # The first request is then answered with a denial; a duplicate of the earlier event changes nothing.
        self.resume(first['id'], HEAD1, t)
        ap.finish(self.dir, first['id'], 'denied', 'owner denied', t)
        with self.assertRaises(ap.AskError):
            ap.finish(self.dir, second['id'], 'executed', 'verify:VERIFIED', t)
        self.assertEqual((self.state(first['id']), self.state(second['id'])), ('DENIED', 'CONSUMED'))

    def test_merge_gate_is_still_what_authorizes_a_merge(self):
        """A recorded item the gate no longer passes cannot be handed back, whatever the ledger says."""
        a, _ = self.rec(7)
        Path(self.scn).write_text(json.dumps({'7': {'rc': 1, 'out': {'verdict': 'REFUSE', 'head': HEAD1}}}))
        with self.assertRaises(ap.AskError):
            self.resume(a['id'], HEAD1, T0 + timedelta(days=1))
        self.assertNotIn('run_exactly', json.dumps(ap.load(self.dir, a['id'])))


class NoSelfAuthorization(unittest.TestCase):
    def test_ledger_has_no_approve_command_and_never_calls_gh_merge(self):
        text = (ROOT / 'scripts/ask-pending.py').read_text()
        self.assertNotIn('approve', [a for a in ('approve',) if f'add_parser("{a}"' in text])
        self.assertNotIn('"gh", "pr", "merge"', text)
        self.assertNotIn('--admin', text.replace("'gh pr merge 7 --admin'", ''))


if __name__ == '__main__':
    unittest.main()
