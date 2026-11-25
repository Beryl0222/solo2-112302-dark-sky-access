import unittest

from darksky.models import Direction
from darksky.parsing import parse_statement


class ParserTest(unittest.TestCase):
    def test_chinese_sentence_is_split_by_direction_and_segment(self):
        result = parse_statement(
            "韩榆：去程进亭从坡上走我可以关灯，回来过堤时须一路有红灯照着。"
            "往返都能有人陪，但不接受断续开灯。",
            source_id="B1",
        )
        outbound = [n for n in result.needs if n.direction == Direction.OUTBOUND]
        returning = [n for n in result.needs if n.direction == Direction.RETURN]

        self.assertEqual(len(outbound), 1)
        self.assertEqual(outbound[0].segment_id, "P1")
        self.assertEqual(outbound[0].kind, "light_off_commitment")
        self.assertFalse(outbound[0].requires_light or False)

        self.assertTrue(returning)
        red = next(n for n in returning if n.kind == "continuous_light")
        self.assertEqual(red.segment_id, "P3")
        self.assertTrue(red.continuous)
        self.assertEqual(red.color, "red")
        self.assertEqual(red.modality, "requires")

        intermittent = next(n for n in result.needs if n.kind == "reject_intermittent_light")
        self.assertFalse(intermittent.intermittent_allowed)

    def test_short_english_distinguishes_allowed_steady_red_from_flashing(self):
        result = parse_statement("Solid red is fine if needed, no flashing on the path.")
        red = next(n for n in result.needs if n.kind == "steady_red")
        flash = next(n for n in result.needs if n.kind == "avoid_flashing")
        self.assertEqual(red.modality, "accepts")
        self.assertEqual(red.color, "red")
        self.assertFalse(red.continuous)
        self.assertFalse(flash.flashing_allowed)

    def test_proxy_no_light_is_not_same_as_child_self_confirmation(self):
        text = (
            "家长代填：我们不要亮光。子女随后写：我说的是走路避闪，"
            "不是不要固定红灯，等我确定是否参加。"
        )
        result = parse_statement(text)
        proxy = [n for n in result.needs if n.proxy and n.kind == "no_bright_light"]
        child_flash = [n for n in result.needs if not n.proxy and n.kind == "avoid_flashing"]
        child_red = [n for n in result.needs if not n.proxy and n.kind == "steady_red"]

        self.assertEqual(len(proxy), 1)
        self.assertFalse(proxy[0].self_reported)
        self.assertTrue(child_flash)
        self.assertFalse(child_flash[0].flashing_allowed)
        self.assertTrue(child_red)
        self.assertEqual(child_red[0].modality, "accepts")

    def test_on_call_assistant_is_not_consent_to_remove_steady_light(self):
        result = parse_statement("随时可叫助手。")
        resource = next(n for n in result.needs if n.alternative == "on_call_assistant")
        self.assertFalse(resource.consent_to_remove_continuous_light)

    def test_except_return_path_only_narrows_light_off_promise(self):
        result = parse_statement(
            "I will keep my light off for the whole observation except on the return path."
        )
        off = next(n for n in result.needs if n.kind == "light_off_commitment")
        exception = next(n for n in result.needs if n.kind == "scope_exception")

        self.assertEqual(off.direction, Direction.AT_STATION)
        self.assertEqual(off.except_directions, [Direction.RETURN])
        self.assertEqual(exception.direction, Direction.RETURN)
        self.assertIn("exception_modifies_light_off_only", exception.tags)
        # 不应生成“整个观测时段绝对要灯/不要灯”的简化结论。
        self.assertFalse(any(n.kind == "continuous_light" for n in result.needs))

    def test_bare_except_fragment_is_scoped_to_return(self):
        result = parse_statement("except on the return path")
        exception = next(n for n in result.needs if n.kind == "scope_exception")
        self.assertEqual(exception.direction, Direction.RETURN)

    def test_conflicting_before_after_statements_yield_movement_question(self):
        from darksky.service import DutyService

        svc = DutyService()
        svc.add_person("x", "小林")
        svc.ingest_statement("我回程必须全程红灯。", person_id="x", source_id="S1")
        svc.ingest_statement(
            "算了，回程把所有灯关掉，我不要光。", person_id="x", source_id="S2"
        )
        questions = svc.reconcile_statements("x", code="X1")
        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0].direction, Direction.RETURN)
        self.assertEqual(questions[0].booking_code, "X1")
        self.assertTrue(questions[0].source_ids)


if __name__ == "__main__":
    unittest.main()
