# DMX 灯具地址编排台

本地运行的 DMX 灯具地址编排 Web 应用：导入灯具型录、建立灯具实例与最多四条 512 通道线路、设置保留区，
由 OR-Tools CP-SAT 求解器在不跨线路、通道连续、无重叠的前提下完成地址编排，支持预览、原子发布与 CSV 导出。

## 功能

- **灯具型录管理**：按 `manufacturer,name,mode,channels` 导入 CSV/JSON；同一型号支持多个模式（占用通道数不同），重复模式更新通道数。
- **灯具实例**：为灯具选择型号/模式，记录上一版线路与起始地址，可锁定既有线路与起始地址。
- **线路与保留区**：固定 4 条 × 512 通道线路，可重命名、停用；每条线路可设置保留通道区。
- **编排规则**：每台灯具获得一段连续通道，不跨线路、不与其他灯具或保留区重叠；锁定灯具必须留在原位。
- **优化目标（按优先级，逐级证明）**：
  1. 已配灯具的**迁移台数最少**；
  2. 在此前提下**启用线路数最少**；
  3. 再按灯具 ID 排序，逐台取 `(线路 ID, 起始地址)` **字典序最小**的确定方案。

  三个阶段都要求 CP-SAT 返回 `OPTIMAL`（真正证明最优）后才采信目标值并固定进入下一级；
  任一阶段限时内只拿到 `FEASIBLE`（可行但未证明最优的候选）时，**不固定其取值继续求解**，
  而是返回与“真正无解”明确区分的“未证最优”状态。
- **预览的四种结果**（均为 HTTP 200，由 `optimality_status` 与 `feasible` 共同区分）：
  - `optimal`：三级目标均已证明最优，持久化为草稿并返回 `plan_id`，可以发布；
  - `unproven` 且 `feasible=true`：有可行候选但未能逐级证明最优，返回候选占用供参考，
    但**无 `plan_id`、不生成可发布草稿**；
  - `unproven` 且 `feasible=false`（空候选）：求解返回 UNKNOWN、候选读取失败或冲突诊断
    超时，未能取得任何完整可用排布。页面提示“暂无可展示排布”，**不当作已证无解**，
    同样不生成草稿、不能发布；
  - `infeasible`：已证明无解，给出具体冲突约束（容量不足、锁定越界、锁定块互相重叠/与保留区冲突、碎块不可排布等）。
- **原子发布**：预览绑定配置版本与最优性状态；配置一旦变化旧预览立即失效并拒绝发布（409），
  未证最优或旧版本遗留、**没有最优性证明记录的草稿同样拒发（409）**，需重新生成预览。
  发布在单个数据库事务内完成，并写入不可变快照。
- **已发布地址表来自快照**：`GET /api/plans/{id}`（已发布方案）与 `GET /api/plans/published/latest`
  从发布时写入的不可变快照还原地址、名称、锁定、迁移与占用信息——发布后改名、切换锁定、
  调整保留区或重命名线路都不会让已发布表与快照 CSV 漂移；已发布状态与原配置版本以方案记录为准，
  不会把快照里发布前的 draft 状态直接返回。草稿仍按当前配置实时重建。
  旧库缺少快照的发布记录回退为按当前配置重建，并在响应中以 `from_snapshot=false` 与 `notice`
  显式标注，不把无法还原的发布时字段伪装成历史事实。
- **浏览器防护**：只有 `optimal` 且配置版本未过期的预览可发布；未证最优时显示黄色
  “未证最优（不可发布）”面板——有候选时展示候选排布仅供参考，空候选时提示
  “暂无可展示排布”；两种情况下发布按钮都保持禁用。
- **CSV 导出**：已发布地址表（UTF-8 带 BOM，Excel 可直接打开），与“已发布”页签同源——
  均取自发布快照，页面看到的内容与下载的 CSV 始终一致。

## 快速开始（Docker Compose，推荐）

需要本机已安装 Docker 与 Docker Compose 插件。

```bash
docker compose up --build -d
```

启动后用浏览器访问：

> **http://localhost:8000**

数据保存在宿主机 `./data` 目录（绑定挂载到容器内 `/data/dmx.db`）中，容器重建不丢数据。

停止：

```bash
docker compose down          # 保留数据
docker compose down -v       # 同时删除数据卷
```

## 本地直接运行（不使用 Docker）

需要 Python 3.11+。

