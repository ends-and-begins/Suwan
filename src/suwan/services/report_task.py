"""Run the first single-device report workflow with an injected offline model."""
from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Protocol

from suwan.adapters.artifacts import (
    artifact_digest, publish_artifact, remove_registered_artifact,
)
from suwan.context import MAX_PROMPT_CHARS
from suwan.tasks.report_store import ReportTaskStore

_FILE_TYPES = frozenset({".txt", ".md", ".markdown"})
_MAX_FILES = 12
_MAX_FILE_BYTES = 512 * 1024
_MAX_INPUT_BYTES = 1024 * 1024
_CHUNK_CHARS = 3000
_MAX_CHUNKS = 48
_MAX_FINDINGS = 20
_MARKDOWN_SPECIAL = re.compile("([" + re.escape("\\`*_{}[]()#+-.!|~^$") + "])")
_LINE_BREAK = re.compile(r"\r\n|[\r\n]")
_NON_LINE_SEPARATORS = str.maketrans(dict.fromkeys("\v\f\x1c\x1d\x1e\x85\u2028\u2029", " "))
_FALLBACK_QUALITY = "evidence_only"
_HEADCOUNTS = (r"待确认", r"(?<!待)(?:已)?确认", r"(?:已)?取消")
_MISSING_HEADCOUNT = re.compile(r"未提供|未提及|未给出|未说明|缺少|无法确定|无从得知")


@dataclass(frozen=True, slots=True)
class ReportChunk:
    source_id: str
    first_line: int
    lines: tuple[str, ...]

    @property
    def last_line(self) -> int:
        return self.first_line + len(self.lines) - 1


class ReportModel(Protocol):
    def extract(self, instruction: str, chunk: ReportChunk) -> list[dict]:
        """Return findings with source_id, first_line and last_line citations."""
        ...

    def synthesize(self, instruction: str, findings: list[dict]) -> dict:
        """Return sections with titles and cited findings, plus limitations."""
        ...


