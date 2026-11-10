#!/usr/bin/env python3
"""Pending-ASK ledger: records requests, never approves, resumes once and only after revalidation."""
import contextlib
import io
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

    def finish(self, item_id, outcome, ref='', at=T0, problems=()):
        return ap.finish(self.dir, item_id, outcome, ref, at, lambda _item: list(problems))

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
        for bad_head in ('not-a-sha', 'abc1234', 'A' * 40):
            with self.assertRaises(ap.AskError):
                ap.record(self.dir, 'x o/r ' + bad_head, 'o/r#1', bad_head, [], 'owner_approval', '', '', 900, 'none', T0)
        with self.assertRaises(ap.AskError):  # target not owner/repo#N
            ap.record(self.dir, f'gh pr merge o/r {HEAD1}', 'o/r', HEAD1, [], 'owner_approval', '', '', 900, 'none', T0)
        with self.assertRaises(ap.AskError):  # action not pinned to the head
            ap.record(self.dir, 'gh pr merge 1 --repo o/r', 'o/r#1', HEAD1, [], 'owner_approval', '', '', 900, 'none', T0)
        with self.assertRaises(ap.AskError):
            ap.record(self.dir, '', 'o/r#1', HEAD1, [], 'owner_approval', '', '', 900, 'none', T0)

    def test_grace_is_capped_at_fifteen_minutes(self):
        self.assertEqual(self.rec(7, grace_s=99999)[0]['grace_s'], 900)

    def test_duplicate_record_is_a_noop_and_cannot_reopen_a_consumed_item(self):
        first, _ = self.rec(7)
        self.set_gate({7: HEAD1})
        self.resume(first['id'], HEAD1, T0)
        self.finish(first['id'], 'executed', 'verify:VERIFIED')
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
        self.finish(a['id'], 'executed', 'verify:VERIFIED')
        with self.assertRaises(ap.AskError):  # duplicate finish after consumption
            self.finish(a['id'], 'executed', 'verify:VERIFIED')
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
        with self.assertRaisesRegex(ap.AskError, 'could not run'):
            self.resume(a['id'], HEAD1, T0)

    def test_denial_is_terminal_and_never_retried_by_the_ledger(self):
        a, _ = self.rec(7)
        self.set_gate({7: HEAD1})
        self.resume(a['id'], HEAD1, T0)
        self.assertEqual(self.finish(a['id'], 'denied', 'host denied')['state'], 'DENIED')
        with self.assertRaises(ap.AskError):
            self.resume(a['id'], HEAD1, T0)

    def test_expired_prompt_is_terminal(self):
        a, _ = self.rec(7, prompt='live')
        self.set_gate({7: HEAD1})
        self.resume(a['id'], HEAD1, T0)
        self.assertEqual(self.finish(a['id'], 'expired', 'prompt expired')['state'], 'EXPIRED')

    def test_executed_needs_durable_proof(self):
        a, _ = self.rec(7)
        self.set_gate({7: HEAD1})
        self.resume(a['id'], HEAD1, T0)
        with self.assertRaisesRegex(ap.AskError, 'needs --ref'):
            self.finish(a['id'], 'executed', '')

    def test_failed_returns_to_pending_with_prompt_unknown(self):
        a, _ = self.rec(7)
        self.set_gate({7: HEAD1})
        self.resume(a['id'], HEAD1, T0)
        item = self.finish(a['id'], 'failed', '')
        self.assertEqual((item['state'], item['prompt']), ('PENDING', 'unknown'))

    def test_finish_without_resume_is_refused(self):
        a, _ = self.rec(7)
        with self.assertRaisesRegex(ap.AskError, 'only a RESUMING item'):
            self.finish(a['id'], 'executed', 'x')

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
        self.finish(a['id'], 'executed', 'verify:VERIFIED')
        events = [json.loads(l)['event'] for l in Path(self.dir, 'receipts.jsonl').read_text().splitlines()]
        self.assertEqual(events, ['recorded', 'resuming', 'finished'])


