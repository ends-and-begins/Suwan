"""第二版固定30项记忆存储/检索验收；使用独立临时目录并禁止联网。

本脚本不评价真实LLM回答。5更新项直接调用事实存储接口，5隐私项
验证存储控制；自然对话识别和前端另由其集成验收验证。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from datetime import datetime
from pathlib import Path

if __package__:
    from ._offline import install_offline_guard
else:
    from _offline import install_offline_guard

GUARD = install_offline_guard()
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from suwan.bus import EventBus
from suwan.clock import ManualClock
from suwan.organs.memory import MemoryOrgan

FIXTURE = ROOT / "tests" / "fixtures" / "round2_memory_cases.json"
FIXTURE_SHA256 = "4183efed5680fea41e70c4c2203fb9318f999554740205555f6880be037fd8a5"
NOW = datetime(2026, 10, 16, 12)
UPDATE_KEYS = ("user.name", "user.city", "preference.drink", "preference.weekend_activity", "preference.contact_time")


def _organ(path: Path) -> MemoryOrgan:
    return MemoryOrgan(EventBus(), ManualClock(NOW), file_path=path, save_every=1000)


def _restart(organ: MemoryOrgan, path: Path) -> MemoryOrgan:
    organ.force_save()
    restored = _organ(path)
    restored._load()
    return restored


def _corpus(organ: MemoryOrgan, fixture: dict) -> dict[str, str]:
    expected = {}
    for case in fixture["recall"]:
        item = organ.add(case["source"], kind="event", source="user")
        expected[case["label"]] = item.id
    for record in fixture["distractors"]:
        kind = "event" if record["kind"] == "experience" else record["kind"]
        if record.get("superseded_by"):
            # 真正的替代链必须完整，不往磁盘伪造指向不存在ID的关系。
            organ.update_fact("test.old_city", "上海", record["content"])
            organ.update_fact("test.old_city", "更新过", "一条已经更新的住处记录")
        else:
            item = organ.add(record["content"], importance=8, kind=kind, source="user" if kind == "event" else "reflection")
            if record.get("deleted"):
                organ.delete(item.id)
    return expected


def _recall_case(base: Path, fixture: dict, case: dict) -> dict:
    path = base / case["label"] / "memories.json"
    organ = _organ(path)
    expected = _corpus(organ, fixture)
    before_ids = {item.id for item in organ.items}
    restored = _restart(organ, path)
    assert before_ids == {item.id for item in restored.items}, "ID在保存重载后发生变化"
    hits = restored.search(case["query"], limit=3)
    ids = [hit.item.id for hit in hits]
    rank = ids.index(expected[case["label"]]) + 1 if expected[case["label"]] in ids else None
    return {"label": case["label"], "query": case["query"], "target": case["source"],
            "rank": rank, "top1": rank == 1, "top3": rank is not None,
            "stored_records": len(restored.items), "ids_stable": True,
            "hits": [{"content": hit.item.content, "kind": hit.item.kind, "source": hit.item.source,
                      "score": hit.score, "reasons": list(hit.reasons)} for hit in hits]}


def _update_case(base: Path, case: dict, key: str) -> dict:
    path = base / case["label"] / "memories.json"
    organ = _organ(path)
    old = organ.update_fact(key, case["old_value"], case["before"])
    new = organ.update_fact(key, case["new_value"], case["after"])
    restored = _restart(organ, path)
    current = restored.get_fact(key)
    old_restored = next(item for item in restored.items if item.id == old.id)
    assert current is not None and current.id == new.id and current.fact_value == case["new_value"]
    assert old_restored.superseded_by == current.id
    assert old.id not in [hit.item.id for hit in restored.search(case["query"], limit=100)]
    return {"label": case["label"], "current_value": current.fact_value, "old_superseded": True,
            "survives_restart": True, "passed": True, "scope": "storage.update_fact"}


def _privacy_case(base: Path, case: dict) -> dict:
    path = base / case["label"] / "memories.json"
    organ = _organ(path)
    action = case["action"]
    retained = organ.add("保留的一条共同经历：一起看过日落", source="user")
    if action == "third_person":
        organ.add(case["source"], source="user")
    elif action == "delete":
        source = organ.add(case["source"], source="user")
        organ.update_fact("user.pet_name", "栗子", case["source"], derived_from=(source.id,))
        organ.delete(source.id)
    elif action == "delete_derived":
        source = organ.add(case["source"], source="share", kind="knowledge")
        derived = organ.add(case["derived"], source="reflection", kind="generated", derived_from=(source.id,))
        organ.delete(source.id)
        assert derived.id not in {item.id for item in organ.items}
    elif action == "pause":
        organ.set_recording_enabled(False)
        assert organ.add(case["source"], source="user") is None
        assert organ.update_fact("user.address", "808号", case["source"]) is None
    restored = _restart(organ, path)
    assert retained.id in {item.id for item in restored.items}
    if action == "unknown":
        assert restored.get_fact("user.birthday") is None
        assert restored.search(case["query"]) == []
    elif action == "third_person":
        # 第三人称的经历可检索，但不能变成本人的确定事实。
        assert restored.get_fact("user.city") is None
    else:
        hits = restored.search(case["query"], limit=100, include_generated=True)
        assert all(case["forbidden"] not in hit.item.content for hit in hits)
        assert all(case["forbidden"] not in item.content for item in restored.items)
        if action == "delete":
            assert restored.get_fact("user.pet_name") is None
        if action == "pause":
            assert restored.recording_enabled is False
            assert restored.get_fact("user.address") is None
    return {"label": case["label"], "action": action, "survives_restart": True,
            "unrelated_record_retained": True, "passed": True, "scope": "storage.privacy"}


def run_acceptance(base: Path) -> dict:
    raw = FIXTURE.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    assert digest == FIXTURE_SHA256, "冻结语料已改变，必须另行记录新版本与旧结果"
    fixture = json.loads(raw)
    recall = [_recall_case(base, fixture, case) for case in fixture["recall"]]
    updates = [_update_case(base, case, key) for case, key in zip(fixture["updates"], UPDATE_KEYS)]
    privacy = [_privacy_case(base, case) for case in fixture["privacy"]]
    top1 = sum(row["top1"] for row in recall)
    top3 = sum(row["top3"] for row in recall)
    return {"fixture_sha256": digest, "python": sys.version.split()[0], "network_blocks": GUARD["network_blocks"],
            "recall_count": len(recall), "top1_count": top1, "top3_count": top3,
            "top3_ratio": top3 / len(recall), "recall": recall, "updates": updates, "privacy": privacy,
            "scope": "deterministic retrieval + memory storage controls; no LLM answer scoring",
            "passed": top3 >= 18 and all(row["passed"] for row in updates + privacy)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="suwan-round2-memory-") as temporary:
        result = run_acceptance(Path(temporary))
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    raise SystemExit(0 if result["passed"] else 1)


if __name__ == "__main__":
    main()
