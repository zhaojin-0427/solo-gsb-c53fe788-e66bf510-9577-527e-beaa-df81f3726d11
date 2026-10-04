"""已发布地址表快照恢复测试：

- 发布后的已发布查询/CSV 必须取自发布时快照，不随后续改名、锁定切换、保留区变化漂移；
- 发布回写既有地址后，快照里的迁移清单不得变空；
- 已发布查询的状态取自方案记录（published），配置版本取自方案记录（原配置版本）；
- 草稿仍按当前预览路径查询；配置变化后旧草稿仍被拒发（409）；
- 旧库中没有快照的发布记录保留查询与 CSV 回退，无法还原的字段不伪装成历史。
"""
from __future__ import annotations

import csv
import io

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app
from app.models import (
    ConfigVersion,
    Fixture,
    FixtureMode,
    FixtureType,
    Line,
    Plan,
    PlanAllocation,
)

@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autoflush=False)
    with Session() as db:
        db.add(ConfigVersion(id=1, version=1))
        for i, nm in ((1, "线路 1"), (2, "线路 2"), (3, "线路 3"), (4, "线路 4")):
            db.add(Line(id=i, name=nm, is_active=True))
        t = FixtureType(manufacturer="ACME", name="Spot")
        db.add(t)
        db.flush()
        m10 = FixtureMode(type_id=t.id, name="10ch", channels=10)
        m3 = FixtureMode(type_id=t.id, name="3ch", channels=3)
        db.add_all([m10, m3])
        db.flush()
        mode_10, mode_3 = m10.id, m3.id
        db.commit()
    app.dependency_overrides[get_db] = lambda: Session()
    with TestClient(app) as c:
        yield c, Session, mode_10, mode_3
    app.dependency_overrides.clear()
    engine.dispose()


def _publish_with_migration(c, Session, mode_3):
    """构造一次确实发生迁移的发布：Block 锁定在 1/20，迫使原配置在该处的 Move 迁走。

    锁定灯具不能移动，因此被迁移的 Move 必然是未锁定的。
    """
    r = c.post(
        "/api/fixtures",
        json={"name": "Move", "mode_id": mode_3,
              "previous_line": 1, "previous_start": 20, "locked": False},
    )
    assert r.status_code == 201, r.text
    move_id = r.json()["id"]
    r = c.post(
        "/api/fixtures",
        json={"name": "Block", "mode_id": mode_3,
              "previous_line": 1, "previous_start": 20, "locked": True},
    )
    assert r.status_code == 201, r.text
    block_id = r.json()["id"]
    # Block 锁定占住 Move 的原位，预览中 Move 必须迁移，Block 留在原位
    p = c.post("/api/plans/preview").json()
    assert p["optimality_status"] == "optimal", p
    move = next(a for a in p["allocations"] if a["fixture_id"] == move_id)
    block = next(a for a in p["allocations"] if a["fixture_id"] == block_id)
    assert move["migrated"] is True
    assert move["locked"] is False
    assert (block["line_id"], block["start"], block["migrated"]) == (1, 20, False)
    assert (move["line_id"], move["start"]) != (1, 20)
    to_line, to_start = move["line_id"], move["start"]
    mig = next(m for m in p["migrations"] if m["fixture_id"] == move_id)
    assert (mig["from_line"], mig["from_start"], mig["to_line"], mig["to_start"]) == (
        1, 20, to_line, to_start
    )
    orig_version = p["config_version"]
    r = c.post(f"/api/plans/{p['plan_id']}/publish")
    assert r.status_code == 200, r.text
    return p["plan_id"], move_id, (to_line, to_start), orig_version