class ReviewHardening(Env):
    def test_list_is_read_only_even_with_a_lapsed_lease(self):
        a, _ = self.rec(7)
        self.set_gate({7: HEAD1})
        self.resume(a['id'], HEAD1, T0)
        path = os.path.join(self.dir, a['id'] + '.json')
        before = Path(path).read_bytes()
        later = T0 + timedelta(seconds=ap.LEASE_S + 5)
        shown = ap.view(ap.recovered_view(ap.load(self.dir, a['id']), later), later)
        self.assertEqual((shown['state'], shown['prompt']), ('PENDING', 'unknown'))
        self.assertEqual(Path(path).read_bytes(), before)  # nothing written: a concurrent resume cannot be overwritten
        self.assertEqual(self.state(a['id']), 'RESUMING')

    def test_malformed_current_head_changes_nothing(self):
        a, _ = self.rec(7)
        for bad in ('abc1234', '', 'Z' * 40):
            with self.assertRaisesRegex(ap.AskError, '40-hex'):
                self.resume(a['id'], bad, T0)
        self.assertEqual(self.state(a['id']), 'PENDING')

    def test_gate_that_cannot_run_is_a_refusal_not_a_traceback(self):
        a, _ = self.rec(7)
        bad = ap.gate_validator('/dev/null', gate_cmd=['/nonexistent/gate'])
        with self.assertRaisesRegex(ap.AskError, 'could not run'):
            ap.begin_resume(self.dir, a['id'], HEAD1, bad, T0)
        self.assertEqual(self.state(a['id']), 'PENDING')

    def test_missing_gate_head_is_refused(self):
        a, _ = self.rec(7)
        Path(self.scn).write_text(json.dumps({'7': {'out': {'verdict': 'PASS', 'merge_command': a['action']}}}))
        with self.assertRaisesRegex(ap.AskError, 'head differs'):
            self.resume(a['id'], HEAD1, T0)

    def test_recorded_gate_options_and_every_settings_layer_are_replayed(self):
        argv_log = os.path.join(self.tmp.name, 'argv.json')
        Path(self.gate).write_text(
            'import json,sys\nopen(%r,"w").write(json.dumps(sys.argv[1:]))\n'
            'print(json.dumps({"verdict":"PASS","head":%r,"merge_command":%r}))' % (argv_log, HEAD1, MERGE.format(pr=7, head=HEAD1)))
        item, _ = ap.record(self.dir, MERGE.format(pr=7, head=HEAD1), 'o/r#7', HEAD1, [], 'owner_approval', 's', '', 900, 'none', T0,
                            'Chisanan232', ['--ci-exception', '--version-policy', '/p.json'])
        validate = ap.gate_validator(['/u/settings.json', '/p/settings.json'], gate_cmd=[sys.executable, self.gate])
        self.assertEqual(validate(item), [])
        argv = json.loads(Path(argv_log).read_text())
        self.assertEqual(argv.count('--settings'), 2)
        self.assertIn('--ci-exception', argv)
        self.assertEqual(argv[argv.index('--owners') + 1], 'Chisanan232')

    def test_executed_is_accepted_only_when_verification_passes(self):
        a, _ = self.rec(7)
        self.set_gate({7: HEAD1})
        self.resume(a['id'], HEAD1, T0)
        with self.assertRaisesRegex(ap.AskError, 'not accepted'):
            self.finish(a['id'], 'executed', 'ref', problems=['verify verdict MISMATCH'])
        self.assertEqual(self.state(a['id']), 'RESUMING')
        self.assertEqual(self.finish(a['id'], 'executed', 'ref')['state'], 'CONSUMED')

    def test_merge_verifier_requires_the_gate_to_say_verified(self):
        a, _ = self.rec(7)
        Path(self.gate).write_text('import json\nprint(json.dumps({"verdict":"VERIFIED"}))')
        self.assertEqual(ap.merge_verifier([sys.executable, self.gate])(a), [])
        Path(self.gate).write_text('import json,sys\nprint(json.dumps({"verdict":"MISMATCH"}))\nsys.exit(1)')
        self.assertTrue(ap.merge_verifier([sys.executable, self.gate])(a))

    def test_unreadable_item_does_not_break_the_ledger(self):
        a, _ = self.rec(7)
        Path(self.dir, 'deadbeefdeadbeef.json').write_text('{not json')
        self.assertEqual([i['id'] for i in ap.all_items(self.dir)], [a['id']])
        self.rec(8)

    def test_bad_ids_are_refused(self):
        for bad in ('../x', 'ABCDEF0123456789', 'abc', 'é' * 16):
            with self.assertRaises(ap.AskError):
                ap.load(self.dir, bad)

    def test_negative_grace_is_clamped(self):
        self.assertEqual(self.rec(7, grace_s=-5)[0]['grace_s'], 0)

    def test_runner_action_is_pinned_the_same_way(self):
        runner = f'/h/.claude/bin/owner-merge-runner --repo o/r --pr 7 --expect-head {HEAD1} --review-evidence /h/e.json'
        item, _ = ap.record(self.dir, runner, 'o/r#7', HEAD1, [], 'owner_approval', 's', '', 900, 'none', T0)
        Path(self.scn).write_text(json.dumps({'7': {'out': {'verdict': 'PASS', 'head': HEAD1, 'merge_command': runner}}}))
        self.assertEqual(self.resume(item['id'], HEAD1, T0)['action'], runner)

    def test_direct_item_is_refused_once_the_gate_prefers_the_runner_and_can_be_re_recorded(self):
        a, _ = self.rec(7)
        runner = f'/h/.claude/bin/owner-merge-runner --repo o/r --pr 7 --expect-head {HEAD1} --review-evidence /h/e.json'
        Path(self.scn).write_text(json.dumps({'7': {'out': {'verdict': 'PASS', 'head': HEAD1, 'merge_command': runner}}}))
        with self.assertRaisesRegex(ap.AskError, 'differs from the recorded action'):
            self.resume(a['id'], HEAD1, T0)
        ap.cancel(self.dir, a['id'], 'executor changed', T0)
        again, created = ap.record(self.dir, runner, 'o/r#7', HEAD1, [], 'owner_approval', 's', '', 900, 'none', T0)
        self.assertTrue(created)


