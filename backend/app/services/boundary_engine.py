"""毫秒边界唯一归属算法。

时间区间统一按半开区间 ``[start_ms, end_ms)`` 处理：相邻输出允许
``prev.end_ms == next.start_ms``，但不允许真正重叠。这样既不会让两个字幕
条目占用同一毫秒，也保留了毫秒刻度上的首尾相接。

内嵌插话（例如长段 [0, 1000] 中的 [100, 200]）不能用两个起点和长段结尾
取中点：那会得到 [550, 200] 这类 start > end 的非法区间。包围关系需要把
外层段切成插话左右两段，而不是把插话起点推到外层结尾附近。
"""
from __future__ import annotations

from typing import Any, Iterable

Segment = dict[str, Any]
Part = list[int]


def as_int(value: Any, default: int | None = None) -> int | None:
    """宽容地把种子数据/请求中的毫秒值转成 int。"""
    if value is None or value == "":
        return default
    try:
        if isinstance(value, str):
            value = value.strip()
        return int(float(value))
    except (TypeError, ValueError):
        return default


def first_value(data: dict[str, Any], *keys: str) -> Any:
    """按候选键名取第一个非空值，兼容不同调用方的命名习惯。"""
    for key in keys:
        value = data.get(key)
        if value is not None and value != "":
            return value
    return None


def segment_id(seg: Segment) -> str:
    value = (
        seg.get("seg_id")
        or seg.get("segment_id")
        or seg.get("source_segment_id")
        or seg.get("分段编号")
        or seg.get("id")
    )
    return str(value)


def segment_bounds(seg: Segment) -> tuple[int, int] | None:
    start = as_int(
        seg.get("start_ms", seg.get("start", seg.get("起始毫秒"))),
    )
    end = as_int(
        seg.get("end_ms", seg.get("end", seg.get("结束毫秒"))),
    )
    if start is None or end is None:
        return None
    return start, end


def _subtract_interval(parts: Iterable[Part], cut_start: int, cut_end: int) -> list[Part]:
    """从一组区间中挖去 [cut_start, cut_end)。"""
    result: list[Part] = []
    for start, end in parts:
        if cut_end <= start or cut_start >= end:
            result.append([start, end])
            continue
        if cut_start > start:
            left_end = min(cut_start, end)
            if left_end > start:
                result.append([start, left_end])
        if cut_end < end:
            right_start = max(cut_end, start)
            if end > right_start:
                result.append([right_start, end])
    return result


def _clip_to_left(parts: Iterable[Part], boundary: int) -> list[Part]:
    return _subtract_interval(parts, boundary, 10**18)


def _clip_to_right(parts: Iterable[Part], boundary: int) -> list[Part]:
    return _subtract_interval(parts, -10**18, boundary)


def _priority(seg: Segment) -> int:
    source = str(
        seg.get("boundary_source")
        or seg.get("边界来源")
        or seg.get("source")
        or seg.get("对齐方式")
        or ""
    )
    if "manual" in source.casefold() or "人工" in source or "手工" in source:
        return 100
    if "confirmed" in source.casefold() or "确认" in source:
        return 80
    return int(as_int(seg.get("priority"), 0) or 0)


