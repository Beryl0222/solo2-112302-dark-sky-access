import unittest

from darksky.models import (
    BookingStatus,
    Direction,
    EquipmentCondition,
    Itinerary,
    LightPolicyWindow,
    LightUse,
    LightingNeed,
    ObservationSlot,
    Segment,
    Escort,
    DutyError,
)
from darksky.service import DutyService


def build_small_service() -> DutyService:
    svc = DutyService(now="20:31")
    svc.add_person("a", "阿甲")
    svc.add_person("b", "阿乙")
    svc.add_slot(ObservationSlot(
        id="S1", station="台", start=20 * 60 + 20, end=20 * 60 + 40, seats=4,
        equipment=[EquipmentCondition(id="eyepiece", label="目镜可调")],
    ))
    svc.add_rule(
        "R-OBS-DARK", "看星绝对禁光", "absolute_dark",
        segment_id="S1", start=20 * 60 + 20, end=20 * 60 + 40,
    )
    svc.add_segment(Segment(
        id="OUT", name="去程坡道", direction=Direction.OUTBOUND, walk_minutes=5,
        windows=[LightPolicyWindow("R-OUT-DARK", 20 * 60 + 10, 21 * 60 + 15, "dark", 100)],
    ))
    svc.add_segment(Segment(
        id="RED", name="红光堤道", direction=Direction.RETURN, walk_minutes=8,
        windows=[LightPolicyWindow(
            "R-RED", 20 * 60 + 38, 20 * 60 + 50, "steady_red", 1, "low", True
        )],
        requires_escort=True,
    ))
    svc.add_escort(Escort(
        id="e1", name="陪行一", segment_id="RED",
        start=20 * 60 + 38, end=20 * 60 + 52, capacity=1,
    ))
    return svc