class RealGateContract(Env):
    """The validator must understand what the real owner-merge-gate.py check prints, not only a stand-in."""

    def test_validator_accepts_the_real_gate_output_shape(self):
        gspec = spec_from_file_location('omg', ROOT / 'scripts/owner-merge-gate.py')
        omg = module_from_spec(gspec)
        gspec.loader.exec_module(omg)
        facts = {'repo': 'o/r', 'pr': 7, 'head': HEAD1, 'base': 'main', 'viewer': 'o'}
        settings = os.path.join(self.tmp.name, 'settings.json')
        Path(settings).write_text('{}')
        omg.evaluate = lambda *a, **k: ('PASS', [], facts)
        omg.read_version_policy = lambda *a, **k: None
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            omg.main(['check', '--repo', 'o/r', '--pr', '7', '--owners', 'o', '--settings', settings], run=lambda *a: '')
        printed = json.loads(out.getvalue())
        self.assertEqual((printed['verdict'], printed['head']), ('PASS', HEAD1))
        self.assertEqual(printed['merge_command'], MERGE.format(pr=7, head=HEAD1))  # the exact string the ledger records
        Path(self.scn).write_text(json.dumps({'7': {'out': printed}}))
        item, _ = self.rec(7)
        self.assertEqual(self.validator()(item), [])