def resolve_boundaries(segments: list[Segment]) -> dict[str, Any]:
    """把可能重叠/包围的语音段解析成正时长、互不重叠的输出区间。

    返回值同时包含：
    - ``outputs``：可直接生成字幕的扁平区间；
    - ``segments``：输入段的副本，并尽量写回 align_start/align_end；
    - ``boundaries``：每一对重叠的处理记录；
    - ``unique_boundaries``：所有输出分界毫秒。
    """
    normalized: list[Segment] = []
    invalid: list[dict[str, Any]] = []

    for raw in segments:
        seg = dict(raw)
        sid = segment_id(seg)
        bounds = segment_bounds(seg)
        if not sid or bounds is None:
            invalid.append({"segment": seg, "reason": "缺少编号或毫秒边界"})
            continue
        start, end = bounds
        if end <= start:
            invalid.append({
                "seg_id": sid,
                "start_ms": start,
                "end_ms": end,
                "reason": "结束毫秒必须大于起始毫秒",
            })
            continue
        seg["seg_id"] = sid
        seg["start_ms"] = start
        seg["end_ms"] = end
        normalized.append(seg)

    # 稳定排序：先按起点，长段在前有助于识别包围关系。
    normalized.sort(key=lambda s: (s["start_ms"], -(s["end_ms"] - s["start_ms"]), s["seg_id"]))

    parts: dict[str, list[Part]] = {
        seg["seg_id"]: [[seg["start_ms"], seg["end_ms"]]] for seg in normalized
    }
    boundary_records: list[dict[str, Any]] = []

    for i, a in enumerate(normalized):
        for b in normalized[i + 1:]:
            a_start, a_end = a["start_ms"], a["end_ms"]
            b_start, b_end = b["start_ms"], b["end_ms"]
            overlap = min(a_end, b_end) - max(a_start, b_start)
            if overlap <= 0:
                continue

            a_contains_b = (
                a_start <= b_start
                and b_end <= a_end
                and (b_end - b_start) < (a_end - a_start)
            )
            b_contains_a = (
                b_start <= a_start
                and a_end <= b_end
                and (a_end - a_start) < (b_end - b_start)
            )

            if a_contains_b:
                # 外层 a 挖掉内嵌 b，形成左右两段；b 完整保留。
                parts[a["seg_id"]] = _subtract_interval(
                    parts[a["seg_id"]], b_start, b_end,
                )
                boundary_records.append({
                    "segment_a": a["seg_id"],
                    "segment_b": b["seg_id"],
                    "rule": "enclosed_split",
                    "boundary_ms": b_start,
                    "start_boundary_ms": b_start,
                    "end_boundary_ms": b_end,
                    "cut_points": [b_start, b_end],
                    "inner_start_ms": b_start,
                    "inner_end_ms": b_end,
                })
                continue

            if b_contains_a:
                parts[b["seg_id"]] = _subtract_interval(
                    parts[b["seg_id"]], a_start, a_end,
                )
                boundary_records.append({
                    "segment_a": b["seg_id"],
                    "segment_b": a["seg_id"],
                    "rule": "enclosed_split",
                    "boundary_ms": a_start,
                    "start_boundary_ms": a_start,
                    "end_boundary_ms": a_end,
                    "cut_points": [a_start, a_end],
                    "inner_start_ms": a_start,
                    "inner_end_ms": a_end,
                })
                continue

            # 普通交叉重叠：在真实重叠窗口中点分界，而不是用某一段的远端。
            boundary_ms = (max(a_start, b_start) + min(a_end, b_end)) // 2
            if a_start <= b_start:
                early, late = a, b
            else:
                early, late = b, a
            parts[early["seg_id"]] = _clip_to_left(parts[early["seg_id"]], boundary_ms)
            parts[late["seg_id"]] = _clip_to_right(parts[late["seg_id"]], boundary_ms)
            boundary_records.append({
                "segment_a": early["seg_id"],
                "segment_b": late["seg_id"],
                "rule": "midpoint",
                "boundary_ms": boundary_ms,
            })

    outputs: list[dict[str, Any]] = []
    for seg in normalized:
        sid = seg["seg_id"]
        seg_parts = sorted(parts.get(sid, []))
        for index, (start, end) in enumerate(seg_parts, start=1):
            if end <= start:
                continue
            output_id = sid if len(seg_parts) == 1 else f"{sid}#L{index}"
            text = str(seg.get("text", seg.get("段内文本", "")) or "")
            outputs.append({
                "output_id": output_id,
                "seg_id": sid,
                "source_segment_id": sid,
                "original_start_ms": seg["start_ms"],
                "original_end_ms": seg["end_ms"],
                "start_ms": start,
                "end_ms": end,
                "duration_ms": end - start,
                "part_index": index,
                "part_count": len(seg_parts),
                "text": text if len(seg_parts) == 1 else "",
                "full_text": text,
                "boundary_source": seg.get("boundary_source", seg.get("边界来源", "segment")),
            })

    outputs.sort(key=lambda item: (item["start_ms"], item["end_ms"], item["seg_id"]))

    # 最终不变量校验。若极端多段冲突仍产生重叠，按高优先级保留并裁剪后者。
    repaired: list[dict[str, Any]] = []
    for item in outputs:
        start, end = item["start_ms"], item["end_ms"]
        if repaired and start < repaired[-1]["end_ms"]:
            if _priority(item) > _priority(repaired[-1]):
                prev = repaired[-1]
                prev["end_ms"] = start
                prev["duration_ms"] = max(0, start - prev["start_ms"])
                if prev["duration_ms"] <= 0:
                    repaired.pop()
            else:
                start = repaired[-1]["end_ms"]
                item["start_ms"] = start
                item["duration_ms"] = end - start
        if end > start:
            repaired.append(item)
    outputs = repaired

    unique_boundaries = sorted({
        value for item in outputs for value in (item["start_ms"], item["end_ms"])
    })

    # 给每个输入段写回一个可用的 align 区间；多段包围拆分时通过 align_ranges 暴露全部。
    output_by_seg: dict[str, list[dict[str, Any]]] = {}
    for item in outputs:
        output_by_seg.setdefault(item["seg_id"], []).append(item)

    result_segments: list[dict[str, Any]] = []
    for seg in normalized:
        sid = seg["seg_id"]
        seg_outputs = output_by_seg.get(sid, [])
        seg["align_ranges"] = [
            [item["start_ms"], item["end_ms"]] for item in seg_outputs
        ]
        if seg_outputs:
            first = seg_outputs[0]
            seg["align_start"] = first["start_ms"]
            # 单段时写真实结尾；多段时写最后一段结尾，完整区间见 align_ranges/outputs。
            seg["align_end"] = seg_outputs[-1]["end_ms"]
            seg["duration_ms"] = seg["align_end"] - seg["align_start"]
        else:
            seg["align_start"] = seg["start_ms"]
            seg["align_end"] = seg["end_ms"]
            seg["duration_ms"] = seg["end_ms"] - seg["start_ms"]
        result_segments.append(seg)

    return {
        "ok": not invalid,
        "invalid": invalid,
        "segments": result_segments,
        "outputs": outputs,
        "boundaries": boundary_records,
        "unique_boundaries": unique_boundaries,
    }


def assert_positive_nonoverlapping(outputs: list[dict[str, Any]]) -> None:
    """测试和生成字幕前使用的不变量断言。"""
    previous_end: int | None = None
    for item in outputs:
        start = int(item["start_ms"])
        end = int(item["end_ms"])
        if end <= start:
            raise ValueError(f"输出 {item.get('output_id')} 时长非正：{start}-{end}")
        if previous_end is not None and start < previous_end:
            raise ValueError(f"输出 {item.get('output_id')} 与前一输出重叠")
        previous_end = end