class SchedulingRuleTest(unittest.TestCase):
    def test_several_instant_lights_do_not_fill_continuous_need(self):
        svc = build_small_service()
        svc.add_need(LightingNeed(
            person_id="a", direction=Direction.RETURN, segment_id="RED",
            kind="continuous_light", modality="requires", continuous=True, color="red",
        ))
        svc.hold_seat("G1", "S1", ["a"], seats=1)
        broken = Itinerary(
            Direction.RETURN, "RED", 1240, 1248,
            light_uses=[
                LightUse(1240, 1241, "steady_red", "low"),
                LightUse(1244, 1245, "steady_red", "low"),
            ],
        )
        issues = svc.evaluate_itinerary_rules(broken, "G1")
        codes = {i.code for i in issues}
        self.assertIn("continuity_not_met", codes)

        person_issues = svc.evaluate_person_needs(broken, "G1", ["a"])
        self.assertIn("person_continuous_red_required", {i.code for i in person_issues})

        continuous = Itinerary(
            Direction.RETURN, "RED", 1240, 1248,
            light_uses=[LightUse(1240, 1248, "steady_red", "low")],
        )
        self.assertFalse(any(i.code == "continuity_not_met"
                             for i in svc.evaluate_itinerary_rules(continuous, "G1")))

    def test_two_groups_cannot_occupy_same_red_window(self):
        svc = build_small_service()
        trip1 = Itinerary(
            Direction.RETURN, "RED", 1240, 1248,
            light_uses=[LightUse(1240, 1248, "steady_red", "low")], escort_id="e1",
        )
        trip2 = Itinerary(
            Direction.RETURN, "RED", 1241, 1249,
            light_uses=[LightUse(1241, 1249, "steady_red", "low")], escort_id="e1",
        )
        svc.admit_code("G1", slot_id="S1", member_ids=["a"], return_trip=trip1)
        svc.admit_code("G2", slot_id="S1", member_ids=["b"], return_trip=trip2)

        issues = svc.evaluate_itinerary_rules(trip2, "G2")
        conflict = [i for i in issues if i.code == "red_window_conflict"]
        self.assertEqual(len(conflict), 1)
        self.assertEqual(conflict[0].rule_id, "R-P4-WINDOW-CAPACITY")
        self.assertEqual(conflict[0].alternative["window_rule_id"], "R-RED")
        self.assertIn("G1", conflict[0].message)
        self.assertTrue(any(i.code == "escort_conflict" for i in issues))

    def test_hold_occupies_seat_but_is_not_confirmation(self):
        svc = build_small_service()
        svc.hold_seat("H1", "S1", ["a"], seats=1)
        self.assertEqual(svc.slot_occupancy("S1"), 1)
        report = svc.evaluate_confirmation("H1")
        self.assertFalse(report.confirmable)
        self.assertIn("seat_hold_not_confirmation", {i.code for i in report.issues})

    def test_confirmation_requires_person_route_equipment_and_escort_acceptance(self):
        svc = build_small_service()
        svc.hold_seat("C1", "S1", ["a"], seats=1, planned_return_segment="RED")
        version = svc.propose_confirmation(
            "C1", segment_id="RED", start="20:40", escort_id="e1"
        )
        report = svc.evaluate_confirmation("C1", version.version)
        codes = {(i.code, i.acceptor_id) for i in report.issues}
        self.assertIn(("missing_route_acceptance", "a"), codes)
        self.assertIn(("missing_equipment_acceptance", "a"), codes)
        self.assertIn(("missing_escort_confirmation", "e1"), codes)

        with self.assertRaises(DutyError):
            svc.issue_proposal("C1", version.version)

        svc.accept_plan("C1", "a", version.version)
        svc.confirm_escort("e1", "C1", version.version)
        report = svc.evaluate_confirmation("C1", version.version)
        self.assertTrue(report.confirmable, [i.to_dict() for i in report.issues])
        svc.issue_proposal("C1", version.version)
        self.assertEqual(svc.bookings["C1"].status, BookingStatus.CONFIRMED)

    def test_confirmed_but_not_entered_group_is_offered_time_shift(self):
        svc = build_small_service()
        svc.add_slot(ObservationSlot(
            id="S2", station="晚台", start=21 * 60, end=21 * 60 + 20, seats=4,
            equipment=[EquipmentCondition(id="eyepiece", label="目镜可调")],
        ))
        svc.add_segment(Segment(
            id="LATE", name="晚间堤道", direction=Direction.RETURN, walk_minutes=8,
            windows=[LightPolicyWindow(
                "R-LATE-RED", 21 * 60 + 18, 21 * 60 + 50,
                "steady_red", 1, "low", True
            )],
            requires_escort=True,
        ))
        svc.add_escort(Escort(
            id="e2", name="陪行二", segment_id="LATE",
            start=21 * 60 + 18, end=21 * 60 + 52,
        ))
        svc.hold_seat("K1", "S2", ["a"], seats=1, planned_return_segment="LATE")
        version = svc.propose_confirmation(
            "K1", segment_id="LATE", start="21:20", escort_id="e2"
        )
        svc.accept_plan("K1", "a", version.version)
        svc.confirm_escort("e2", "K1", version.version)
        svc.issue_proposal("K1", version.version)
        self.assertEqual(svc.bookings["K1"].status, BookingStatus.CONFIRMED)

        svc.report_closure("LATE", start="21:15", reason="栈道临时闭合")
        disruption = svc.handle_disruptions("21:15")
        entry = next(e for e in disruption["reschedule_confirmed_not_entered"]
                     if e["code"] == "K1")
        self.assertTrue(entry["may_shift_time"])
        self.assertTrue(entry["time_options"])

    def test_observation_period_has_absolute_light_ban(self):
        svc = build_small_service()
        issues = svc.evaluate_observation_rules(
            "S1", [LightUse(1225, 1226, "white", "bright")]
        )
        self.assertEqual([i.code for i in issues], ["absolute_light_ban"])

    def test_on_call_assistant_does_not_consent_to_removing_continuous_light(self):
        svc = build_small_service()
        svc.add_need(LightingNeed(
            person_id="a", direction=Direction.RETURN, segment_id="RED",
            kind="continuous_light", modality="requires", continuous=True,
        ))
        svc.add_need(LightingNeed(
            person_id="a", direction=Direction.RETURN, segment_id="RED",
            kind="alternative_resource", modality="offers",
            alternative="on_call_assistant",
            consent_to_remove_continuous_light=False,
        ))
        svc.hold_seat("A1", "S1", ["a"])
        trip = Itinerary(
            Direction.RETURN, "RED", 1240, 1248,
            measures={"continuous_light_replaced_by": "on_call_assistant"},
        )
        issues = svc.evaluate_person_needs(trip, "A1", ["a"])
        self.assertIn("assistant_not_light_consent", {i.code for i in issues})

        svc.add_acceptance(
            "a", kind="lighting",
            terms={"remove_continuous_light": True},
        )
        issues = svc.evaluate_person_needs(trip, "A1", ["a"])
        self.assertNotIn("assistant_not_light_consent", {i.code for i in issues})


if __name__ == "__main__":
    unittest.main()