class SecondReview(Env):
    def test_list_writes_nothing_even_with_an_unreadable_item(self):
        self.rec(7)
        Path(self.dir, 'deadbeefdeadbeef.json').write_text('{bad')
        before = Path(self.dir, 'receipts.jsonl').read_bytes()
        ap.all_items(self.dir, quiet=True)
        self.assertEqual(Path(self.dir, 'receipts.jsonl').read_bytes(), before)

    def test_a_stale_item_can_be_requested_again_but_a_consumed_one_cannot(self):
        a, _ = self.rec(7)
        with self.assertRaises(ap.AskError):
            self.resume(a['id'], HEAD2, T0)
        self.assertEqual(self.state(a['id']), 'STALE_HEAD')
        again, created = self.rec(7)
        self.assertTrue(created)
        self.assertEqual(again['state'], 'PENDING')
        self.set_gate({7: HEAD1})
        self.resume(a['id'], HEAD1, T0)
        self.finish(a['id'], 'executed', 'ref')
        self.assertFalse(self.rec(7)[1])

    def test_a_re_resume_after_a_lapsed_lease_closes_an_already_merged_item(self):
        a, _ = self.rec(7)
        self.set_gate({7: HEAD1})
        self.resume(a['id'], HEAD1, T0)
        later = T0 + timedelta(seconds=ap.LEASE_S + 1)
        with self.assertRaisesRegex(ap.AskError, 'already verified'):
            ap.begin_resume(self.dir, a['id'], HEAD1, self.validator(), later, lambda _item: [])
        self.assertEqual(self.state(a['id']), 'CONSUMED')

    def test_a_re_resume_when_not_merged_proceeds_normally(self):
        a, _ = self.rec(7)
        self.set_gate({7: HEAD1})
        self.resume(a['id'], HEAD1, T0)
        later = T0 + timedelta(seconds=ap.LEASE_S + 1)
        got = ap.begin_resume(self.dir, a['id'], HEAD1, self.validator(), later, lambda _item: ['verify verdict MISMATCH'])
        self.assertEqual(got['resumes'], 2)

    def test_non_object_gate_output_is_a_refusal(self):
        a, _ = self.rec(7)
        Path(self.gate).write_text('print("[1,2]")')
        with self.assertRaisesRegex(ap.AskError, 'could not run'):
            self.resume(a['id'], HEAD1, T0)

    def test_concurrent_resumes_hand_the_action_back_exactly_once(self):
        import threading
        a, _ = self.rec(7)
        self.set_gate({7: HEAD1})
        results = []

        def attempt():
            try:
                ap.begin_resume(self.dir, a['id'], HEAD1, self.validator(), T0)
                results.append('ok')
            except ap.AskError:
                results.append('refused')
        threads = [threading.Thread(target=attempt) for _ in range(6)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(sorted(results), ['ok'] + ['refused'] * 5)
        self.assertEqual(ap.load(self.dir, a['id'])['resumes'], 1)

    def test_settings_layers_are_recorded_with_the_item(self):
        item, _ = ap.record(self.dir, MERGE.format(pr=7, head=HEAD1), 'o/r#7', HEAD1, [], 'owner_approval', 's', '', 900, 'none', T0,
                            settings=['/u/s.json', '/p/s.json'])
        self.assertEqual(item['settings'], ['/u/s.json', '/p/s.json'])


class FinalHeadReview(Env):
    def test_reopen_refuses_different_gate_options(self):
        ap.record(self.dir, MERGE.format(pr=7, head=HEAD1), 'o/r#7', HEAD1, [], 'owner_approval', 's', '', 900, 'none', T0,
                  'o', ['--ci-exception'], ['/u/s.json'])
        with self.assertRaises(ap.AskError):
            self.resume(item_key_for(7), HEAD2, T0)
        with self.assertRaisesRegex(ap.AskError, 'different gate options'):
            ap.record(self.dir, MERGE.format(pr=7, head=HEAD1), 'o/r#7', HEAD1, [], 'owner_approval', 's', '', 900, 'none', T0,
                      'o', [], ['/u/s.json'])
        again, created = ap.record(self.dir, MERGE.format(pr=7, head=HEAD1), 'o/r#7', HEAD1, [], 'owner_approval', 's', '', 900, 'none', T0,
                                   'o', ['--ci-exception'], ['/u/s.json'])
        self.assertTrue(created)

    def test_reopen_keeps_the_hand_back_count(self):
        a, _ = self.rec(7)
        self.set_gate({7: HEAD1})
        self.resume(a['id'], HEAD1, T0)
        later = T0 + timedelta(seconds=ap.LEASE_S + 1)
        with self.assertRaises(ap.AskError):
            ap.begin_resume(self.dir, a['id'], HEAD2, self.validator(), later, lambda _i: ['x'])
        self.assertEqual(self.rec(7)[0]['resumes'], 1)

    def test_even_the_first_resume_closes_a_merge_that_already_happened(self):
        a, _ = self.rec(7)
        self.set_gate({7: HEAD1})
        with self.assertRaisesRegex(ap.AskError, 'already verified'):
            ap.begin_resume(self.dir, a['id'], HEAD1, self.validator(), T0, lambda _i: [])
        self.assertEqual(self.state(a['id']), 'CONSUMED')

    def test_lease_recovery_is_receipted_with_the_open_prompt_warning(self):
        a, _ = self.rec(7)
        self.set_gate({7: HEAD1})
        self.resume(a['id'], HEAD1, T0)
        ap.begin_resume(self.dir, a['id'], HEAD1, self.validator(), T0 + timedelta(seconds=ap.LEASE_S + 1), lambda _i: ['not merged'])
        lines = [json.loads(l) for l in Path(self.dir, 'receipts.jsonl').read_text().splitlines()]
        lapse = [l for l in lines if l['event'] == 'lease_expired'][0]
        self.assertTrue(lapse['earlier_handback_may_still_be_open'])


def item_key_for(pr, head=HEAD1):
    return ap.item_key(MERGE.format(pr=pr, head=head), f'o/r#{pr}', head)


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
        self.finish(second['id'], 'executed', 'verify:VERIFIED', t)

        # The first request is then answered with a denial; a duplicate of the earlier event changes nothing.
        self.resume(first['id'], HEAD1, t)
        self.finish(first['id'], 'denied', 'owner denied', t)
        with self.assertRaises(ap.AskError):
            self.finish(second['id'], 'executed', 'verify:VERIFIED', t)
        self.assertEqual((self.state(first['id']), self.state(second['id'])), ('DENIED', 'CONSUMED'))

    def test_merge_gate_is_still_what_authorizes_a_merge(self):
        """A recorded item the gate no longer passes cannot be handed back, whatever the ledger says."""
        a, _ = self.rec(7)
        Path(self.scn).write_text(json.dumps({'7': {'rc': 1, 'out': {'verdict': 'REFUSE', 'head': HEAD1}}}))
        with self.assertRaises(ap.AskError):
            self.resume(a['id'], HEAD1, T0 + timedelta(days=1))
        self.assertNotIn('run_exactly', json.dumps(ap.load(self.dir, a['id'])))


class NoSelfAuthorization(unittest.TestCase):
    def test_the_ledger_has_no_approve_or_execute_command(self):
        import argparse
        parser_names = set()
        real = argparse.ArgumentParser.add_subparsers

        def spy(self, *a, **k):
            sub = real(self, *a, **k)
            orig = sub.add_parser
            sub.add_parser = lambda name, *x, **y: (parser_names.add(name), orig(name, *x, **y))[1]
            return sub
        argparse.ArgumentParser.add_subparsers = spy
        try:
            with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                ap.main(['--help'])
        finally:
            argparse.ArgumentParser.add_subparsers = real
        self.assertEqual(parser_names, {'record', 'list', 'restart', 'resume', 'finish', 'cancel'})
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            ap.main(['approve', '--id', 'x'])

    def test_the_only_subprocess_is_the_gate(self):
        text = (ROOT / 'scripts/ask-pending.py').read_text()
        self.assertEqual(text.count('subprocess.run('), 1)  # _run_gate: check and verify only
        self.assertNotIn('"merge"', text)


if __name__ == '__main__':
    unittest.main()
