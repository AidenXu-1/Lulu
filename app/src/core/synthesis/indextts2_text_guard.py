"""Conservative text-length guardrails for IndexTTS2 synthesis."""

from __future__ import annotations

from dataclasses import dataclass

RECOMMENDED_CHAR_LIMIT = 120
RISKY_CHAR_LIMIT = 280


@dataclass(frozen=True)
class TextLengthAssessment:
    level: str
    char_count: int
    recommended_limit: int = RECOMMENDED_CHAR_LIMIT
    risky_limit: int = RISKY_CHAR_LIMIT
    label: str = ""
    hint: str = ""
    blocking_reason: str = ""

    @property
    def is_blocked(self) -> bool:
        return self.level in {"empty", "blocked"}

    @property
    def is_risky(self) -> bool:
        return self.level == "risky"


def count_text_chars(text: str) -> int:
    """Count visible non-whitespace characters, matching user-facing length hints."""

    return sum(1 for char in str(text or "") if not char.isspace())


def assess_text_length(text: str) -> TextLengthAssessment:
    count = count_text_chars(text)
    if count <= 0:
        return TextLengthAssessment(
            level="empty",
            char_count=0,
            label="未输入文字",
            hint=(
                f"Lulu 当前版本设置单次字符保护:建议 {RECOMMENDED_CHAR_LIMIT} 字以内,"
                f"超过 {RISKY_CHAR_LIMIT} 字会阻断;这不是模型官方字符限制。"
            ),
            blocking_reason="请输入要生成的文字",
        )
    if count <= RECOMMENDED_CHAR_LIMIT:
        return TextLengthAssessment(
            level="normal",
            char_count=count,
            label="长度合适",
            hint=f"{count} 字 · Lulu 当前版本的单次字符保护建议为 {RECOMMENDED_CHAR_LIMIT} 字以内,当前可直接生成。",
        )
    if count <= RISKY_CHAR_LIMIT:
        return TextLengthAssessment(
            level="risky",
            char_count=count,
            label="偏长可尝试",
            hint=(
                f"{count} 字 · 已超过 Lulu 当前版本单次字符保护建议 {RECOMMENDED_CHAR_LIMIT} 字。"
                "这是为避免未验证的长文本一次生成失败而设置,不是模型官方字符限制。"
            ),
        )
    return TextLengthAssessment(
        level="blocked",
        char_count=count,
        label="过长已阻断",
        hint=(
            f"{count} 字 · 已超过 Lulu 当前版本单次字符保护上限 {RISKY_CHAR_LIMIT} 字。"
            "当前未实现官方式自动分段、多段拼接或长文本压测,请先缩短文本。"
        ),
        blocking_reason=f"文本过长:{count} 字,请缩短到 {RISKY_CHAR_LIMIT} 字以内",
    )
