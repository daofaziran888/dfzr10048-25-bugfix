from __future__ import annotations

from app.pipeline.integration import PipelineOrchestrator
from app.services.alignment_workflow import workflow
from app.services.boundary_engine import assert_positive_nonoverlapping


def test_enclosed_interjection_is_not_cut_to_reversed_boundary():
    orch = PipelineOrchestrator()
    segments = [
        {"seg_id": "LONG", "start_ms": 0, "end_ms": 1000, "text": "长段"},
        {"seg_id": "INTERJECT", "start_ms": 100, "end_ms": 200, "text": "插话"},
        {"seg_id": "NEXT", "start_ms": 1000, "end_ms": 1400, "text": "口播"},
    ]
    result = orch.link3_overlap_boundary("T-NESTED", segments)
    outputs = result["outputs"]

    assert_positive_nonoverlapping(outputs)
    by_id = {}
    for output in outputs:
        by_id.setdefault(output["seg_id"], []).append((output["start_ms"], output["end_ms"]))

    assert by_id["INTERJECT"] == [(100, 200)]
    assert by_id["LONG"] == [(0, 100), (200, 1000)]
    assert by_id["NEXT"] == [(1000, 1400)]
    # 旧缺陷会得到 550-200；任何输出都不能 start > end。
    assert all(item["end_ms"] > item["start_ms"] for item in outputs)


def test_manual_boundary_wins_over_duplicate_auto_and_reaches_srt():
    case = workflow.reproduce_boundary_case()
    assert case["ok"]

    segment_ref = "SPCH-B-INTERJECT"
    ok, message, alignment = workflow.save_manual_adjustment(
        segment_ref, shift_ms=20, text="这里插一句", confirmed=True,
    )
    assert ok, message
    assert alignment is not None
    assert alignment["实际使用边界"] == "120-220"
    assert alignment["边界来源"] == "人工"

    first = workflow.receive_auto_calibration(
        segment_ref, 100, 200, idempotency_key="test-duplicate-auto",
    )
    second = workflow.receive_auto_calibration(
        segment_ref, 100, 200, idempotency_key="test-duplicate-auto",
    )
    assert first["ok"] and first["applied"] is False
    assert second["duplicate"] is True
    assert second["effective_source"] == "manual"

    ok, message, pack = workflow.retry_pack_generation("SBPK-BOUNDARY")
    assert ok, message
    assert pack is not None
    content = pack["file_content"]
    assert "00:00:00,120 --> 00:00:00,220" in content
    assert "这里插一句" in content
    assert "00:00:01,000 --> 00:00:01,500" in content

    provenance = workflow.provenance(segment_ref)
    assert provenance is not None
    assert provenance["alignments"][0]["source_segment_id"] == segment_ref
    assert any(pack_item["字幕包编号"] == "SBPK-BOUNDARY" for pack_item in provenance["packs"])

    from app.services.subtitle_pack import SubtitlePackService
    from app.store import store

    pack_row = workflow.find_row("subtitle_pack", "SBPK-BOUNDARY")
    assert pack_row is not None
    action_entry, action_message = SubtitlePackService().run_action(
        int(pack_row["id"]), "重试生成", None,
    )
    assert action_entry is not None
    assert action_entry["status"] == "可下载"
    assert "00:00:00,120 --> 00:00:00,220" in action_entry["file_content"]
    assert store.find("subtitle_pack", int(pack_row["id"])) is not None