def test_published_query_restores_from_snapshot_after_writeback(client):
    c, Session, _m10, mode_3 = client
    plan_id, move_id, (to_line, to_start), orig_version = _publish_with_migration(c, Session, mode_3)

    # 发布回写后即使配置版本推进，已发布查询仍取快照：迁移清单不消失、状态不是 draft
    latest = c.get("/api/plans/published/latest").json()
    assert latest["has_published"] is True
    assert latest["plan_id"] == plan_id
    assert latest["status"] == "published"  # 来自方案记录，而非快照内发布前的 draft
    assert latest["config_version"] == orig_version  # 原配置版本来自方案记录
    assert latest["published_at"]
    assert latest["snapshot_available"] is True
    assert latest["migrations_available"] is True

    by_id = {a["fixture_id"]: a for a in latest["allocations"]}
    assert (by_id[move_id]["line_id"], by_id[move_id]["start"]) == (to_line, to_start)
    assert by_id[move_id]["migrated"] is True
    assert by_id[move_id]["locked"] is False

    mig = next(m for m in latest["migrations"] if m["fixture_id"] == move_id)
    assert (mig["from_line"], mig["from_start"], mig["to_line"], mig["to_start"]) == (
        1, 20, to_line, to_start
    )

    got = c.get(f"/api/plans/{plan_id}").json()
    assert got["status"] == "published"
    assert got["config_version"] == orig_version
    assert any(m["fixture_id"] == move_id for m in got["migrations"])


def test_published_table_stable_against_rename_lock_and_reservation_changes(client):
    c, Session, _m10, mode_3 = client
    plan_id, move_id, to_addr, orig_version = _publish_with_migration(c, Session, mode_3)

    # 发布后改名、切换锁定、调整保留区（均推进配置版本）
    r = c.put(
        f"/api/fixtures/{move_id}",
        json={"name": "Renamed", "previous_line": to_addr[0],
              "previous_start": to_addr[1], "locked": True},
    )
    assert r.status_code == 200, r.text
    r = c.post("/api/reservations", json={"line_id": 1, "start": 100, "end": 120, "reason": "desk"})
    assert r.status_code == 201, r.text

    latest = c.get("/api/plans/published/latest").json()
    assert latest["status"] == "published"
    assert latest["config_version"] == orig_version
    by_id = {a["fixture_id"]: a for a in latest["allocations"]}
    move = by_id[move_id]
    assert move["name"] == "Move"  # 快照保留发布时名称
    assert move["locked"] is False  # 快照保留发布时锁定（发布后才被勾上锁定）
    assert move["name_historical"] is True
    assert move["locked_historical"] is True
    # 占用中的保留区也是发布时的：新保留区不应出现在已发布表
    line1 = next(o for o in latest["occupancy"] if o["line_id"] == 1)
    assert all(not (r["start"] == 100 and r["end"] == 120) for r in line1["reservations"])
    # 迁移明细仍然完整
    mig = next(m for m in latest["migrations"] if m["fixture_id"] == move_id)
    assert (mig["from_line"], mig["from_start"]) == (1, 20)
    assert mig["locked"] is False

    # CSV 与页面同源（快照）：仍是旧名称，新配置变化不进入导出
    resp = c.get(f"/api/plans/published/{plan_id}/csv")
    assert resp.status_code == 200
    rows = list(csv.reader(io.StringIO(resp.content.decode("utf-8-sig"))))
    data_rows = [r for r in rows[1:] if r and r[0].isdigit()]
    csv_move = next(r for r in data_rows if int(r[0]) == move_id)
    assert csv_move[1] == "Move"
    assert (int(csv_move[2]), int(csv_move[3])) == to_addr
    assert csv_move[6] == "是"  # 迁移
    assert csv_move[7] == "否"  # 发布时未锁定
    assert "Renamed".encode() not in resp.content
    assert resp.headers["content-disposition"].endswith(f'v{orig_version}.csv"')


def test_draft_still_uses_live_preview_path(client):
    c, _Session, _m10, mode_3 = client
    c.post("/api/fixtures", json={"name": "D1", "mode_id": mode_3})
    p = c.post("/api/plans/preview").json()
    plan_id = p["plan_id"]
    assert p["status"] == "draft"

    # 改名后草稿查询反映当前名称（预览路径），且没有快照可用标记问题
    fx = c.get("/api/fixtures").json()[0]
    r = c.put(f"/api/fixtures/{fx['id']}", json={"name": "DraftRenamed"})
    assert r.status_code == 200
    got = c.get(f"/api/plans/{plan_id}").json()
    assert got["status"] == "draft"
    assert got["allocations"][0]["name"] == "DraftRenamed"

    # 配置已变化：过期草稿拒发
    r = c.post(f"/api/plans/{plan_id}/publish")
    assert r.status_code == 409
    assert "重新生成" in r.json()["detail"]

    # 尚未发布的方案不能取已发布 CSV
    assert c.get(f"/api/plans/{plan_id}/csv").status_code == 404
    # 新草稿（版本匹配）仍可发布
    p2 = c.post("/api/plans/preview").json()
    assert c.post(f"/api/plans/{p2['plan_id']}/publish").status_code == 200


