"""The frozen synthetic P1 report fixture shared by offline acceptance runs."""
from __future__ import annotations

from pathlib import Path


_FIXTURES = {
    "S01-初版公告.md": (
        "# 周末读书会初版公告\n"
        "活动日期为 11 月 29 日，初定 13:30–15:30。\n"
        "初定地点为社区活动室 A，人数上限 36 人。\n"
        "本次活动免费，报名截止为 11 月 27 日 18:00。\n"
    ),
    "S02-报名统计.txt": (
        "周末读书会报名统计，截至 11 月 26 日 18:00。\n"
        "有效确认 24 人，待确认 5 人，已取消 3 人，共 32 条报名记录。\n"
        "每位已确认读者占 1 个座位。待确认、已取消均不计入当前已占座位。\n"
    ),
    "S03-最终变更.md": (
        "# 周末读书会最终变更\n"
        "本通知替代初版公告中的时间、地点、人数上限三个字段。\n"
        "活动日期仍为 11 月 29 日，活动时间调整为 14:00–16:00。\n"
        "地点调整为社区活动室 B，人数上限调整为 30 人。\n"
        "活动费用和报名截止沿用初版公告。\n"
    ),
}
_INSTRUCTION = (
    "仅根据用户选中的 S01、S02、S03 写一份 Markdown 读书会简报。"
    "正文包括最终安排、报名情况、尚可使用席位，以及停车安排。"
    "最终变更仅覆盖它明确列出的字段。关键事实注明资料 ID 和可定位的行号或片段，最后列出资料清单。"
)


def _fact(text, *ranges):
    return {
        "text": text,
        "citations": [
            {"source_id": source, "first_line": first, "last_line": last}
            for source, first, last in ranges
        ],
    }


_WHEN = "11 月 29 日 14:00–16:00"
_WHERE = "社区活动室 B，人数上限 30 人"
_COST = "活动免费，11 月 27 日 18:00 截止报名"
_SIGNUPS = "截至 11 月 26 日 18:00，确认 24 人，待确认 5 人，取消 3 人，共 32 条记录"
_SEATS = "按当前确认口径，剩余 6 席（30−24）"
_PARKING = "停车安排资料未提供"


class _FixtureModel:
    """Scripted model for the frozen P0 sample. Each model invocation is observable."""

    def __init__(self, trace_path: Path | None = None):
        self.trace_path = trace_path
        self.expected_inputs = {name[:3]: tuple(text.splitlines()) for name, text in _FIXTURES.items()}
        self.findings = {
            "S01": [
                _fact("初版为 13:30–15:30、活动室 A、上限 36 人", ("S01", 2, 3)),
                _fact(_COST, ("S01", 4, 4)),
            ],
            "S02": [
                _fact(_SIGNUPS, ("S02", 1, 2)),
                _fact("当前占用只计算已确认人员", ("S02", 2, 3)),
            ],
            "S03": [
                _fact(_WHEN, ("S03", 2, 3)),
                _fact(_WHERE, ("S03", 2, 4)),
                _fact("费用与报名截止沿用初版", ("S03", 5, 5)),
            ],
        }

    def _record(self, operation):
        if self.trace_path is None:
            return
        with self.trace_path.open("a", encoding="utf-8") as stream:
            stream.write(operation + "\n")

    def extract(self, instruction, chunk):
        self._record(chunk.source_id)
        expected = self.expected_inputs[chunk.source_id]
        if chunk.first_line != 1 or chunk.last_line != len(expected) or tuple(chunk.lines) != expected:
            raise AssertionError("the extractor did not receive the frozen source with its original line numbers")
        return self.findings[chunk.source_id]

    def synthesize(self, instruction, findings):
        self._record("synthesize")
        expected = [fact for source in self.findings.values() for fact in source]
        if len(findings) != len(expected) or any(fact not in findings for fact in expected):
            raise AssertionError("the summarizer did not recover the original source checkpoint")
        return {
            "sections": [
                {
                    "title": "最终安排",
                    "findings": [
                        _fact(_WHEN, ("S03", 2, 3)),
                        _fact(_WHERE, ("S03", 2, 2), ("S03", 4, 4)),
                        _fact(_COST, ("S01", 4, 4), ("S03", 5, 5)),
                    ],
                },
                {"title": "报名情况", "findings": [_fact(_SIGNUPS, ("S02", 1, 2))]},
                {"title": "尚可使用席位", "findings": [_fact(_SEATS, ("S03", 4, 4), ("S02", 2, 3))]},
            ],
            "limitations": [_PARKING],
        }
