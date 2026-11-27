"""语音分段、字幕对齐与字幕包交付之间的一致性工作流。

这里集中处理三件容易在联调中出错的事：

1. 手工边界与自动校准竞争时，人工已确认/保存的边界优先；
2. 重复到达的自动校准结果按幂等键去重，不产生重复对齐行；
3. 字幕包文件由同一份“实际生效边界”生成，保证列表、预览和文件时码一致。
"""
from __future__ import annotations

import hashlib
import threading
from datetime import datetime
from typing import Any, ClassVar

from app.services.boundary_engine import as_int, resolve_boundaries
from app.store import store

SourceLabel = {
    "manual": "人工调整（实际使用）",
    "auto": "自动校准（实际使用）",
    "system": "包围拆分（系统重算）",
}

SourceValue = {
    "manual": "人工",
    "auto": "自动",
    "system": "系统",
}


def timecode(ms: int, fmt: str = "srt") -> str:
    """把毫秒转成 SRT/WebVTT/ASS 时码。"""
    ms = max(0, int(ms))
    hour, rem = divmod(ms, 3_600_000)
    minute, rem = divmod(rem, 60_000)
    second, millisecond = divmod(rem, 1000)
    normalized = (fmt or "srt").lower()
    if normalized == "vtt":
        sep = "."
        return f"{hour:02d}:{minute:02d}:{second:02d}{sep}{millisecond:03d}"
    if normalized == "ass":
        return f"{hour:d}:{minute:02d}:{second:02d}.{millisecond // 10:02d}"
    sep = ","
    return f"{hour:02d}:{minute:02d}:{second:02d}{sep}{millisecond:03d}"