```bash
python3 -m pip install -r requirements.txt
python3 -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

然后访问 http://localhost:8000 。SQLite 数据库默认位于 `./data/dmx.db`，可用环境变量
`DMX_DB_PATH` 修改位置。

## 使用流程

1. 打开 **灯具型录** 页签，点击“载入示例 CSV”再“导入文本内容”（或上传自己的 `.csv`/`.json`）。
2. 打开 **灯具实例**，添加灯具；对已在现场配过地址的灯具填写“既有线路/起始地址”，需要保持不动的勾选“锁定”。
3. 打开 **线路与保留区**，按需停用线路、添加保留通道区。
4. 回到 **编排**，点击“生成预览”：
   - 查看四条线路的占用条与迁移清单；摘要中出现“✅ 已逐级证明最优”才可发布；
   - 若出现黄色“未证最优（不可发布）”面板且下方带候选排布，说明求解器限时内未完成
     某级最优性证明，候选排布仅供参考，请减少灯具数量或稍后重新生成；
   - 若黄色面板提示“暂无可展示排布”（空候选），说明求解器限时内连一个完整候选都没
     取得（UNKNOWN / 候选读取失败 / 诊断超时）——这**不是**已证明无解，不生成草稿、
     不能发布，请减少灯具数量、放宽部分锁定或稍后重试；
   - 若出现红色冲突面板（已证无解），按提示调整约束后重新生成；
   - 预览期间如果改了任何配置，顶部出现黄色横幅且发布按钮禁用，需重新生成预览。
5. 点击“按此预览原子发布”。发布后在 **已发布** 页签查看地址表并下载 CSV。
   已发布表与 CSV 均取自发布时的不可变快照：此后再改名、切换锁定或调整保留区，
   已发布页签与 CSV 仍保持发布时刻的内容；仅旧库缺少快照的发布记录会回退为按当前
   配置重建，并在页面上以黄色横幅提示“可能与发布时不一致”。
6. 发布成功后，方案中的地址自动成为各灯具的“既有地址”，下一轮编排会优先保持它们不动。

## API 摘要

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET/POST | `/api/catalog` | 列出/新建型号 |
| POST | `/api/catalog/import` | 上传 CSV/JSON 批量导入型录 |
| DELETE | `/api/catalog/{id}` | 删除型号（仍被引用时拒绝） |
| GET/POST | `/api/fixtures` | 灯具列表/新建 |
| PUT/DELETE | `/api/fixtures/{id}` | 编辑/删除灯具 |
| GET | `/api/lines` / PATCH `/api/lines/{id}` | 线路列表/重命名/启停 |
| GET/POST/DELETE | `/api/reservations[/{id}]` | 保留区管理 |
| GET | `/api/plans/version` | 当前配置版本与最近发布信息 |
| POST | `/api/plans/preview` | 求解并生成预览 |
| GET | `/api/plans/{id}` | 方案详情：草稿按当前配置重建，已发布从快照还原 |
| POST | `/api/plans/{id}/publish` | 按预览原子发布（版本不符返回 409） |
| GET | `/api/plans/published/latest` | 最近一次发布的完整地址表（取自发布快照） |
| GET | `/api/plans/published/{id}/csv` | 下载已发布地址表 CSV（与已发布查询同源） |

交互式文档：启动后访问 http://localhost:8000/docs 。

### 预览与发布：实际请求/响应示例

**1）生成预览（已证最优，可发布）**

```bash
curl -s -X POST http://localhost:8000/api/plans/preview
```

```json
{
  "feasible": true,
  "optimality_status": "optimal",
  "id": 12,
  "plan_id": 12,
  "config_version": 7,
  "status": "draft",
  "migration_count": 0,
  "used_lines": 1,
  "created_at": "2026-10-02T08:30:00.000000",
  "allocations": [
    {"fixture_id": 1, "name": "Spot1", "line_id": 1, "start": 10, "end": 19,
     "channels": 10, "migrated": false, "locked": false}
  ],
  "migrations": [],
  "occupancy": [
    {"line_id": 1, "name": "线路 1", "is_active": true, "size": 512,
     "used_channels": 10, "free_channels": 502, "fixtures": ["..."], "reservations": []}
  ],
  "conflicts": []
}
```

只有 `optimality_status` 为 `optimal` 的响应才带 `plan_id` 并落库为可发布草稿。

**2）生成预览（未证最优、有候选，不可发布）**

灯具很多、求解器限时内只证明到可行性时，返回 200 但状态为 `unproven`：
响应包含候选排布供参考，但**没有 `plan_id`**，数据库中也不会生成草稿。

```json
{
  "feasible": true,
  "optimality_status": "unproven",
  "unproven_stage": "lines",
  "message": "第二阶段（启用线路数最少）在限定时间内仅找到可行解，未能证明启用线路数最少；未生成可发布草稿，请重试。",
  "config_version": 7,
  "migration_count": 4,
  "used_lines": 3,
  "allocations": ["...候选排布，仅供参考..."],
  "migrations": ["..."],
  "occupancy": ["..."],
  "conflicts": [{"type": "unproven", "message": "第二阶段（启用线路数最少）……"}]
}
```

`unproven_stage` 取值：`migration`（迁移台数未证最优）、`lines`（启用线路数未证最优）、
`lexicographic`（字典序破局未能逐台证明）、`diagnosis`（冲突诊断超时）。这种结果调用
发布接口会得到 404（无草稿可发）；浏览器中发布按钮禁用。请减少灯具数量或稍后重试。

**3）生成预览（未证最优、空候选，暂无可展示排布）**

求解返回 UNKNOWN、候选读取失败或冲突诊断超时时，连一个完整候选排布都没能取得。
此时仍返回 HTTP 200 与 `optimality_status=unproven`（附阶段信息），但 `feasible=false`
且 `allocations` 为空：

```json
{
  "feasible": false,
  "optimality_status": "unproven",
  "unproven_stage": "migration",
  "message": "第一阶段（已配灯具迁移台数最少）在限定时间内未能证明最优，且未取得任何可展示的候选排布；这不代表已证明无解，未生成可发布草稿，请减少灯具数量或稍后重试。",
  "config_version": 7,
  "migration_count": 0,
  "used_lines": 0,
  "allocations": [],
  "migrations": [],
  "occupancy": [],
  "conflicts": [{"type": "unproven", "message": "第一阶段（已配灯具迁移台数最少）……"}]
}
```

注意：`feasible=false` 在这里**不**表示已证明无解——它只表示本次求解未取得可展示的
完整排布。页面据此提示“暂无可展示排布”，不显示冲突面板，也不生成草稿或开放发布。
是否可行的裁决始终以是否取得完整可用排布为准，不会把空候选误报为“候选预览”。

**4）生成预览（真正无解）**

```json
{
  "feasible": false,
  "optimality_status": "infeasible",
  "config_version": 7,
  "conflicts": [
    {"type": "capacity",
     "message": "通道总需求 640 超过启用线路的可用容量 512（1 条 × 512，扣除保留区）。",
     "total_demand": 640, "total_capacity": 512}
  ],
  "occupancy": []
}
```

`infeasible` 表示已证明无解；`unproven` 表示可能有解但没来得及证明最优（含空候选的
“暂无可展示排布”）——三者不会混淆。

**5）发布已证最优的预览**

```bash
curl -s -X POST http://localhost:8000/api/plans/12/publish
```

```json
{"ok": true, "plan_id": 12, "config_version": 8,
 "optimality_status": "optimal", "published_at": "2026-10-02T08:31:05.000000"}
