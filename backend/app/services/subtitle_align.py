"""字幕对齐关联领域服务：把语音段与字幕行按时间轴对齐并记录偏移，产出对齐结果数据集供字幕包生成与纠错回路消费。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.services.alignment_workflow import workflow
from app.store import store


def _payload_values(payload: Any | None) -> dict[str, Any]:
    if payload is None:
        return {}
    if isinstance(payload, dict):
        merged = dict(payload.get("values") or {})
        merged.update({k: v for k, v in payload.items() if k not in {"action", "values", "remark"}})
        return merged
    values = dict(getattr(payload, "values", {}) or {})
    values.update(getattr(payload, "model_extra", None) or {})
    return values


class SubtitleAlignService(BaseModuleService):
    MODULE = "subtitle_align"
    ENTITY = "对齐结果"
    OPERATION_TERM = "关联"
    LIST_FIELDS = ["对齐编号", "关联分段", "字幕行号", "对齐起始毫秒", "对齐结束毫秒", "偏移毫秒", "对齐方式", "对齐行数", "实际起始毫秒", "实际结束毫秒", "实际使用边界", "边界来源", "原分段编号"]
    REQUIRED_FIELDS = ["对齐编号", "关联分段", "字幕行号"]
    STATUS_ORDER = ["待对齐", "对齐中", "已对齐", "偏移超限"]
    ACTION_RULES = {"启动对齐": "对齐中", "确认对齐": "已对齐", "登记超限": "偏移超限", "接收自动校准": "已对齐", "采用手工边界": "已对齐"}
    NEGATIVE_ACTIONS = []

    def run_action(
        self, entry_id: int, action: str, payload: Any | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"
        segment_ref = entry.get("关联分段")

        if action == "接收自动校准":
            values = _payload_values(payload)
            result = workflow.receive_auto_calibration(
                segment_ref,
                start_ms=values.get("实际起始毫秒", values.get("start_ms")),
                end_ms=values.get("实际结束毫秒", values.get("end_ms")),
                offset_ms=values.get("偏移毫秒", values.get("offset_ms")),
                calibration_id=values.get("calibration_id"),
                idempotency_key=values.get("idempotency_key"),
            )
            row = workflow.find_alignment(str(segment_ref))
            return (row, result["message"]) if result.get("ok") else (None, result["message"])

        if action == "采用手工边界":
            ok, message, row = workflow.save_manual_adjustment(segment_ref)
            return (row, message) if ok else (None, message)

        return super().run_action(entry_id, action, payload)

    def receive_auto_calibration(self, segment_ref: str, *args: Any, **kwargs: Any):
        return workflow.receive_auto_calibration(segment_ref, *args, **kwargs)

    def save_manual_boundary(self, segment_ref: str, *args: Any, **kwargs: Any):
        return workflow.save_manual_adjustment(segment_ref, *args, **kwargs)

    def get_effective_rows(self) -> list[dict[str, Any]]:
        """返回每个分段实际采用的对齐边界及其来源。"""
        return [
            row for row in store.rows(self.MODULE)
            if str(row.get("实际使用", "是")) != "否"
        ]

    list_effective = get_effective_rows

    def apply_manual_result(self, segment_ref: str, *args: Any, **kwargs: Any):
        return workflow.save_manual_adjustment(segment_ref, *args, **kwargs)

    def receive_auto_result(self, segment_ref: str, *args: Any, **kwargs: Any):
        return workflow.receive_auto_calibration(segment_ref, *args, **kwargs)

    def check_offset_threshold(self, align_id: str, max_offset: int) -> tuple[bool, str]:
        """检查对齐偏移是否超过阈值。"""
        rows = store.rows(self.MODULE)
        for r in rows:
            if r.get("对齐编号") == align_id:
                offset = int(r.get("偏移毫秒", "0"))
                if offset > max_offset:
                    r["status"] = "偏移超限"
                    r["abnormal"] = True
                    return False, f"偏移 {offset}ms 超过阈值 {max_offset}ms，需要纠错回路处理"
                return True, f"偏移 {offset}ms 在阈值内"
        return False, f"对齐条目 {align_id} 不存在"

    def get_alignments_by_segment(self, segment_id: str) -> list[dict[str, Any]]:
        """获取指定语音段的所有对齐结果。"""
        rows = store.rows(self.MODULE)
        return [r for r in rows if r.get("关联分段") == segment_id]

    def get_overshoot_alignments(self, max_offset: int) -> list[dict[str, Any]]:
        """获取所有偏移超限的对齐结果。"""
        rows = store.rows(self.MODULE)
        return [
            r for r in rows
            if int(r.get("偏移毫秒", "0")) > max_offset
        ]

    def recalculate_alignment(
        self, align_id: str, new_start: int, new_end: int, new_offset: int,
    ) -> tuple[bool, str]:
        """重新计算对齐参数并更新；拒绝 start >= end 的乱序/零时长边界。"""
        if new_end <= new_start:
            return False, f"对齐 {align_id} 边界非法：{new_start}-{new_end}"
        rows = store.rows(self.MODULE)
        for r in rows:
            if r.get("对齐编号") == align_id:
                r["对齐起始毫秒"] = str(new_start)
                r["对齐结束毫秒"] = str(new_end)
                r["实际起始毫秒"] = str(new_start)
                r["实际结束毫秒"] = str(new_end)
                r["实际使用边界"] = f"{new_start}-{new_end}"
                r["effective_start_ms"] = new_start
                r["effective_end_ms"] = new_end
                r["used_boundary"] = [new_start, new_end]
                r["偏移毫秒"] = str(new_offset)
                r["对齐方式"] = "纠错后重算"
                r["边界来源"] = "系统"
                r["boundary_source"] = "system"
                r["status"] = "已对齐"
                r["abnormal"] = False
                return True, f"对齐 {align_id} 已重新计算"
        return False, f"对齐条目 {align_id} 不存在"