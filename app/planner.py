"""编排生成、发布与发布快照构建。"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session

from . import solver
from .models import (
    Fixture,
    FixtureMode,
    FixtureType,
    Line,
    Plan,
    PlanAllocation,
    PublishedSnapshot,
    Reservation,
)
from .services import bump_config_version, get_config_version


def build_spec(db: Session) -> solver.SolveSpec:
    lines = [
        solver.LineSpec(id=l.id, name=l.name, active=l.is_active)
        for l in db.query(Line).order_by(Line.id).all()
    ]
    reservations = [
        solver.ReservationSpec(id=r.id, line_id=r.line_id, start=r.start, end=r.end, reason=r.reason)
        for r in db.query(Reservation).order_by(Reservation.id).all()
    ]
    rows = (
        db.query(Fixture, FixtureMode, FixtureType)
        .join(FixtureMode, Fixture.mode_id == FixtureMode.id)
        .join(FixtureType, FixtureMode.type_id == FixtureType.id)
        .order_by(Fixture.id)
        .all()
    )
    fixtures = [
        solver.FixtureSpec(
            id=fx.id,
            name=fx.name,
            channels=mode.channels,
            prev_line=fx.previous_line,
            prev_start=fx.previous_start,
            locked=fx.locked,
        )
        for fx, mode, _t in rows
    ]
    return solver.SolveSpec(fixtures=fixtures, reservations=reservations, lines=lines)


def create_preview(db: Session) -> dict:
    spec = build_spec(db)
    result = solver.solve(spec)
    version = get_config_version(db)
    if result.optimality == solver.INFEASIBLE:
        return {
            "feasible": False,
            "optimality_status": solver.INFEASIBLE,
            "config_version": version,
            "conflicts": result.conflicts,
            "occupancy": [],
        }

    if not result.is_optimal:
        # 未证最优：绝不生成可发布草稿。是否取得完整可用排布决定 feasible——
        # 有候选则 feasible=True 供参考展示；空候选（UNKNOWN/候选读取失败/
        # 诊断超时）则 feasible=False，仅表示暂无可展示排布，不代表已证无解。
        return _unproven_payload(db, result, version)

    plan = Plan(
        config_version=version,
        status="draft",
        optimality_status=solver.OPTIMAL,
        migration_count=result.migration_count,
        used_lines=result.used_lines,
    )
    db.add(plan)
    db.flush()
    for a in result.allocations:
        db.add(
            PlanAllocation(
                plan_id=plan.id,
                fixture_id=a.fixture_id,
                line_id=a.line_id,
                start=a.start,
                end=a.end,
                migrated=a.migrated,
            )
        )
    db.commit()
    db.refresh(plan)
    return _plan_payload(db, plan, feasible=True)


def _unproven_payload(db: Session, result: solver.SolveResult, version: int) -> dict:
    """未证最优时返回的预览（无 plan_id，不可发布）。

    feasible 按是否取得完整可用排布裁决：有候选时为 True（仅供查看）；
    空候选（求解 UNKNOWN、候选读取失败或冲突诊断超时）时为 False，
    仍为 HTTP 200 并携带 unproven 状态与阶段信息，不当作已证无解。
    """
    has_candidate = result.feasible
    names = {f.id: f.name for f in db.query(Fixture).all()}
    locked_ids = {f.id for f in db.query(Fixture).filter(Fixture.locked.is_(True)).all()}
    prev = {f.id: (f.previous_line, f.previous_start, f.locked) for f in db.query(Fixture).all()}
    alloc_out = [
        {
            "fixture_id": a.fixture_id,
            "name": names.get(a.fixture_id, f"#{a.fixture_id}"),
            "line_id": a.line_id,
            "start": a.start,
            "end": a.end,
            "channels": a.channels,
            "migrated": a.migrated,
            "locked": a.fixture_id in locked_ids,
        }
        for a in result.allocations
    ]
    migrations = []
    for a in alloc_out:
        old_line, old_start, _locked = prev.get(a["fixture_id"], (None, None, False))
        if old_line is not None and (old_line, old_start) != (a["line_id"], a["start"]):
            migrations.append(
                {
                    "fixture_id": a["fixture_id"],
                    "name": a["name"],
                    "from_line": old_line,
                    "from_start": old_start,
                    "to_line": a["line_id"],
                    "to_start": a["start"],
                    "locked": a["fixture_id"] in locked_ids,
                }
            )
    return {
        "feasible": has_candidate,
        "optimality_status": solver.UNPROVEN,
        "unproven_stage": result.unproven_stage,
        "message": result.unproven_message,
        "config_version": version,
        "migration_count": result.migration_count,
        "used_lines": result.used_lines,
        "allocations": alloc_out,
        "migrations": migrations,
        "occupancy": result.occupancy,
        "conflicts": result.conflicts,
    }


def _plan_payload(db: Session, plan: Plan, *, feasible: bool = True) -> dict:
    allocs = (
        db.query(PlanAllocation)
        .filter(PlanAllocation.plan_id == plan.id)
        .order_by(PlanAllocation.fixture_id)
        .all()
    )
    names = {f.id: f.name for f in db.query(Fixture).all()}
    locked_ids = {f.id for f in db.query(Fixture).filter(Fixture.locked.is_(True)).all()}
    alloc_out = [
        {
            "fixture_id": a.fixture_id,
            "name": names.get(a.fixture_id, f"#{a.fixture_id}"),
            "line_id": a.line_id,
            "start": a.start,
            "end": a.end,
            "channels": a.end - a.start + 1,
            "migrated": a.migrated,
            "locked": a.fixture_id in locked_ids,
        }
        for a in allocs
    ]
    return {
        "feasible": feasible,
        "id": plan.id,
        "plan_id": plan.id,
        "config_version": plan.config_version,
        "status": plan.status,
        "optimality_status": plan.optimality_status,
        "migration_count": plan.migration_count,
        "used_lines": plan.used_lines,
        "created_at": plan.created_at,
        "allocations": alloc_out,
        "migrations": _migrations_from_alloc(db, alloc_out),
        "occupancy": _occupancy_for(db, allocs),
        "conflicts": [],
    }


def _migrations_from_alloc(db: Session, alloc_out: list[dict]) -> list[dict]:
    prev = {f.id: (f.previous_line, f.previous_start, f.locked) for f in db.query(Fixture).all()}
    items = []
    for a in alloc_out:
        old_line, old_start, locked = prev.get(a["fixture_id"], (None, None, False))
        # 从未配置过的新灯具不算迁移
        if old_line is not None and (old_line, old_start) != (a["line_id"], a["start"]):
            items.append(
                {
                    "fixture_id": a["fixture_id"],
                    "name": a["name"],
                    "from_line": old_line,
                    "from_start": old_start,
                    "to_line": a["line_id"],
                    "to_start": a["start"],
                    "locked": locked,
                }
            )
    return items


def _occupancy_for(db: Session, allocs: list[PlanAllocation]) -> list[dict]:
    lines = db.query(Line).order_by(Line.id).all()
    reservations = db.query(Reservation).all()
    names = {f.id: f.name for f in db.query(Fixture).all()}
    locked_ids = {f.id for f in db.query(Fixture).filter(Fixture.locked.is_(True)).all()}
    blocks: dict[int, list[dict]] = {l.id: [] for l in lines}
    for a in allocs:
        blocks[a.line_id].append(
            {
                "fixture_id": a.fixture_id,
                "name": names.get(a.fixture_id, f"#{a.fixture_id}"),
                "start": a.start,
                "end": a.end,
                "channels": a.end - a.start + 1,
                "migrated": a.migrated,
                "locked": a.fixture_id in locked_ids,
            }
        )
    out = []
    for line in lines:
        res = [
            {"id": r.id, "start": r.start, "end": r.end, "reason": r.reason}
            for r in sorted((r for r in reservations if r.line_id == line.id), key=lambda r: r.start)
        ]
        occupied: set[int] = set()
        for b in blocks[line.id]:
            occupied.update(range(b["start"], b["end"] + 1))
        for r in res:
            occupied.update(range(r["start"], r["end"] + 1))
        out.append(
            {
                "line_id": line.id,
                "name": line.name,
                "is_active": line.is_active,
                "size": solver.LINE_SIZE,
                "used_channels": len(occupied),
                "free_channels": solver.LINE_SIZE - len(occupied),
                "fixtures": sorted(blocks[line.id], key=lambda b: b["start"]),
                "reservations": res,
            }
        )
    return out


def get_plan_payload(db: Session, plan_id: int) -> dict:
    plan = db.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="预览不存在（可能已被新配置覆盖）")
    # 已发布方案必须从发布时快照恢复；草稿/已作废草稿仍按当前配置的预览路径查询。
    if plan.status == "published":
        return _published_payload(db, plan)
    return _plan_payload(db, plan, feasible=True)


def latest_published(db: Session) -> Plan | None:
    return (
        db.query(Plan)
        .filter(Plan.status == "published")
        .order_by(Plan.published_at.desc(), Plan.id.desc())
        .first()
    )


def get_published_payload(db: Session, plan_id: int) -> dict:
    plan = db.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="预览不存在（可能已被新配置覆盖）")
    if plan.status != "published":
        raise HTTPException(status_code=404, detail="该方案尚未发布")
    return _published_payload(db, plan)


# 旧库中早于快照功能上线、无法还原发布时明细时给出的统一说明。
LEGACY_PUBLISHED_NOTE = (
    "该发布记录早于快照功能上线，数据库中没有发布时快照：地址与逐台迁移标记取自方案记录，"
    "灯具名称与锁定状态为查询时的当前值，迁移明细与发布时线路占用已无法还原。"
)


def _published_payload(db: Session, plan: Plan) -> dict:
    """从发布时快照恢复已发布地址表。

    快照在发布事务内、回写既有地址之前构建，因此名称、锁定、迁移明细与占用都是
    发布当时的事实；读取时状态与原配置版本仍以方案记录为准——快照构建于状态翻转
    之前，不能把快照里发布前的 draft 状态直接返回。
    """
    snap = (
        db.query(PublishedSnapshot)
        .filter(PublishedSnapshot.plan_id == plan.id)
        .order_by(PublishedSnapshot.id.desc())
        .first()
    )
    if snap is None:
        return _legacy_published_payload(db, plan)

    payload = json.loads(snap.payload)
    payload["status"] = "published"
    payload["config_version"] = plan.config_version
    payload["published_at"] = plan.published_at
    payload["snapshot_available"] = True
    payload.setdefault("migrations_available", True)
    payload.setdefault("occupancy_available", True)
    for a in payload.get("allocations", []):
        # 快照中的名称/锁定即发布时事实；兼容更早版本写入、未带标记的快照。
        a.setdefault("name_historical", True)
        a.setdefault("locked_historical", True)
    return payload


def _legacy_published_payload(db: Session, plan: Plan) -> dict:
    """旧库兼容：没有快照的已发布记录只从方案与分配行尽力恢复。

    PlanAllocation 中的线路/起止地址与逐台 migrated 是真实保留的发布时事实；
    名称与锁定只能取当前灯具值并显式标记非历史（灯具已删除则名称回退为 #id、
    锁定未知）；迁移明细（发布前的既有地址）与发布时线路占用没有任何记录可还原，
    一律置空并给出不可用标记，绝不伪装成历史事实。
    """
    allocs = (
        db.query(PlanAllocation)
        .filter(PlanAllocation.plan_id == plan.id)
        .order_by(PlanAllocation.fixture_id)
        .all()
    )
    fixtures = {f.id: f for f in db.query(Fixture).all()}
    alloc_out: list[dict] = []
    for a in allocs:
        fx = fixtures.get(a.fixture_id)
        alloc_out.append(
            {
                "fixture_id": a.fixture_id,
                "name": fx.name if fx is not None else f"#{a.fixture_id}",
                "line_id": a.line_id,
                "start": a.start,
                "end": a.end,
                "channels": a.end - a.start + 1,
                "migrated": a.migrated,  # 发布时逐台迁移事实
                "locked": fx.locked if fx is not None else None,  # 当前值，非历史
                "name_historical": False,
                "locked_historical": False,
            }
        )
    return {
        "feasible": True,
        "id": plan.id,
        "plan_id": plan.id,
        "config_version": plan.config_version,
        "status": "published",
        "optimality_status": plan.optimality_status,
        "migration_count": plan.migration_count,
        "used_lines": plan.used_lines,
        "created_at": plan.created_at,
        "published_at": plan.published_at,
        "allocations": alloc_out,
        "migrations": [],
        "migrations_available": False,
        "occupancy": [],
        "occupancy_available": False,
        "conflicts": [],
        "snapshot_available": False,
        "fallback_note": LEGACY_PUBLISHED_NOTE,
    }


def publish_plan(db: Session, plan_id: int) -> dict:
    """原子发布：版本必须与当前配置一致，旧预览一律拒绝。"""
    current_version = get_config_version(db)
    plan = db.get(Plan, plan_id)
    if plan is None:
        raise HTTPException(status_code=404, detail="预览不存在")
    if plan.status != "draft":
        raise HTTPException(status_code=409, detail=f"该预览已为 {plan.status} 状态，不能重复发布")
    if plan.optimality_status != solver.OPTIMAL:
        if plan.optimality_status is None:
            raise HTTPException(
                status_code=409,
                detail="该预览由旧版本生成、没有最优性证明记录，不能发布，请重新生成预览。",
            )
        raise HTTPException(
            status_code=409,
            detail=f"该预览的最优性状态为 {plan.optimality_status}，未逐级证明最优，不能发布，请重新生成预览。",
        )
    if plan.config_version != current_version:
        raise HTTPException(
            status_code=409,
            detail=(
                f"配置已变化（预览版本 {plan.config_version}，当前版本 {current_version}），"
                "请基于最新配置重新生成预览后再发布。"
            ),
        )

    # payload 在状态翻转、既有地址回写之前构建：其中的名称、锁定与迁移明细都是
    # 发布当时的事实，之后的改名、锁定切换或保留区调整都不会再影响已发布表。
    payload = _plan_payload(db, plan, feasible=True)
    now = datetime.now(timezone.utc)
    payload["snapshot_available"] = True
    payload["migrations_available"] = True
    payload["occupancy_available"] = True
    for a in payload["allocations"]:
        a["name_historical"] = True
        a["locked_historical"] = True

    # 同一事务内完成：发布本预览、作废其余草稿、回写既有地址、保存快照
    db.query(Plan).filter(Plan.status == "draft", Plan.id != plan.id).update(
        {Plan.status: "superseded"}, synchronize_session=False
    )
    plan.status = "published"
    plan.published_at = now
    # 快照保存发布时的历史内容，但状态与发布时间以方案记录为准（读取时覆盖），
    # 避免把发布前的 draft 状态当成已发布结果返回。
    db.add(
        PublishedSnapshot(
            plan_id=plan.id,
            config_version=current_version,
            published_at=now,
            payload=json.dumps(payload, ensure_ascii=False, default=str),
        )
    )
    for a in payload["allocations"]:
        fx = db.get(Fixture, a["fixture_id"])
        if fx is not None:
            fx.previous_line = a["line_id"]
            fx.previous_start = a["start"]
    # 发布改变了灯具的“既有地址”，配置版本再推进一步，使旧草稿全部失效
    new_version = bump_config_version(db)
    db.commit()
    return {
        "ok": True,
        "plan_id": plan.id,
        "config_version": new_version,
        "optimality_status": solver.OPTIMAL,
        "published_at": now,
    }
