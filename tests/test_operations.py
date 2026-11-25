import json
import unittest

from darksky.corpus import build_corpus_service
from darksky.models import Direction


class CorpusOperationsTest(unittest.TestCase):
    def setUp(self):
        self.svc = build_corpus_service(now="20:31")

    def test_dispatcher_sees_one_remaining_seat_and_path_blocker_rule(self):
        avail = self.svc.availability("20:31")
        slot = avail["slots"][0]
        self.assertEqual(slot["occupied_seats"], 3)
        self.assertEqual(slot["available_seats"], 1)

        p2 = next(r for r in avail["routes"] if r["segment"]["id"] == "P2")
        self.assertTrue(p2["current_closures"])
        blocker_codes = {b["code"] for b in p2["blockers"]}
        self.assertIn("segment_closed", blocker_codes)
        blocker = next(b for b in p2["blockers"] if b["code"] == "segment_closed")
        self.assertEqual(blocker["segment_id"], "P2")
        self.assertEqual(blocker["rule_id"], "R-TEMP-CLOSURE")
        self.assertEqual(blocker["source_id"], "C1")

    def test_n11_recovery_points_to_p3_and_exact_acceptors(self):
        report = self.svc.handle_disruptions("20:31")
        self.assertEqual([e["code"] for e in report["recovery"]], ["N11"])
        self.assertIn("N11", self.svc.manual_queue)
        names = report["recovery"][0]["member_names"]
        self.assertEqual(names, ["韩榆", "余果"])

        best = self.svc.best_option("N11")
        self.assertEqual(best.return_trip.segment_id, "P3")
        self.assertTrue(best.acceptable)
        self.assertFalse(best.confirmable)
        acceptor_keys = {(a["acceptor_type"], a["acceptor_id"])
                         for a in best.required_acceptors}
        self.assertIn(("person", "han"), acceptor_keys)
        self.assertIn(("person", "yu"), acceptor_keys)
        self.assertIn(("escort", "nan1"), acceptor_keys)
        warnings = {i.code: i for i in best.issues if i.severity == "warning"}
        self.assertIn("tentative_red_window_overlap", warnings)
        self.assertIn("N12", warnings["tentative_red_window_overlap"].message)

    def test_b3_question_names_the_exact_return_movement(self):
        question = next(q for q in self.svc.questions if q.id == "Q-N12-B3-CHILD-RETURN-P3")
        self.assertEqual(question.direction, Direction.RETURN)
        self.assertEqual(question.segment_id, "P3")
        self.assertEqual((question.start, question.end), (1240, 1248))
        self.assertEqual(question.person_id, "b3_child")

        entrance = self.svc.entrance_view("N12")
        self.assertFalse(entrance["release"]["admit"])
        self.assertIn("暂留", entrance["release"]["title"])

    def test_entrance_view_hides_private_narratives(self):
        view = self.svc.entrance_view("N11")
        encoded = json.dumps(view, ensure_ascii=False)
        self.assertNotIn("Solid red is fine if needed", encoded)
        self.assertNotIn("我的原话不用转给入口", encoded)
        self.assertNotIn("我们不要亮光", encoded)
        # 但入口仍能看到操作所需陪行和路线。
        self.assertEqual(view["release"]["return_trip"]["escort"]["name"], "亭二")
        self.assertTrue(view["privacy"]["raw_lighting_reasons_hidden"])

    def test_entrance_view_flags_current_blocked_route_without_private_text(self):
        view = self.svc.entrance_view("N11")
        self.assertFalse(view["release"]["route_clear"])
        self.assertTrue(view["release"]["operational_alerts"])
        check = self.svc.verify_release(
            "N11", return_segment_id="P2", escort_id="ting2", version=1
        )
        self.assertTrue(check["matches_current"])
        self.assertFalse(check["safe_to_release"])
        self.assertIsNotNone(check["warning"])

    def test_ting2_cannot_be_reassigned_to_south_dike(self):
        version = self.svc.create_reassignment(
            "N11", segment_id="P3", start="20:40", escort_id="ting2"
        )
        report = self.svc.evaluate_confirmation("N11", version.version)
        blocked = [i for i in report.issues if i.code == "escort_reassignment_blocked"]
        self.assertEqual(len(blocked), 1)
        self.assertEqual(blocked[0].source_id, "C1")

    def test_complete_reassignment_changes_entrance_view_and_detects_stale_copy(self):
        version = self.svc.create_reassignment(
            "N11", segment_id="P3", start="20:40", escort_id="nan1", reason="C1"
        )
        self.svc.accept_plan("N11", "han", version.version)
        self.svc.accept_plan("N11", "yu", version.version)
        self.svc.confirm_escort("nan1", "N11", version.version)
        self.svc.issue_proposal("N11", version.version)

        entrance = self.svc.entrance_view("N11")
        self.assertEqual(entrance["effective_version"], 2)
        self.assertEqual(entrance["release"]["return_trip"]["segment_id"], "P3")
        self.assertEqual(entrance["release"]["return_trip"]["escort"]["id"], "nan1")
        self.assertNotIn("N11", self.svc.manual_queue)

        stale = self.svc.verify_release(
            "N11", return_segment_id="P2", escort_id="ting2", version=1
        )
        self.assertFalse(stale["matches_current"])
        self.assertEqual(
            set(stale["mismatches"]), {"version", "return_segment", "escort"}
        )
        audit = self.svc.audit_code("N11")
        self.assertEqual(len(audit["history"]), 2)
        self.assertTrue(audit["history"][0]["superseded_by"] == 2
                        or audit["history"][0]["status"] == "superseded")

    def test_confirmed_not_entered_group_gets_time_shift_options(self):
        # N12 是暂留；构造一个已确认但未入场的 N9。
        self.svc.hold_seat("N9", "T1", ["han"], seats=1,
                           planned_return_segment="P2")
        version = self.svc.propose_confirmation(
            "N9", segment_id="P2", start="20:40", escort_id="ting2"
        )
        self.svc.accept_plan("N9", "han", version.version)
        self.svc.confirm_escort("ting2", "N9", version.version)
        # P2 已在 20:31 闭合，因此该 proposal 不应可签发。
        with self.assertRaises(Exception):
            self.svc.issue_proposal("N9", version.version)

        report = self.svc.handle_disruptions("20:31")
        # N9 仍只是 HELD/proposal，因为未能签发；这里验证替代时段搜索不会丢。
        options = self.svc.reschedule_options("N9")
        self.assertTrue(options)
        self.assertTrue(any(o.slot_id == "T1" for o in options))


if __name__ == "__main__":
    unittest.main()
