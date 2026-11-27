"""边界修复、手工/自动校准竞争、字幕预览与文件下载的专用工作流接口。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import Response

from app.services.alignment_workflow import timecode, workflow
from app.services.boundary_engine import as_int, first_value, resolve_boundaries
from app.store import store

router = APIRouter(prefix="/api", tags=["对齐交付工作流"])


def _values(body: dict[str, Any]) -> dict[str, Any]:
    values = dict(body.get("values") or {})
    for key, value in body.items():
        if key != "values":
            values.setdefault(key, value)
    return values


@router.post("/workflow/reproduce-boundary-case")
def reproduce_boundary_case() -> dict[str, Any]:
    return workflow.reproduce_boundary_case()


@router.post("/workflow/resolve-boundaries")
def resolve_boundaries_endpoint(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    segments = body.get("segments") if isinstance(body.get("segments"), list) else []
    return resolve_boundaries(segments)


@router.post("/speech_segment/recalculate")
def recalculate_speech_segments(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    if isinstance(body.get("segments"), list):
        return resolve_boundaries(body["segments"])
    task_id = body.get("task_id") or body.get("关联任务")
    if not task_id:
        raise HTTPException(status_code=400, detail="需要 task_id 或 segments")
    rows = [r for r in store.rows("speech_segment") if str(r.get("关联任务")) == str(task_id)]
    result = resolve_boundaries(rows)
    result["task_id"] = task_id
    return result


@router.post("/workflow/speech-segments/{segment_ref}/manual/prepare")
def prepare_manual(segment_ref: str, body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    values = _values(body)
    ok, message, token = workflow.prepare_manual_adjustment(
        segment_ref,
        start_ms=first_value(values, "start_ms", "start", "起始毫秒", "new_start", "new_start_ms"),
        end_ms=first_value(values, "end_ms", "end", "结束毫秒", "new_end", "new_end_ms"),
        shift_ms=first_value(values, "shift_ms", "后移毫秒", "shift"),
        text=first_value(values, "text", "段内文本"),
        token=values.get("token"),
    )
    if not ok:
        return {"ok": False, "message": message}
    return {"ok": True, "message": message, "save_token": token, "token": token}


@router.post("/workflow/speech-segments/{segment_ref}/manual/commit")
def commit_manual(segment_ref: str, body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    token = str(body.get("save_token") or body.get("token") or "")
    ok, message, entry = workflow.commit_manual_adjustment(segment_ref, token)
    return {"ok": ok, "message": message, "entry": entry}


@router.post("/workflow/speech-segments/{segment_ref}/manual")
def save_manual(segment_ref: str, body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    values = _values(body)
    ok, message, entry = workflow.save_manual_adjustment(
        segment_ref,
        start_ms=first_value(values, "start_ms", "start", "起始毫秒", "new_start", "new_start_ms"),
        end_ms=first_value(values, "end_ms", "end", "结束毫秒", "new_end", "new_end_ms"),
        shift_ms=first_value(values, "shift_ms", "后移毫秒", "shift"),
        text=first_value(values, "text", "段内文本", "confirmed_text"),
        confirmed=bool(values.get("confirmed", values.get("确认字句", True))),
    )
    return {"ok": ok, "message": message, "entry": entry}


@router.post("/workflow/speech-segments/{segment_ref}/auto")
def receive_auto(segment_ref: str, body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    values = _values(body)
    return workflow.receive_auto_calibration(
        segment_ref,
        start_ms=first_value(values, "start_ms", "start", "起始毫秒", "对齐起始毫秒", "new_start"),
        end_ms=first_value(values, "end_ms", "end", "结束毫秒", "对齐结束毫秒", "new_end"),
        offset_ms=first_value(values, "offset_ms", "偏移毫秒", "new_offset"),
        confidence=values.get("confidence"),
        calibration_id=first_value(values, "calibration_id", "result_id"),
        idempotency_key=first_value(values, "idempotency_key", "key", "request_id"),
    )


@router.post("/workflow/auto-calibration")
def receive_auto_by_body(body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    segment_ref = body.get("segment_id") or body.get("segment_ref") or body.get("关联分段")
    if not segment_ref:
        raise HTTPException(status_code=400, detail="需要 segment_id 或 关联分段")
    return receive_auto(str(segment_ref), body)


@router.get("/workflow/provenance/{segment_ref}")
def provenance(segment_ref: str) -> dict[str, Any]:
    result = workflow.provenance(segment_ref)
    if result is None:
        raise HTTPException(status_code=404, detail=f"分段 {segment_ref} 不存在")
    return result


@router.get("/subtitle_align/effective")
def effective_alignments() -> dict[str, Any]:
    rows = [
        row for row in store.rows("subtitle_align")
        if str(row.get("实际使用", "是")) != "否"
    ]
    return {"items": rows, "total": len(rows)}


@router.post("/subtitle_pack/{pack_ref}/retry")
def retry_pack(pack_ref: str) -> dict[str, Any]:
    ok, message, entry = workflow.retry_pack_generation(pack_ref)
    return {"ok": ok, "message": message, "entry": entry}


@router.get("/subtitle_pack/{pack_ref}/preview")
def preview_pack(pack_ref: str) -> dict[str, Any]:
    result = workflow.preview_pack(pack_ref)
    if result is None:
        raise HTTPException(status_code=404, detail=f"字幕包 {pack_ref} 不存在")
    return result


@router.get("/subtitle_pack/{pack_ref}/file")
@router.get("/subtitle_pack/{pack_ref}/download")
@router.get("/subtitle_pack/{pack_ref}/content")
def pack_file(
    pack_ref: str,
    download: int = Query(default=1),
    format: str | None = Query(default=None),
) -> Response:
    result = workflow.preview_pack(pack_ref)
    if result is None:
        raise HTTPException(status_code=404, detail=f"字幕包 {pack_ref} 不存在")
    pack = result["pack"]
    fmt = (format or pack.get("导出格式") or "SRT").lower()
    if format and format.lower() != str(pack.get("导出格式", "")).lower():
        content = workflow.render_file(result["cues"], format)
        fmt = format.lower()
    else:
        content = result["content"]
        fmt = str(pack.get("导出格式", "srt")).lower()

    ext = {"webvtt": "vtt", "vtt": "vtt", "ass": "ass"}.get(fmt, "srt")
    media_type = {
        "vtt": "text/vtt; charset=utf-8",
        "ass": "text/plain; charset=utf-8",
    }.get(ext, "application/x-subrip; charset=utf-8")
    filename = f"{pack.get('字幕包编号', pack_ref)}.{ext}"
    disposition = "attachment" if download else "inline"
    headers = {"Content-Disposition": f'{disposition}; filename="{filename}"'}
    if download:
        row = workflow.find_row("subtitle_pack", pack_ref)
        if row is not None:
            row["下载次数"] = str((as_int(row.get("下载次数"), 0) or 0) + 1)
    return Response(content=content, media_type=media_type, headers=headers)


@router.get("/workflow/timecode/{ms}")
def timecode_preview(ms: int, format: str = "srt") -> dict[str, str]:
    return {"ms": str(ms), "timecode": timecode(ms, format)}
