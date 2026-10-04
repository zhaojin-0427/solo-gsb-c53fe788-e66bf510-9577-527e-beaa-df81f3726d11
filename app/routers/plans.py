"""编排预览、原子发布、已发布地址表查询与 CSV 下载。"""
from __future__ import annotations

import csv
import io

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from .. import planner
from ..database import get_db
from ..services import get_config_version

router = APIRouter(prefix="/api/plans", tags=["plans"])


@router.get("/version")
def version(db: Session = Depends(get_db)):
    latest = planner.latest_published(db)
    return {
        "config_version": get_config_version(db),
        "latest_published_plan_id": latest.id if latest else None,
        "latest_published_at": latest.published_at if latest else None,
    }


@router.post("/preview")
def preview(db: Session = Depends(get_db)):
    return planner.create_preview(db)


@router.get("/{plan_id}")
def get_plan(plan_id: int, db: Session = Depends(get_db)):
    return planner.get_plan_payload(db, plan_id)


@router.post("/{plan_id}/publish")
def publish(plan_id: int, db: Session = Depends(get_db)):
    return planner.publish_plan(db, plan_id)


@router.get("/published/latest")
def latest(db: Session = Depends(get_db)):
    plan = planner.latest_published(db)
    if plan is None:
        return {"has_published": False}
    payload = planner.get_published_payload(db, plan.id)
    payload["has_published"] = True
    return payload


@router.get("/published/{plan_id}/csv")
def published_csv(plan_id: int, db: Session = Depends(get_db)):
    # 与已发布查询同一恢复路径：有快照时地址/名称/锁定/迁移均为发布时事实；
    # 旧库无快照时回退到方案记录，无法还原的字段不伪装成历史。
    payload = planner.get_published_payload(db, plan_id)

    buf = io.StringIO()
    buf.write("﻿")
    writer = csv.writer(buf)
    writer.writerow(["灯具ID", "灯具名称", "线路", "起始地址", "结束地址", "通道数", "是否迁移", "是否锁定"])
    for a in payload["allocations"]:
        if payload.get("snapshot_available", False):
            locked_cell = "是" if a["locked"] else "否"
        else:
            locked_cell = "是" if a.get("locked") is True else "否" if a.get("locked") is False else "未知"
        writer.writerow(
            [
                a["fixture_id"],
                a["name"],
                a["line_id"],
                a["start"],
                a["end"],
                a["channels"],
                "是" if a["migrated"] else "否",
                locked_cell,
            ]
        )
    if not payload.get("snapshot_available", False) and payload.get("fallback_note"):
        writer.writerow([])
        writer.writerow([payload["fallback_note"]])
    buf.seek(0)
    filename = f"dmx_addresses_plan{plan_id}_v{payload['config_version']}.csv"
    return StreamingResponse(
        buf,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
