"""语音分段切分领域服务：把转写产出切成带毫秒边界的语音段，产出语音段数据集供字幕对齐与说话人标注消费。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.boundary_engine import first_value, resolve_boundaries
from app.services.alignment_workflow import workflow
from app.store import store


class SpeechSegmentService(BaseModuleService):
    MODULE = "speech_segment"
    ENTITY = "语音段"
    OPERATION_TERM = "切分"
    LIST_FIELDS = ["分段编号", "关联任务", "起始毫秒", "结束毫秒", "段内文本", "段置信度", "静音前毫秒", "重叠标记", "边界来源", "手工状态", "原分段编号"]
    REQUIRED_FIELDS = ["分段编号", "关联任务", "起始毫秒"]
    STATUS_ORDER = ["已切分", "待对齐", "已对齐", "边界冲突"]
    ACTION_RULES = {"提交对齐": "待对齐", "确认对齐": "已对齐", "登记冲突": "边界冲突", "保存手工调整": "待对齐", "后移20毫秒": "待对齐", "重算边界": "待对齐", "确认字句": "已对齐", "接收自动校准": "对齐中"}
    NEGATIVE_ACTIONS = []

    @staticmethod
    def _payload_values(payload: Any | None) -> dict[str, Any]:
        if payload is None:
            return {}
        if isinstance(payload, dict):
            merged = dict(payload.get("values") or {})
            for key, value in payload.items():
                if key not in {"action", "values", "remark"} and key not in merged:
                    merged[key] = value
            return merged
        values = dict(getattr(payload, "values", {}) or {})
        extra = getattr(payload, "model_extra", None) or {}
        for key, value in extra.items():
            values.setdefault(key, value)
        return values

    def run_action(
        self, entry_id: int, action: str, payload: Any | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        values = self._payload_values(payload)
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        ref = entry.get("分段编号") or entry_id

        if action == "保存手工调整":
            ok, message, _ = workflow.save_manual_adjustment(
                ref,
                start_ms=first_value(values, "起始毫秒", "start_ms", "start", "new_start", "new_start_ms", "manual_start"),
                end_ms=first_value(values, "结束毫秒", "end_ms", "end", "new_end", "new_end_ms", "manual_end"),
                shift_ms=first_value(values, "shift_ms", "后移毫秒", "shift"),
                text=first_value(values, "段内文本", "text", "confirmed_text", "人工确认字句"),
                confirmed=bool(values.get("confirmed", values.get("确认字句", False))),
            )
            return (workflow.find_segment(ref), message) if ok else (None, message)

        if action == "后移20毫秒":
            ok, message, _ = workflow.save_manual_adjustment(ref, shift_ms=20)
            return (workflow.find_segment(ref), message) if ok else (None, message)

        if action == "接收自动校准":
            result = workflow.receive_auto_calibration(
                ref,
                start_ms=first_value(values, "起始毫秒", "start_ms", "start", "new_start", "对齐起始毫秒"),
                end_ms=first_value(values, "结束毫秒", "end_ms", "end", "new_end", "对齐结束毫秒"),
                offset_ms=first_value(values, "偏移毫秒", "offset_ms", "new_offset"),
                confidence=first_value(values, "段置信度", "confidence"),
                calibration_id=values.get("calibration_id"),
                idempotency_key=first_value(values, "idempotency_key", "key", "request_id"),
            )
            return (workflow.find_segment(ref), result["message"]) if result.get("ok") else (None, result["message"])

        if action == "重算边界":
            task_id = values.get("task_id") or entry.get("关联任务")
            result = self.recalculate_task(str(task_id))
            return entry, f"已重算任务 {task_id} 的边界，产出 {len(result['outputs'])} 个正时长区间"

        entry, message = super().run_action(entry_id, action, payload)
        if entry is not None and action in {"提交对齐", "确认对齐"}:
            workflow.ensure_alignment_for_action(ref)
        return entry, message

    def recalculate_boundaries(self, segments: list[dict[str, Any]]) -> dict[str, Any]:
        """纯边界解析入口，供接口和测试直接调用。"""
        return resolve_boundaries(segments)

    def recalculate_task(self, task_id: str) -> dict[str, Any]:
        """重算仓库中某个转写任务的全部分段。"""
        segments = self.get_segments_by_task(task_id)
        return resolve_boundaries(segments)

    # 常见命名别名，便于不同调用方按业务语言使用同一套边界算法。
    recompute_task = recalculate_task
    recalculate_segments = recalculate_boundaries
    recompute_segments = recalculate_boundaries
    resolve_overlaps = recalculate_boundaries

    def save_manual_adjustment(self, segment_ref: str, *args: Any, **kwargs: Any):
        return workflow.save_manual_adjustment(segment_ref, *args, **kwargs)

    def receive_auto_calibration(self, segment_ref: str, *args: Any, **kwargs: Any):
        return workflow.receive_auto_calibration(segment_ref, *args, **kwargs)

    def get_provenance(self, segment_ref: str) -> dict[str, Any] | None:
        return workflow.provenance(segment_ref)

    def detect_overlap(
        self, seg_a_id: str, seg_b_id: str,
    ) -> tuple[bool, str, int]:
        """检测两个语音段的重叠毫秒数。"""
        rows = store.rows(self.MODULE)
        seg_a = seg_b = None
        for r in rows:
            if r.get("分段编号") == seg_a_id:
                seg_a = r
            elif r.get("分段编号") == seg_b_id:
                seg_b = r
        if not seg_a or not seg_b:
            return False, "分段不存在", 0
        a_start = int(seg_a.get("起始毫秒", "0"))
        a_end = int(seg_a.get("结束毫秒", "0"))
        b_start = int(seg_b.get("起始毫秒", "0"))
        b_end = int(seg_b.get("结束毫秒", "0"))
        overlap = max(0, min(a_end, b_end) - max(a_start, b_start))
        if overlap > 0:
            return True, f"重叠 {overlap} ms", overlap
        return False, "无重叠", 0

    def set_boundary(
        self, seg_id: str, new_start: int, new_end: int,
    ) -> tuple[bool, str]:
        """设置语音段的唯一归属边界；非法倒序边界不得落库。"""
        if new_end <= new_start:
            return False, f"分段 {seg_id} 边界非法：[{new_start}, {new_end}) 必须有正时长"
        rows = store.rows(self.MODULE)
        for r in rows:
            if r.get("分段编号") == seg_id:
                r["起始毫秒"] = str(new_start)
                r["结束毫秒"] = str(new_end)
                r["重叠标记"] = "已去重"
                r["边界来源"] = "人工"
                r["boundary_source"] = "manual"
                return True, f"分段 {seg_id} 边界设置为 [{new_start}, {new_end}]"
        return False, f"分段 {seg_id} 不存在"

    def get_segments_by_task(self, task_id: str) -> list[dict[str, Any]]:
        """获取指定转写任务的全部语音段，按起始毫秒排序。"""
        rows = store.rows(self.MODULE)
        segs = [r for r in rows if r.get("关联任务") == task_id]
        segs.sort(key=lambda r: int(r.get("起始毫秒", "0")))
        return segs

    def get_segments_with_overlap(self) -> list[dict[str, Any]]:
        """获取所有带有重叠标记的语音段。"""
        rows = store.rows(self.MODULE)
        return [r for r in rows if r.get("重叠标记") != "无"]