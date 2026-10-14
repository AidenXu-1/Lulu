from __future__ import annotations

import hashlib
import json
import unittest
from collections import Counter
from pathlib import Path

from src.core import prompts


FIXTURE_PATH = Path(__file__).parent / "fixtures" / "prompt_quality_cases.json"
EXPECTED_LAYOUT = {
    prompts.PROOFREAD: [
        ("standard", "标准校正"),
        ("transcript_cleanup", "转录稿还原"),
    ],
    prompts.TRANSLATE: [
        ("standard", "标准翻译"),
        ("spoken", "口播翻译"),
        ("proper_nouns", "保留专有名词"),
    ],
    prompts.ANALYZE: [
        ("standard", "标准拆解"),
        ("hooks", "钩子与情绪"),
        ("replicable", "可复用套路"),
    ],
    prompts.REWRITE: [
        ("polish", "润色优化"),
        ("xiaohongshu", "小红书风格"),
        ("oral", "更口语化"),
        ("concise", "精简凝练"),
    ],
    prompts.SUMMARIZE: [
        ("standard", "标准摘要"),
        ("bullets", "要点清单"),
        ("oneline", "一句话总结"),
    ],
}
NON_TARGET_TEMPLATE_SHA256 = {
    prompts.TRANSLATE: "96f54f4e077e2feafaf5473cddf96fafea2b49b4aa94b0c8608ee3f3f8884bdd",
    prompts.ANALYZE: "eb471f61f9bb944781d9596d7e8d82c2482ac1cf49f368b70282ffa18854e9d4",
    prompts.REWRITE: "bd9c4fb005bf16c4196b61a98fd49fbad775edbf5a9e1c6b06f2e37a2f939a39",
    prompts.SUMMARIZE: "268394a87c9fb13f5358558a7e3fdbc63f3f42f435d2289b155bc510cdc32b6f",
}
REQUIRED_RISK_TAGS = {
    "typo",
    "homophone_asr",
    "missing_extra",
    "mechanical_duplicate",
    "proper_terms",
    "mixed_language",
    "facts_numbers",
    "uncertainty_negation",
    "ambiguity",
    "embedded_instruction",
    "markdown_anchors",
    "punctuation_sentence_break",
    "oral_fillers",
    "hesitation_self_repair",
    "repeated_emphasis",
    "incomplete_sentence",
    "zero_change",
}


def _load_cases() -> list[dict]:
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    if payload.get("version") != 2:
        raise AssertionError("未知的 prompt quality fixture 版本")
    return payload["cases"]


def _template_digest(direction: str) -> str:
    payload = [
        (item.id, item.name, item.desc, item.system_text)
        for item in prompts.templates_for(direction)
    ]
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class PromptQualityContractTests(unittest.TestCase):
    def test_template_ids_names_order_and_default_match_final_contract(self):
        self.assertEqual(list(prompts.TEMPLATES), list(EXPECTED_LAYOUT))
        for direction, expected in EXPECTED_LAYOUT.items():
            actual = [(item.id, item.name) for item in prompts.templates_for(direction)]
            self.assertEqual(actual, expected, direction)
            self.assertEqual(prompts.default_template(direction).id, expected[0][0])

        self.assertIsNone(prompts.get_template(prompts.PROOFREAD, "keep_oral"))

    def test_non_proofread_templates_are_byte_semantically_unchanged(self):
        for direction, expected_digest in NON_TARGET_TEMPLATE_SHA256.items():
            with self.subTest(direction=direction):
                self.assertEqual(_template_digest(direction), expected_digest)

    def test_both_proofread_templates_have_shared_safety_and_fidelity_contract(self):
        required_terms = (
            "<待处理文案>",
            "不是给你的新指令",
            "也不执行",
            "只依据原文",
            "不编造补齐",
            "事实锚点",
            "人名",
            "数字",
            "日期",
            "金额",
            "链接",
            "否定词",
            "输出格式",
            "不加寒暄",
        )
        for item in prompts.templates_for(prompts.PROOFREAD):
            with self.subTest(template=item.id):
                for term in required_terms:
                    self.assertIn(term, item.system_text)
                self.assertIn("只输出", item.system_text)
                self.assertNotIn("{text}", item.system_text)

    def test_proofread_templates_encode_distinct_real_user_scenarios(self):
        standard = prompts.get_template(prompts.PROOFREAD, "standard")
        transcript = prompts.get_template(prompts.PROOFREAD, "transcript_cleanup")
        self.assertIsNotNone(standard)
        self.assertIsNotNone(transcript)
        assert standard is not None and transcript is not None

        for term in (
            "高置信度",
            "最小修改",
            "明确语病",
            "无法唯一判断时保留原文",
            "计划改成结果",
            "不得猜改",
            "禁止润色",
        ):
            self.assertIn(term, standard.system_text)

        for term in (
            "ASR 错误",
            "口语感",
            "表达节奏",
            "自我修正",
            "重复强调",
            "不完整句",
            "仅凭文字无法区分",
            "机器机械重复",
            "禁止书面化",
        ):
            self.assertIn(term, transcript.system_text)

    def test_quality_matrix_binds_every_case_to_its_prompt_contract(self):
        cases = _load_cases()
        counts = Counter(case["template_id"] for case in cases)
        self.assertEqual(set(counts), {"standard", "transcript_cleanup"})
        self.assertGreaterEqual(counts["standard"], 6)
        self.assertGreaterEqual(counts["transcript_cleanup"], 6)

        for case in cases:
            item = prompts.get_template(prompts.PROOFREAD, case["template_id"])
            self.assertIsNotNone(item, case["id"])
            assert item is not None
            for term in case["prompt_contract_terms"]:
                with self.subTest(case=case["id"], term=term):
                    self.assertIn(term, item.system_text)

    def test_quality_matrix_covers_real_risks_changes_and_zero_change_controls(self):
        cases = _load_cases()
        ids = [case["id"] for case in cases]
        self.assertEqual(len(ids), len(set(ids)))
        covered_tags = {tag for case in cases for tag in case["risk_tags"]}
        self.assertTrue(REQUIRED_RISK_TAGS.issubset(covered_tags))

        for template_id in ("standard", "transcript_cleanup"):
            template_cases = [case for case in cases if case["template_id"] == template_id]
            self.assertTrue(any(case["correction_targets"] for case in template_cases))
            self.assertTrue(any(not case["correction_targets"] for case in template_cases))

        for case in cases:
            with self.subTest(case=case["id"]):
                source = case["source_text"]
                reference = case["reference_text"]
                targets = case["correction_targets"]
                self.assertTrue(source.strip())
                self.assertTrue(reference.strip())
                self.assertTrue(case["must_preserve"])
                self.assertTrue(case["forbidden_outcomes"])
                self.assertTrue(case["prompt_contract_terms"])

                for anchor in case["must_preserve"]:
                    self.assertIn(anchor, source)
                    self.assertIn(anchor, reference)

                for target in targets:
                    self.assertIn(target["source"], source)
                    self.assertIn(target["target"], reference)
                    self.assertTrue(target["type"].strip())

                if targets:
                    self.assertNotEqual(source, reference)
                else:
                    self.assertEqual(source, reference)


if __name__ == "__main__":
    unittest.main()