```

发布被拒绝的情况（均为 HTTP 409）：

- 配置版本过期（预览生成后改了灯具/线路/保留区）：
  `配置已变化（预览版本 7，当前版本 9），请基于最新配置重新生成预览后再发布。`
- 未证最优/无证明记录的草稿（含旧版本遗留草稿）：
  `该预览由旧版本生成、没有最优性证明记录，不能发布，请重新生成预览。`
- 重复发布：`该预览已为 published 状态，不能重复发布`。

**6）核对预览的最优性状态**

`GET /api/plans/{plan_id}` 返回体含 `optimality_status`；`GET /api/plans/published/latest`
与发布快照同样带该字段，可在查询与发布两侧核对。

**7）已发布地址表的快照语义**

已发布方案的查询响应中：

- `from_snapshot=true`：地址、名称、锁定、迁移、占用均还原自发布时的不可变快照，
  `status`/`config_version`/`published_at` 以方案记录为准（快照内可能仍是发布前的
  draft 状态，不会直接返回）；
- `from_snapshot=false` 且带 `notice`：旧库缺少快照的发布记录，内容按当前配置重建，
  名称/锁定/迁移/占用可能与发布时不一致——这是显式标注的回退，不是发布时事实；
- 草稿（`status=draft`）始终按当前配置实时重建，用于预览路径。

## 技术栈

- 后端：Python 3.11、FastAPI、SQLAlchemy 2.0、SQLite（WAL）、Google OR-Tools CP-SAT
- 前端：原生 HTML/CSS/JavaScript（无构建步骤、无框架依赖）
- 部署：Docker / Docker Compose 单容器，数据卷持久化
