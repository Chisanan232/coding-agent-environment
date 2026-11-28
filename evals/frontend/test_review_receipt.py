"""Deterministic completeness controls; all defective receipts are channel-pinned."""
import hashlib
import json
import unittest

from review_receipt import approved_bytes


def digest(data):
    return hashlib.sha256(data).hexdigest()


class ReleaseReviewTest(unittest.TestCase):
    def setUp(self):
        self.artifact = b"Final observations"
        self.sources = {"frames": b"reviewed frames"}
        self.index = json.dumps({"frames": digest(self.sources["frames"])}).encode()
        self.items = {"A": "Trace assessed", "B": "Observation assessed",
                      "C": "Frames assessed", "Closing": "Release conclusion assessed"}
        self.report = {"decision": "APPROVED", "file": "final/claude.md",
                       "sha256": digest(self.artifact), "claim_assessments": self.items.copy()}
        self.record = {"stage": "final", "independent": True, "reviewer": "peer",
                       "reviewer_role": "Independent reviewer", "source_index_sha256": digest(self.index),
                       "reports": {"claude": self.report}}

        self.approval = json.dumps(self.record).encode()

    def deliver(self, raw=None, **changes):
        if raw is None:
            raw = json.dumps(self.record).encode()
        args = dict(stage="final", reviewer="peer", trusted_receipts={digest(raw)},
                    source_index=self.index, sources=self.sources, reviewed_receipt=self.approval)
        args.update(changes)
        return approved_bytes("claude", self.artifact, raw, **args)

    def assert_invalid_structure(self, raw=None):
        candidate = raw if raw is not None else json.dumps(self.record).encode()
        self.assertIsNone(self.deliver(candidate, reviewed_receipt=candidate))

    def test_valid(self):
        self.assertEqual(self.deliver(), self.artifact)

    def test_missing_any_mandatory_id(self):
        for item in self.items:
            with self.subTest(item=item):
                self.report["claim_assessments"] = self.items.copy()
                del self.report["claim_assessments"][item]
                self.assert_invalid_structure()

    def test_unrelated_extra_or_replacement(self):
        for substitute in ("Closing logistics", "Closing summary", "Closing ", "closing"):
            with self.subTest(substitute=substitute):
                assessments = self.items.copy()
                assessments[substitute] = "Logistics reviewed"
                self.report["claim_assessments"] = assessments
                self.assert_invalid_structure()
                del assessments["Closing"]
                self.assert_invalid_structure()

    def test_duplicate_json_ids_rejected_before_overwrite(self):
        raw = json.dumps(self.record).encode()
        for fragment, duplicate in (
            (b'"Closing":', b'"Closing": "Ignored", "Closing":'),
            (b'"A":', b'"\\u0041": "Ignored", "A":'),
            (b'"claim_assessments":', b'"claim_assessments": {}, "claim_assessments":'),
            (b'"claude":', b'"claude": {}, "claude":'),
        ):
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, raw)
                self.assert_invalid_structure(raw.replace(fragment, duplicate))

    def test_missing_malformed_or_blank_assessment(self):
        for value in (None, True, {}, [], "", " \n", 12):
            with self.subTest(value=value):
                self.report["claim_assessments"]["Closing"] = value
                self.assert_invalid_structure()
        for container in ([], "Closing", None):
            self.report["claim_assessments"] = container
            self.assert_invalid_structure()
        self.assert_invalid_structure(b'{')

    def test_text_assigned_to_wrong_review_item(self):
        assessments = self.report["claim_assessments"]
        assessments["A"], assessments["Closing"] = assessments["Closing"], assessments["A"]
        self.assertIsNone(self.deliver())

    def test_wrong_artifact_path_hash_or_stage(self):
        for field, value in (("file", "final/codex.md"), ("file", "drafts/claude.md"),
                             ("sha256", digest(b"Changed summary"))):
            with self.subTest(field=field, value=value):
                saved = self.report[field]
                self.report[field] = value
                self.assertIsNone(self.deliver())
                self.report[field] = saved
        self.assertIsNone(self.deliver(stage="first-pass"))

    def test_coordinated_substitution_against_frozen_approval(self):
        self.artifact = b"Substituted report"
        self.report["sha256"] = digest(self.artifact)
        self.assertIsNone(self.deliver())

        self.artifact = b"Final observations"
        self.report["sha256"] = digest(self.artifact)
        self.sources = {"frames": b"substituted evidence"}
        self.index = json.dumps({"frames": digest(self.sources["frames"])}).encode()
        self.record["source_index_sha256"] = digest(self.index)
        self.assertIsNone(self.deliver())

    def test_trust_and_evidence_binding(self):
        self.assertIsNone(self.deliver(trusted_receipts=set()))
        self.assertIsNone(self.deliver(reviewer="author"))
        self.assertIsNone(self.deliver(source_index=b'{}'))
        self.assertIsNone(self.deliver(sources={"frames": b"other frames"}))
        self.assertIsNone(self.deliver(sources={}))
        self.record["independent"] = False
        self.assertIsNone(self.deliver())


if __name__ == "__main__":
    unittest.main()