class AlignmentWorkflow:
    """操作内存仓库的工作流服务；进程内单例即可。"""

    _auto_keys: ClassVar[set[str]] = set()
    _auto_receipts: ClassVar[dict[str, dict[str, Any]]] = {}
    _tokens: ClassVar[dict[str, dict[str, Any]]] = {}
    _global_lock: ClassVar[threading.RLock] = threading.RLock()
    _segment_locks: ClassVar[dict[str, threading.RLock]] = {}

    # ---------- 基础查找 ----------

    def _lock(self, segment_ref: str) -> threading.RLock:
        key = str(segment_ref)
        with self._global_lock:
            return self._segment_locks.setdefault(key, threading.RLock())

    @staticmethod
    def find_row(module: str, ref: Any) -> dict[str, Any] | None:
        rows = store.rows(module)
        ref_text = str(ref)
        for row in rows:
            if str(row.get("id")) == ref_text:
                return row
        code_fields = {
            "speech_segment": "分段编号",
            "subtitle_align": "对齐编号",
            "subtitle_pack": "字幕包编号",
            "transcribe_task": "任务编号",
            "media_source": "媒体源编号",
        }
        code_field = code_fields.get(module)
        if code_field:
            for row in rows:
                if str(row.get(code_field)) == ref_text:
                    return row
        # 最后宽容地在任意“编号”字段中匹配。
        for row in rows:
            for key, value in row.items():
                if key.endswith("编号") and str(value) == ref_text:
                    return row
        return None

    def find_segment(self, ref: Any) -> dict[str, Any] | None:
        return self.find_row("speech_segment", ref)

    @staticmethod
    def segment_code(seg: dict[str, Any]) -> str:
        return str(seg.get("分段编号") or seg.get("seg_id") or seg.get("id"))

    @staticmethod
    def _next_id(module: str) -> int:
        rows = store.rows(module)
        return max((as_int(row.get("id"), 0) or 0 for row in rows), default=0) + 1

    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # ---------- 字幕对齐行 ----------

    def find_alignment(self, segment_code: str) -> dict[str, Any] | None:
        rows = store.rows("subtitle_align")
        effective = [
            row for row in rows
            if str(row.get("关联分段")) == segment_code
            and str(row.get("实际使用", "是")) != "否"
        ]
        if effective:
            return effective[0]
        for row in rows:
            if str(row.get("关联分段")) == segment_code:
                return row
        return None

    def upsert_alignment(
        self,
        seg: dict[str, Any],
        start_ms: int,
        end_ms: int,
        source: str,
        *,
        offset_ms: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        seg_code = self.segment_code(seg)
        row = self.find_alignment(seg_code)
        if row is None:
            rows = store.rows("subtitle_align")
            line_no = self._next_id("subtitle_align")
            row = {
                "id": self._next_id("subtitle_align"),
                "status": "已对齐",
                "pending": False,
                "abnormal": False,
                "对齐编号": f"SALN-{seg_code}",
                "关联分段": seg_code,
                "字幕行号": str(line_no),
                "对齐起始毫秒": str(start_ms),
                "对齐结束毫秒": str(end_ms),
                "偏移毫秒": "0",
                "对齐方式": SourceLabel.get(source, source),
                "对齐行数": "1",
            }
            rows.append(row)

        old_start = as_int(row.get("对齐起始毫秒"), start_ms) or start_ms
        if offset_ms is None:
            offset_ms = start_ms - old_start
        source_label = SourceLabel.get(source, source)
        source_value = SourceValue.get(source, source)
        row.update({
            "status": "已对齐",
            "pending": False,
            "abnormal": False,
            "对齐起始毫秒": str(start_ms),
            "对齐结束毫秒": str(end_ms),
            "偏移毫秒": str(offset_ms),
            "对齐方式": source_label,
            "实际起始毫秒": str(start_ms),
            "实际结束毫秒": str(end_ms),
            "实际使用边界": f"{start_ms}-{end_ms}",
            "边界来源": source_value,
            "来源": source_label,
            "source": source,
            "boundary_source": source,
            "实际使用": "是",
            "原分段编号": seg_code,
            "segment_id": seg_code,
            "source_segment_id": seg_code,
            "effective_start_ms": start_ms,
            "effective_end_ms": end_ms,
            "used_boundary": [start_ms, end_ms],
        })
        if extra:
            row.update(extra)
        return row

    def ensure_alignment_for_action(self, seg_ref: Any) -> dict[str, Any] | None:
        seg = self.find_segment(seg_ref)
        if seg is None:
            return None
        start = as_int(seg.get("起始毫秒"), 0) or 0
        end = as_int(seg.get("结束毫秒"), start) or start
        raw_source = str(seg.get("boundary_source") or seg.get("边界来源") or "system")
        if raw_source in {"人工", "手工"}:
            source = "manual"
        elif raw_source == "自动":
            source = "auto"
        else:
            source = raw_source if raw_source in SourceLabel else "system"
        return self.upsert_alignment(seg, start, end, source)

    # ---------- 手工调整（含两阶段保存，用来复现保存未完成窗口） ----------

    def prepare_manual_adjustment(
        self,
        segment_ref: Any,
        start_ms: Any = None,
        end_ms: Any = None,
        shift_ms: Any = None,
        text: str | None = None,
        token: str | None = None,
    ) -> tuple[bool, str, str | None]:
        seg = self.find_segment(segment_ref)
        if seg is None:
            return False, f"语音分段 {segment_ref} 不存在", None
        seg_code = self.segment_code(seg)
        lock = self._lock(seg_code)
        lock.acquire()
        try:
            current_start = as_int(seg.get("起始毫秒"), 0) or 0
            current_end = as_int(seg.get("结束毫秒"), current_start) or current_start
            start = as_int(start_ms, current_start)
            end = as_int(end_ms, current_end)
            shift = as_int(shift_ms)
            if shift is not None and start_ms is None and end_ms is None:
                start += shift
                end += shift
            if start is None or end is None or end <= start:
                return False, f"手工边界非法：{start}-{end}，结束必须大于起始", None
            token = token or f"manual-{seg_code}-{hashlib.md5(f'{start}:{end}:{self._now()}'.encode()).hexdigest()[:10]}"
            self._tokens[token] = {
                "segment_code": seg_code,
                "start_ms": start,
                "end_ms": end,
                "text": text,
            }
            seg["手工状态"] = "保存中"
            seg["manual_save_token"] = token
            return True, f"分段 {seg_code} 手工调整正在保存", token
        finally:
            lock.release()

    def commit_manual_adjustment(self, segment_ref: Any, token: str) -> tuple[bool, str, dict[str, Any] | None]:
        seg = self.find_segment(segment_ref)
        if seg is None:
            return False, f"语音分段 {segment_ref} 不存在", None
        seg_code = self.segment_code(seg)
        pending = self._tokens.get(str(token))
        if not pending or pending.get("segment_code") != seg_code:
            return False, "手工保存令牌无效或已完成", None
        with self._lock(seg_code):
            start, end = pending["start_ms"], pending["end_ms"]
            current_start = as_int(seg.get("起始毫秒"), start) or start
            current_end = as_int(seg.get("结束毫秒"), end) or end
            seg.setdefault("原始起始毫秒", str(current_start))
            seg.setdefault("原始结束毫秒", str(current_end))
            seg["起始毫秒"] = str(start)
            seg["结束毫秒"] = str(end)
            seg["边界来源"] = "人工"
            seg["boundary_source"] = "manual"
            seg["手工状态"] = "已保存"
            seg["manual_save_token"] = ""
            if pending.get("text") is not None:
                seg["段内文本"] = pending["text"]
                seg["人工确认字句"] = pending["text"]
            align = self.upsert_alignment(seg, start, end, "manual")
            self._tokens.pop(str(token), None)
            return True, f"分段 {seg_code} 的手工边界 {start}-{end} 已生效", dict(align)

    def save_manual_adjustment(
        self,
        segment_ref: Any,
        start_ms: Any = None,
        end_ms: Any = None,
        shift_ms: Any = None,
        text: str | None = None,
        confirmed: bool = True,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        ok, message, token = self.prepare_manual_adjustment(
            segment_ref, start_ms, end_ms, shift_ms, text,
        )
        if not ok or not token:
            return False, message, None
        ok, message, align = self.commit_manual_adjustment(segment_ref, token)
        if ok and align is not None and confirmed:
            seg = self.find_segment(segment_ref)
            if seg is not None:
                seg["字句确认"] = "是"
                seg["手工状态"] = "已确认"
                seg["status"] = "已对齐"
                seg["pending"] = False
        return ok, message, align

    # ---------- 自动校准与幂等/竞争控制 ----------

    def receive_auto_calibration(
        self,
        segment_ref: Any,
        start_ms: Any = None,
        end_ms: Any = None,
        offset_ms: Any = None,
        confidence: Any = None,
        calibration_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        seg = self.find_segment(segment_ref)
        if seg is None:
            return {"ok": False, "message": f"语音分段 {segment_ref} 不存在"}
        seg_code = self.segment_code(seg)

        current_start = as_int(seg.get("起始毫秒"), 0) or 0
        current_end = as_int(seg.get("结束毫秒"), current_start) or current_start
        start = as_int(start_ms, current_start)
        end = as_int(end_ms, current_end)
        if start is None or end is None or end <= start:
            return {"ok": False, "message": f"自动校准边界非法：{start}-{end}"}

        provided_key = str(
            idempotency_key
            or calibration_id
            or hashlib.md5(f"{seg_code}:{start}:{end}".encode()).hexdigest()[:16]
        )
        key = f"{seg_code}:{provided_key}"

        with self._lock(seg_code):
            if key in self._auto_keys:
                receipt = dict(self._auto_receipts.get(key, {}))
                receipt["duplicate"] = True
                receipt["ok"] = True
                receipt["message"] = f"分段 {seg_code} 的重复自动校准已幂等接收，未覆盖实际边界"
                align = self.find_alignment(seg_code)
                if align is not None:
                    count = as_int(align.get("自动校准重复次数"), 0) or 0
                    align["自动校准重复次数"] = str(count + 1)
                    receipt["entry"] = dict(align)
                receipt["effective_source"] = (
                    (align or {}).get("boundary_source")
                    or (align or {}).get("边界来源")
                    or seg.get("boundary_source")
                )
                return receipt

            self._auto_keys.add(key)
            manual_pending = str(seg.get("手工状态", "")) == "保存中"
            manual_effective = (
                str(seg.get("boundary_source", "")) == "manual"
                or str(seg.get("边界来源", "")) in {"manual", "人工", "手工"}
            )
            manual_wins = manual_pending or manual_effective

            candidate = {
                "start_ms": start,
                "end_ms": end,
                "offset_ms": as_int(offset_ms, 0) or 0,
                "confidence": confidence,
                "calibration_id": calibration_id,
                "idempotency_key": provided_key,
                "received_at": self._now(),
            }
            align = self.find_alignment(seg_code)
            if align is not None:
                align["自动校准候选"] = candidate
                align["幂等键"] = provided_key

            if manual_wins:
                effective_align = self.upsert_alignment(
                    seg,
                    as_int(align.get("实际起始毫秒") if align else None, current_start) or current_start,
                    as_int(align.get("实际结束毫秒") if align else None, current_end) or current_end,
                    "manual",
                    extra={"自动校准候选": candidate, "幂等键": provided_key},
                )
                result = {
                    "ok": True,
                    "duplicate": False,
                    "applied": False,
                    "manual_wins": True,
                    "idempotency_key": provided_key,
                    "effective_source": "manual",
                    "message": f"分段 {seg_code} 已有手工边界，自动校准仅作为候选保存",
                    "entry": dict(effective_align),
                }
            else:
                seg["起始毫秒"] = str(start)
                seg["结束毫秒"] = str(end)
                seg["边界来源"] = "自动"
                seg["boundary_source"] = "auto"
                effective_align = self.upsert_alignment(
                    seg, start, end, "auto",
                    offset_ms=as_int(offset_ms, 0) or 0,
                    extra={"幂等键": provided_key},
                )
                result = {
                    "ok": True,
                    "duplicate": False,
                    "applied": True,
                    "manual_wins": False,
                    "idempotency_key": provided_key,
                    "effective_source": "auto",
                    "message": f"分段 {seg_code} 已采用自动校准边界 {start}-{end}",
                    "entry": dict(effective_align),
                }
            self._auto_receipts[key] = {k: v for k, v in result.items() if k != "entry"}
            return result

    # ---------- 重算与交付 cues ----------

    def segments_for_pack(self, pack: dict[str, Any]) -> list[dict[str, Any]]:
        media_id = str(pack.get("关联媒体源", ""))
        task_ids: set[str] = set()
        explicit_task = pack.get("关联任务") or pack.get("task_id")
        if explicit_task:
            task_ids.add(str(explicit_task))

        if media_id:
            channels = [
                str(r.get("通道编号")) for r in store.rows("capture_channel")
                if str(r.get("关联媒体源")) == media_id
            ]
            shards = [
                str(r.get("分片编号")) for r in store.rows("media_shard")
                if str(r.get("关联通道")) in channels
            ]
            for task in store.rows("transcribe_task"):
                if str(task.get("关联分片")) in shards:
                    task_ids.add(str(task.get("任务编号")))

        all_segments = store.rows("speech_segment")
        if task_ids:
            scoped = [r for r in all_segments if str(r.get("关联任务")) in task_ids]
            if scoped:
                return scoped
        return list(all_segments)

    @staticmethod
    def _distribute_text(outputs: list[dict[str, Any]], segments: list[dict[str, Any]]) -> None:
        text_by_seg = {
            AlignmentWorkflow.segment_code(seg): str(
                seg.get("人工确认字句") or seg.get("段内文本") or ""
            )
            for seg in segments
        }
        grouped: dict[str, list[dict[str, Any]]] = {}
        for out in outputs:
            grouped.setdefault(out["seg_id"], []).append(out)
        for sid, items in grouped.items():
            full_text = text_by_seg.get(sid, "")
            if len(items) == 1:
                items[0]["text"] = full_text
                continue
            total_duration = sum(max(1, int(i["duration_ms"])) for i in items)
            length = len(full_text)
            cursor = 0
            for index, item in enumerate(items):
                if index == len(items) - 1:
                    piece = full_text[cursor:]
                else:
                    count = round(length * int(item["duration_ms"]) / total_duration)
                    count = max(1, min(length - cursor - (len(items) - index - 1), count)) if length else 0
                    piece = full_text[cursor:cursor + count]
                    cursor += count
                item["text"] = piece

    def build_cues(self, pack: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        segments = self.segments_for_pack(pack or {})
        inputs: list[dict[str, Any]] = []
        source_by_seg: dict[str, str] = {}
        for seg in segments:
            code = self.segment_code(seg)
            align = self.find_alignment(code)
            start = as_int(seg.get("起始毫秒"), 0) or 0
            end = as_int(seg.get("结束毫秒"), start) or start
            source = str(seg.get("boundary_source") or "")
            if align is not None:
                start = as_int(
                    align.get("effective_start_ms", align.get("实际起始毫秒", align.get("对齐起始毫秒"))),
                    start,
                ) or start
                end = as_int(
                    align.get("effective_end_ms", align.get("实际结束毫秒", align.get("对齐结束毫秒"))),
                    end,
                ) or end
                source = str(align.get("boundary_source") or align.get("边界来源") or source)
            if source in {"人工", "手工"}:
                source = "manual"
            elif source == "自动":
                source = "auto"
            source = source or "system"
            source_by_seg[code] = source
            cue_seed = dict(seg)
            cue_seed.update({
                "seg_id": code,
                "start_ms": start,
                "end_ms": end,
                "boundary_source": source,
            })
            inputs.append(cue_seed)

        resolved = resolve_boundaries(inputs)
        outputs = resolved["outputs"]
        self._distribute_text(outputs, segments)

        cues: list[dict[str, Any]] = []
        for index, out in enumerate(outputs, start=1):
            source = source_by_seg.get(out["seg_id"], out.get("boundary_source", "system"))
            text = str(out.get("text") or "").strip()
            if not text:
                continue
            cues.append({
                "index": len(cues) + 1,
                "start_ms": out["start_ms"],
                "end_ms": out["end_ms"],
                "duration_ms": out["duration_ms"],
                "text": text,
                "source_segment_id": out["source_segment_id"],
                "output_id": out["output_id"],
                "source": source,
                "boundary_source": source,
                "timecode": f"{timecode(out['start_ms'])} --> {timecode(out['end_ms'])}",
            })
        # 重新编号，避免空文本造成跳号。
        for i, cue in enumerate(cues, start=1):
            cue["index"] = i
        return cues

    @staticmethod
    def render_file(cues: list[dict[str, Any]], fmt: str) -> str:
        normalized = (fmt or "SRT").strip().lower()
        if normalized == "vtt" or normalized == "webvtt":
            blocks = ["WEBVTT", ""]
            for cue in cues:
                blocks.append(str(cue["index"]))
                blocks.append(
                    f"{timecode(cue['start_ms'], 'vtt')} --> {timecode(cue['end_ms'], 'vtt')}"
                )
                blocks.append(cue["text"])
                blocks.append("")
            return "\n".join(blocks).strip() + "\n"

        if normalized == "ass":
            lines = [
                "[Script Info]",
                "ScriptType: v4.00+",
                "",
                "[Events]",
                "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
            ]
            for cue in cues:
                lines.append(
                    "Dialogue: 0,"
                    f"{timecode(cue['start_ms'], 'ass')},{timecode(cue['end_ms'], 'ass')},"
                    f"Default,,0,0,0,,{cue['text']}"
                )
            return "\n".join(lines) + "\n"

        blocks = []
        for cue in cues:
            blocks.append(str(cue["index"]))
            blocks.append(f"{timecode(cue['start_ms'], 'srt')} --> {timecode(cue['end_ms'], 'srt')}")
            blocks.append(cue["text"])
            blocks.append("")
        return "\n".join(blocks).strip() + "\n"

    def retry_pack_generation(self, pack_ref: Any) -> tuple[bool, str, dict[str, Any] | None]:
        pack = self.find_row("subtitle_pack", pack_ref)
        if pack is None:
            return False, f"字幕包 {pack_ref} 不存在", None
        fmt = str(pack.get("导出格式") or "SRT")
        cues = self.build_cues(pack)
        content = self.render_file(cues, fmt)
        timecodes = [
            f"{timecode(cue['start_ms'], 'vtt' if fmt.lower() == 'webvtt' else 'srt')} --> "
            f"{timecode(cue['end_ms'], 'vtt' if fmt.lower() == 'webvtt' else 'srt')}"
            for cue in cues
        ]
        pack.update({
            "status": "可下载",
            "pending": False,
            "abnormal": False,
            "字幕行数": str(len(cues)),
            "生成时间": self._now(),
            "重试次数": str((as_int(pack.get("重试次数"), 0) or 0) + 1),
            "file_content": content,
            "_subtitle_content": content,
            "timecodes": timecodes,
            "cues": cues,
            "预览地址": f"/api/subtitle_pack/{pack.get('id')}/preview",
            "下载地址": f"/api/subtitle_pack/{pack.get('id')}/file",
        })
        return True, f"字幕包 {pack.get('字幕包编号')} 已按实际生效边界重新生成", dict(pack)

    def preview_pack(self, pack_ref: Any) -> dict[str, Any] | None:
        pack = self.find_row("subtitle_pack", pack_ref)
        if pack is None:
            return None
        content = pack.get("file_content") or pack.get("_subtitle_content")
        cues = pack.get("cues")
        if not content or cues is None:
            _, _, updated = self.retry_pack_generation(pack_ref)
            if updated is None:
                return None
            pack = updated
            content = pack.get("file_content")
            cues = pack.get("cues")
        return {
            "ok": True,
            "pack": {k: v for k, v in pack.items() if not k.startswith("_")},
            "format": pack.get("导出格式", "SRT"),
            "content": content,
            "cues": cues,
            "timecodes": pack.get("timecodes", []),
        }

    def provenance(self, segment_ref: Any) -> dict[str, Any] | None:
        seg = self.find_segment(segment_ref)
        if seg is None:
            return None
        seg_code = self.segment_code(seg)
        alignments = [
            dict(row) for row in store.rows("subtitle_align")
            if str(row.get("关联分段")) == seg_code
        ]
        outputs = resolve_boundaries(store.rows("speech_segment"))["outputs"]
        related_outputs = [o for o in outputs if o["source_segment_id"] == seg_code]
        packs = []
        for pack in store.rows("subtitle_pack"):
            cues = pack.get("cues") or []
            matching_cues = [
                cue for cue in cues
                if str(cue.get("source_segment_id")) == seg_code
            ]
            if matching_cues:
                packs.append({
                    "字幕包编号": pack.get("字幕包编号"),
                    "id": pack.get("id"),
                    "status": pack.get("status"),
                    "timecodes": [cue.get("timecode") for cue in matching_cues],
                    "cues": matching_cues,
                    "file_content": pack.get("file_content"),
                    "download_url": f"/api/subtitle_pack/{pack.get('id')}/file",
                })
        return {
            "ok": True,
            "segment": dict(seg),
            "alignments": alignments,
            "outputs": related_outputs,
            "packs": packs,
        }

    def bootstrap_existing_rows(self) -> None:
        """给种子数据补齐“实际边界/来源”字段，避免列表出现空来源。"""
        for seg in store.rows("speech_segment"):
            if not seg.get("boundary_source"):
                method = str(seg.get("对齐方式", ""))
                if "人工" in method or "手工" in method:
                    seg["boundary_source"] = "manual"
                    seg["边界来源"] = "人工"
                elif "自动" in method:
                    seg["boundary_source"] = "auto"
                    seg["边界来源"] = "自动"
                else:
                    seg["boundary_source"] = "system"
                    seg.setdefault("边界来源", "系统")
                seg.setdefault("手工状态", "未调整")
                seg.setdefault("原分段编号", self.segment_code(seg))

        for row in store.rows("subtitle_align"):
            start = as_int(row.get("实际起始毫秒", row.get("对齐起始毫秒")), 0) or 0
            end = as_int(row.get("实际结束毫秒", row.get("对齐结束毫秒")), start) or start
            method = str(row.get("对齐方式", ""))
            if "人工" in method or "手工" in method:
                source, source_value = "manual", "人工"
            elif "自动" in method:
                source, source_value = "auto", "自动"
            else:
                source, source_value = "system", "系统"
            row.setdefault("实际起始毫秒", str(start))
            row.setdefault("实际结束毫秒", str(end))
            row.setdefault("实际使用边界", f"{start}-{end}")
            row.setdefault("边界来源", source_value)
            row.setdefault("来源", SourceLabel[source])
            row.setdefault("boundary_source", source)
            row.setdefault("实际使用", "是")
            row.setdefault("原分段编号", row.get("关联分段"))
            row.setdefault("effective_start_ms", start)
            row.setdefault("effective_end_ms", end)

    def reproduce_boundary_case(self) -> dict[str, Any]:
        """创建稳定的 0-1000ms 例题，便于页面和接口复现。"""
        task_id = "TRCB-BOUNDARY"
        specs = [
            ("SPCH-B-LONG", 0, 1000, "我们先讨论整体框架再安排后续发布", "无"),
            ("SPCH-B-INTERJECT", 100, 200, "这里插一句", "内嵌插话"),
            ("SPCH-B-NEXT", 1000, 1500, "接下来是单独口播", "无"),
        ]
        for code, start, end, text, mark in specs:
            existing = self.find_segment(code)
            if existing is None:
                row = {
                    "id": self._next_id("speech_segment"),
                    "status": "待对齐",
                    "pending": True,
                    "abnormal": mark != "无",
                    "分段编号": code,
                    "关联任务": task_id,
                    "起始毫秒": str(start),
                    "结束毫秒": str(end),
                    "段内文本": text,
                    "段置信度": "0.99",
                    "静音前毫秒": "0",
                    "重叠标记": mark,
                    "边界来源": "segment",
                    "手工状态": "未调整",
                    "原分段编号": code,
                }
                store.rows("speech_segment").append(row)
            else:
                existing.update({"起始毫秒": str(start), "结束毫秒": str(end)})

        pack_code = "SBPK-BOUNDARY"
        if self.find_row("subtitle_pack", pack_code) is None:
            store.rows("subtitle_pack").append({
                "id": self._next_id("subtitle_pack"),
                "status": "待生成",
                "pending": True,
                "abnormal": False,
                "字幕包编号": pack_code,
                "关联媒体源": "MEDI-BOUNDARY",
                "关联任务": task_id,
                "绑定版本": "v-boundary",
                "字幕行数": "0",
                "覆盖时长秒": "2",
                "导出格式": "SRT",
                "下载次数": "0",
                "生成时间": "-",
            })
        resolved = resolve_boundaries([
            seg for seg in store.rows("speech_segment")
            if seg.get("关联任务") == task_id
        ])
        return {"ok": True, "task_id": task_id, **resolved}


workflow = AlignmentWorkflow()
workflow.bootstrap_existing_rows()