def test_legacy_published_without_snapshot_keeps_query_and_csv_fallback(client):
    c, Session, mode_10, mode_3 = client
    with Session() as db:
        # 旧库：已发布方案 + 分配行，但没有 PublishedSnapshot
        plan = Plan(
            config_version=4,
            status="published",
            optimality_status="optimal",
            migration_count=1,
            used_lines=1,
        )
        db.add(plan)
        db.flush()
        db.add(Fixture(name="Legacy", mode_id=mode_3, previous_line=1, previous_start=1,
                       locked=False))
        db.flush()
        fx = db.query(Fixture).filter(Fixture.name == "Legacy").one()
        # 现存灯具：地址/迁移是事实；已删除灯具（999）的名称回退 #id、锁定未知
        db.add_all([
            PlanAllocation(plan_id=plan.id, fixture_id=fx.id,
                           line_id=1, start=10, end=12, migrated=False),
            PlanAllocation(plan_id=plan.id, fixture_id=999,
                           line_id=1, start=20, end=22, migrated=True),
        ])
        db.commit()
        plan_id = plan.id
        legacy_fx = fx.id

    # 最近发布查询仍可返回（回退），但显式标记哪些不是历史事实
    latest = c.get("/api/plans/published/latest").json()
    assert latest["has_published"] is True
    assert latest["plan_id"] == plan_id
    assert latest["status"] == "published"
    assert latest["config_version"] == 4
    assert latest["snapshot_available"] is False
    assert latest["migrations_available"] is False
    assert latest["occupancy_available"] is False
    assert latest["migrations"] == []  # 无法还原的迁移明细不得伪造
    assert latest["occupancy"] == []
    assert latest["fallback_note"]
    by_id = {a["fixture_id"]: a for a in latest["allocations"]}
    a = by_id[legacy_fx]
    assert (a["line_id"], a["start"], a["end"], a["migrated"]) == (1, 10, 12, False)
    assert a["name"] == "Legacy"
    assert a["locked"] is False
    assert a["name_historical"] is False
    assert a["locked_historical"] is False
    gone = by_id[999]
    assert gone["name"] == "#999"
    assert gone["locked"] is None
    assert gone["migrated"] is True

    # 改名后回退结果的名称确实是当前值（并被标记为非历史）
    r = c.put(f"/api/fixtures/{legacy_fx}", json={"name": "LegacyRenamed"})
    assert r.status_code == 200
    latest = c.get("/api/plans/published/latest").json()
    by_id = {a["fixture_id"]: a for a in latest["allocations"]}
    assert by_id[legacy_fx]["name"] == "LegacyRenamed"
    assert by_id[legacy_fx]["name_historical"] is False

    # CSV 回退保留：地址/迁移为事实，已删除灯具锁定未知用“未知”，并附说明行
    resp = c.get(f"/api/plans/published/{plan_id}/csv")
    assert resp.status_code == 200
    rows = list(csv.reader(io.StringIO(resp.content.decode("utf-8-sig"))))
    data_rows = [r for r in rows[1:] if r and r[0].isdigit()]
    by_fid = {int(r[0]): r for r in data_rows}
    assert by_fid[legacy_fx][1] == "LegacyRenamed"
    assert (int(by_fid[legacy_fx][2]), int(by_fid[legacy_fx][3])) == (1, 10)
    assert by_fid[999][1] == "#999"
    assert by_fid[999][6] == "是"
    assert by_fid[999][7] == "未知"
    note_rows = [r for r in rows if r and "快照" in r[0]]
    assert note_rows

    # 非发布/不存在方案仍 404
    assert c.get("/api/plans/9999").status_code == 404
    assert c.get("/api/plans/published/9999/csv").status_code == 404