class ReportStepBlocked(Exception):
    def __init__(
        self, reason: str, source_id: str | None = None, *,
        state: str = "paused", clear_dispatch_intent: bool = False,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.source_id = source_id
        self.state = state
        self.clear_dispatch_intent = clear_dispatch_intent


def _read_source(path: Path, *, physical_lines: bool = True) -> tuple[bytes, tuple[str, ...]]:
    info = path.stat(follow_symlinks=False)
    if path.is_junction() or not stat.S_ISREG(info.st_mode):
        raise ValueError("请选择普通文本文件")
    with path.open("rb") as stream:
        if not os.path.samestat(info, os.fstat(stream.fileno())):
            raise ValueError("文件在打开前已变更")
        raw = stream.read(_MAX_FILE_BYTES + 1)
    if len(raw) > _MAX_FILE_BYTES:
        raise ValueError("单份资料最多 512 KiB")
    text = raw.decode("utf-8-sig")
    if not text.strip() or "\x00" in text:
        raise ValueError("资料必须是非空的 UTF-8 文本")
    if not physical_lines:
        return raw, tuple(text.splitlines())
    lines = _LINE_BREAK.split(text)
    if lines[-1] == "":
        lines.pop()
    return raw, tuple(line.translate(_NON_LINE_SEPARATORS) for line in lines)


def _request_json(payload: object) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _extraction_row(line: int, text: str) -> dict:
    return {"行号": line, "内容": text}


def _extract_payload(instruction: str, chunk: ReportChunk) -> dict:
    return {
        "报告要求": instruction,
        "source_id": chunk.source_id,
        "原文": [_extraction_row(line, text) for line, text in enumerate(chunk.lines, chunk.first_line)],
    }


def _strict_report_json(candidate: str) -> object:
    def checked_object(pairs: list[tuple[str, object]]) -> dict:
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate report JSON field")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        raise ValueError("non-standard report JSON constant")

    return json.loads(candidate, object_pairs_hook=checked_object, parse_constant=reject_constant)


def _chunks(source_id: str, lines: tuple[str, ...], instruction: str) -> Iterable[ReportChunk]:
    overhead = len(_request_json(_extract_payload(instruction, ReportChunk(source_id, 1, ()))))
    if overhead >= MAX_PROMPT_CHARS:
        raise ValueError("报告要求编码后超出模型输入上限，请缩短或去除特殊字符")
    start = 0
    size = 0
    wire_size = overhead
    for index, line in enumerate(lines):
        length = len(line) + 1
        row_chars = len(_request_json(_extraction_row(index + 1, line)))
        if length > _CHUNK_CHARS:
            raise ValueError("资料含超过 2999 个字符的一行，请先换行")
        if overhead + row_chars > MAX_PROMPT_CHARS:
            raise ValueError("一行资料编码后超出模型输入上限，请缩短报告要求或拆分该行")
        if size + length > _CHUNK_CHARS or wire_size + row_chars + (index > start) > MAX_PROMPT_CHARS:
            yield ReportChunk(source_id, start + 1, lines[start:index])
            start, size = index, 0
            wire_size = overhead
        size += length
        wire_size += row_chars + (index > start)
    yield ReportChunk(source_id, start + 1, lines[start:])


def _text(value: object, limit: int) -> str:
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError("invalid model text")
    text = " ".join(value.split())
    if not text or "\x00" in text:
        raise ValueError("empty or invalid model text")
    text.encode("utf-8")
    return text


def _findings(value: object, allowed: list[tuple[str, int, int]]) -> list[dict]:
    if not isinstance(value, list) or len(value) > _MAX_FINDINGS:
        raise ValueError("invalid findings")
    findings = []
    for row in value:
        if not isinstance(row, dict) or set(row) != {"text", "citations"}:
            raise ValueError("invalid finding")
        citations = row["citations"]
        if not isinstance(citations, list) or not 1 <= len(citations) <= 8:
            raise ValueError("missing or excessive citations")
        checked = []
        for citation in citations:
            if not isinstance(citation, dict) or set(citation) != {"source_id", "first_line", "last_line"}:
                raise ValueError("invalid citation")
            source, first, last = citation["source_id"], citation["first_line"], citation["last_line"]
            if not isinstance(source, str) or type(first) is not int or type(last) is not int:
                raise ValueError("invalid citation coordinates")
            if not any(source == sid and low <= first <= last <= high for sid, low, high in allowed):
                raise ValueError("citation outside the validated evidence")
            checked.append({"source_id": source, "first_line": first, "last_line": last})
        findings.append({"text": _text(row["text"], 600), "citations": checked})
    return findings


def _summary(value: object, allowed: list[tuple[str, int, int]]) -> dict:
    if not isinstance(value, dict) or set(value) != {"sections", "limitations"}:
        raise ValueError("invalid report structure")
    sections, limitations = value["sections"], value["limitations"]
    if not isinstance(sections, list) or len(sections) > 8:
        raise ValueError("invalid sections")
    if not isinstance(limitations, list) or len(limitations) > 12:
        raise ValueError("invalid limitations")
    if not sections and not limitations:
        raise ValueError("empty report")
    checked = []
    for section in sections:
        if not isinstance(section, dict) or set(section) != {"title", "findings"}:
            raise ValueError("invalid section")
        findings = _findings(section["findings"], allowed)
        if not findings:
            raise ValueError("empty section")
        checked.append({"title": _text(section["title"], 96), "findings": findings})
    return {"sections": checked, "limitations": [_text(item, 320) for item in limitations]}


def _requested_headcount_conflicts(instruction: str, evidence: list[dict], summary: dict) -> bool:
    conclusions = [item["text"] for section in summary["sections"] for item in section["findings"]]
    for label in _HEADCOUNTS:
        metric = rf"{label}(?:报名)?人数"
        if re.search(metric, instruction) is None:
            continue
        pattern = re.compile(
            rf"{metric}\s*(?:为|共|[:：])?\s*([0-9]+)(?![0-9.])"
            rf"|{label}(?:报名)?\s*(?:为|共|[:：])?\s*([0-9]+)\s*人"
        )
        observed = {int(numeric or with_unit) for item in evidence for numeric, with_unit in pattern.findall(item["text"])}
        if not observed:
            continue
        if any(re.search(metric, gap) and _MISSING_HEADCOUNT.search(gap) for gap in summary["limitations"]):
            return True
        if len(observed) == 1 and all(
            observed.isdisjoint(int(numeric or with_unit) for numeric, with_unit in pattern.findall(conclusion))
            for conclusion in conclusions
        ):
            return True
    return False


def _source_labels(sources: list[dict]) -> dict[str, str]:
    # Distinguish common same-name selections without exposing the full local path.
    parts = {}
    for source in sources:
        path = Path(source["path"])
        if not path.name:
            raise ValueError("report source path has no file name")
        parts[source["id"]] = path.parts[1:] if path.anchor else path.parts
    labels = {}
    for sid, current in parts.items():
        for depth in range(1, min(len(current), 4) + 1):
            suffix = current[-depth:]
            label = " / ".join(" ".join(part.splitlines()) for part in suffix)
            if all(
                tuple(part.casefold() for part in suffix) != tuple(part.casefold() for part in other[-depth:])
                for other_id, other in parts.items() if other_id != sid
            ):
                labels[sid] = label
                break
        else:
            labels[sid] = f"{label}（{sid}）"
    return labels


def _evidence_report(findings: list[dict]) -> dict:
    groups: dict[str, list[dict]] = {}
    for item in findings:
        groups.setdefault(item["citations"][0]["source_id"], []).append(item)
    return {
        "sections": [{"title": f"{sid} 提取要点", "findings": items} for sid, items in groups.items()],
        "limitations": [],
        "quality": _FALLBACK_QUALITY,
    }


def _plain_markdown(value: str) -> str:
    text = value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return _MARKDOWN_SPECIAL.sub(r"\\\1", text)


def _markdown(summary: dict, sources: list[dict], *, legacy_labels: bool = False) -> str:
    if summary.get("quality") == _FALLBACK_QUALITY:
        lines = [
            "# 资料摘录（待复核）", "",
            "资料要点总量超过模型单次汇总上限。以下保留了各来源已提取的要点，尚未进行跨来源核对或汇总。", "",
        ]
    else:
        lines = ["# 资料整理报告", ""]
    for section in summary["sections"]:
        lines += [f"## {_plain_markdown(section['title'])}", ""]
        for finding in section["findings"]:
            refs = []
            for citation in finding["citations"]:
                first, last = citation["first_line"], citation["last_line"]
                position = str(first) if first == last else f"{first}–{last}"
                refs.append(f"{citation['source_id']} 第 {position} 行")
            lines.append(f"- {_plain_markdown(finding['text'])}（{'；'.join(refs)}）")
        lines.append("")
    if summary["limitations"]:
        lines += ["## 资料缺口", "", *[f"- {_plain_markdown(item)}" for item in summary["limitations"]], ""]
    lines += ["## 参考资料", ""]
    labels = _source_labels(sources) if not legacy_labels else {}
    for source in sources:
        sid = source["id"]
        label = labels[sid] if not legacy_labels else Path(source["path"]).name
        revision = f"（来源版本 `{source['sha256'][:12]}`）" if not legacy_labels else ""
        lines.append(f"- {sid}  {_plain_markdown(label)}{revision}")
    return "\n".join(lines) + "\n"


class ReportTaskService:
    """Store each checkpoint before moving to the next explicit step."""

    def __init__(self, store: ReportTaskStore, output_dir: str | Path, model: ReportModel | None = None) -> None:
        self.store = store
        self.output_dir = Path(output_dir).resolve()
        self.model = model

    def submit(self, paths: Iterable[str | Path], instruction: str, *, request_key: str) -> str:
        if not isinstance(instruction, str) or not instruction.strip() or len(instruction) > 4000:
            raise ValueError("请填写 1 至 4000 个字符的报告要求")
        instruction = instruction.strip()
        if "\x00" in instruction:
            raise ValueError("报告要求不能包含空字符")
        instruction.encode("utf-8")
        sources, plan = [], []
        selected: set[Path] = set()
        total_bytes = 0
        for index, requested in enumerate(paths, start=1):
            if index > _MAX_FILES:
                raise ValueError("每个任务最多选择 12 份资料")
            path = Path(requested).expanduser()
            if path.suffix.lower() not in _FILE_TYPES or path.is_symlink() or path.is_junction():
                raise ValueError("请选择 TXT 或 Markdown 普通文件")
            path = path.resolve(strict=True)
            if path in selected:
                raise ValueError("同一文件不能在一个任务中重复选择")
            selected.add(path)
            raw, lines = _read_source(path)
            total_bytes += len(raw)
            if total_bytes > _MAX_INPUT_BYTES:
                raise ValueError("一项任务的资料总大小最多 1 MiB")
            source_id = f"S{index:02d}"
            sources.append({"id": source_id, "path": str(path), "sha256": hashlib.sha256(raw).hexdigest(),
                            "scope": "local", "line_numbering": "physical"})
            for chunk in _chunks(source_id, lines, instruction):
                if len(plan) == _MAX_CHUNKS:
                    raise ValueError("资料需处理的片段超过 48 个，请分成多个报告任务")
                plan.append({"id": f"{source_id}-{chunk.first_line}", "kind": "extract", "source_id": source_id,
                             "first_line": chunk.first_line, "last_line": chunk.last_line})
        if not sources:
            raise ValueError("请至少选择一份资料")
        plan.extend(({"id": "summary", "kind": "synthesize"}, {"id": "publish", "kind": "publish"}))
        return self.store.create(request_key, instruction, sources, plan)

    def advance(
        self, task_id: str, *, model_for_step: Callable[[dict, dict], ReportModel] | None = None,
        claimed_step: dict | None = None,
    ) -> dict:
        step = claimed_step if claimed_step is not None else self.store.claim(task_id)
        if step is None:
            return self.store.get(task_id)
        task = self.store.get(task_id)
        attempt = step["attempt"]
        try:
            documents = self._verified_sources(task["sources"])
            if step["kind"] == "publish":
                self._publish(task, step)
            else:
                model = self.model if model_for_step is None else model_for_step(task, step)
                if model is None:
                    raise ReportStepBlocked("model_unavailable")
                result = self._model_step(model, task, step, documents, external=model_for_step is not None)
                try:
                    self._verified_sources(task["sources"])
                except ReportStepBlocked as exc:
                    if model_for_step is not None and result.get("quality") != _FALLBACK_QUALITY:
                        raise ReportStepBlocked("source_result_discarded", exc.source_id, state="unknown") from exc
                    raise
                self.store.complete(task_id, step["id"], attempt, result)
        except ReportStepBlocked as exc:
            self.store.fail_step(task_id, step["id"], attempt, exc.reason, state=exc.state, source_id=exc.source_id,
                                 clear_dispatch_intent=exc.clear_dispatch_intent)
        return self.store.get(task_id)

    def run(self, task_id: str) -> dict:
        task = self.store.get(task_id)
        while task["state"] == "ready":
            task = self.advance(task_id)
        return task

    @staticmethod
    def _verified_sources(sources: list[dict]) -> dict[str, tuple[str, ...]]:
        documents = {}
        for source in sources:
            sid = source["id"]
            try:
                raw, lines = _read_source(Path(source["path"]), physical_lines=source.get("line_numbering") == "physical")
            except (OSError, ValueError) as exc:
                raise ReportStepBlocked("source_unreadable", sid) from exc
            if hashlib.sha256(raw).hexdigest() != source["sha256"]:
                raise ReportStepBlocked("source_changed", sid)
            documents[sid] = lines
        return documents

    @staticmethod
    def _call_model(operation: Callable, *args) -> object:
        try:
            return operation(*args)
        except ReportStepBlocked:
            raise
        except Exception as exc:
            raise ReportStepBlocked("model_failed") from exc

    def _saved_steps(self, task_id: str) -> list[dict]:
        try:
            return self.store.steps(task_id)
        except (KeyError, RecursionError, TypeError, ValueError) as exc:
            raise ReportStepBlocked("task_integrity", state="failed") from exc

    @staticmethod
    def _saved_findings(steps: list[dict]) -> tuple[list[dict], list[tuple[str, int, int]]]:
        findings = []
        try:
            for row in steps:
                if row["kind"] == "extract":
                    if row["state"] != "completed":
                        raise ValueError("incomplete extraction checkpoint")
                    span = (row["source_id"], row["first_line"], row["last_line"])
                    findings.extend(_findings(row["result"]["findings"], [span]))
        except (KeyError, RecursionError, TypeError, ValueError) as exc:
            raise ReportStepBlocked("task_integrity", state="failed") from exc
        allowed = [(item["source_id"], item["first_line"], item["last_line"])
                   for finding in findings for item in finding["citations"]]
        return findings, allowed

    def _model_step(
        self, model: ReportModel, task: dict, step: dict, documents: dict[str, tuple[str, ...]], *, external: bool
    ) -> dict:
        instruction = task["instruction"]
        failure_state = "unknown" if external else "paused"
        try:
            if step["kind"] == "extract":
                sid, first, last = step["source_id"], step["first_line"], step["last_line"]
                chunk = ReportChunk(sid, first, documents[sid][first - 1:last])
                value = self._call_model(model.extract, instruction, chunk)
                return {"findings": _findings(value, [(sid, first, last)])}
        except ValueError as exc:
            raise ReportStepBlocked("invalid_model_result", state=failure_state) from exc
        findings, allowed = self._saved_findings(self._saved_steps(task["id"]))
        try:
            value = self._call_model(model.synthesize, instruction, findings)
        except ReportStepBlocked as exc:
            if exc.reason == "synthesis_input_too_large":
                return _evidence_report(findings)
            raise
        try:
            summary = _summary(value, allowed)
        except ValueError as exc:
            raise ReportStepBlocked("invalid_model_result", state=failure_state) from exc
        if _requested_headcount_conflicts(instruction, findings, summary):
            raise ReportStepBlocked("report_facts_need_review", state=failure_state)
        return summary

    def _publish(self, task: dict, step: dict) -> None:
        saved = self._saved_steps(task["id"])
        findings, allowed = self._saved_findings(saved)
        try:
            summary = next(row["result"] for row in saved if row["kind"] == "synthesize")
            if isinstance(summary, dict) and summary.get("quality") == _FALLBACK_QUALITY:
                if not findings or summary != _evidence_report(findings):
                    raise ValueError("invalid local evidence checkpoint")
            else:
                summary = _summary(summary, allowed)
        except (KeyError, StopIteration, TypeError, ValueError) as exc:
            raise ReportStepBlocked("task_integrity", state="failed") from exc
        content = _markdown(summary, task["sources"])
        path = self.output_dir / f"report-{task['id']}.md"
        if (
            not any(source.get("line_numbering") for source in task["sources"])
            and summary.get("quality") != _FALLBACK_QUALITY
            and not path.is_symlink()
            and path.is_file()
        ):
            # A pre-upgrade publication interrupted before receipt can be reused byte-for-byte.
            legacy = _markdown(summary, task["sources"], legacy_labels=True)
            try:
                with path.open("rb") as stream:
                    if hashlib.file_digest(stream, "sha256").hexdigest() == artifact_digest(legacy):
                        content = legacy
            except OSError as exc:
                raise ReportStepBlocked("artifact_write_failed") from exc
        digest = artifact_digest(content)

        def write() -> None:
            self._verified_sources(task["sources"])
            publish_artifact(path, content)

        def verify_source() -> None:
            try:
                self._verified_sources(task["sources"])
            except ReportStepBlocked:
                # Preserve this known version privately and remove only the matching,
                # checked object from the normal output location.
                draft = self.output_dir / ".drafts" / f"{path.stem}-{digest}.md"
                with suppress(OSError, ValueError):
                    if not draft.parent.is_symlink() and not draft.parent.is_junction():
                        publish_artifact(draft, content)
                        remove_registered_artifact(path, digest)
                raise

        try:
            self.store.register_artifact(task["id"], step["id"], step["attempt"], str(path),
                                         digest, publish=write, verify_source=verify_source)
        except FileExistsError as exc:
            raise ReportStepBlocked("artifact_conflict") from exc
        except (OSError, ValueError) as exc:
            raise ReportStepBlocked("artifact_write_failed") from exc
