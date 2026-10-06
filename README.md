# 博物馆展厅灯光场景编排 API

为博物馆展厅编排**可定时切换的灯光场景**的 HTTP 服务。输入灯具（通道）与场景（亮度、渐变时长、生效时间窗），服务会：

1. **校验**
   - 场景引用的灯具必须存在（`unknown_lamp`）；
   - 亮度必须在 **0–100**（百分比，对应 DMX 0–255）范围内（`level_out_of_range`）；
   - 场景时间窗合法（`end_at > start_at`，`invalid_time_window`）、同一灯具在一个场景内只能出现一次（`duplicate_channel_in_scene`）；
   - **同一通道上，普通场景的渐变区间 `[start_at, start_at + fade_seconds)` 不得重叠**（端点相接合法），冲突返回具体通道、时间区间和涉及的两个场景（`fade_interval_overlap`）；
   - 紧急场景彼此的生效时间窗不得重叠（`emergency_overlap`）。
2. **合成可执行时间线**：按通道输出排序后的指令序列（`fade` / `emergency_enter` / `emergency_exit`），每条指令带 `at`、`from_level`、`to_level`、`fade_end_at`，可直接下发 DMX/调光网关。
3. **紧急抢占**：紧急场景（`priority: "emergency"`）在 `start_at` 立即抢占普通场景；在 `end_at` 释放通道时，**只恢复到"抢占前已经开始、且释放时仍在生效时间窗内"的那个普通场景**；在紧急期间才到点的普通场景标记为 `skipped`，不会被恢复执行。
4. 校验失败返回 **HTTP 422** 和结构化冲突明细（通道/区间/原因），**不保存无效编排**。

---

## 运行

### Docker Compose（推荐）

```bash
docker compose up --build
# 服务监听 http://localhost:8080
```

### 本地 Python

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

打开交互文档：<http://localhost:8080/docs>（Swagger UI）、<http://localhost:8080/redoc>。

### 测试

```bash
pip install pytest httpx
pytest -q
```

---

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查 |
| `PUT` | `/halls/{hall_id}` | 校验并全量保存一个展厅编排（成功 200，冲突 422 且不写入） |
| `POST` | `/validate` | 只校验/试编译（dry run），不落库 |
| `GET` | `/halls` | 列出已保存展厅 |
| `GET` | `/halls/{hall_id}` | 获取编译后的完整计划 |
| `GET` | `/halls/{hall_id}/timeline` | 只取每通道可执行时间线 |
| `GET` | `/halls/{hall_id}/state?at=...` | 模拟某时刻各通道的实际亮度/生效场景 |
| `DELETE` | `/halls/{hall_id}` | 删除编排 |

数据为**内存存储**（线程安全，单 worker），重启清空；如需多实例持久化可将 `app/store.py` 替换为 Redis/数据库实现，`app/engine.py` 是纯函数、与存储无关。

### 数据模型要点

- `lamp`：`id` 即通道号；`default_level` 是无场景生效时的保持亮度。
- `scene`：
  - `priority`：`normal`（默认）或 `emergency`；
  - `start_at` / `end_at`：ISO 8601（建议带 `Z` 或时区偏移，无时区按 UTC 处理），区间左闭右开；
  - `items[]`：`lamp_id` + `level`(0–100) + `fade_seconds`（渐变时长，0 为瞬切）；紧急项可选 `restore_fade_seconds` 指定释放回普通场景时的渐变时长（默认沿用 `fade_seconds`）。

---

## 调用示例

示例文件：[`examples/bronze-gallery.json`](examples/bronze-gallery.json)。

### 1. 提交编排（校验通过 → 保存并返回时间线）

```bash
curl -sS -X PUT http://localhost:8080/halls/bronze-gallery \
  -H 'Content-Type: application/json' \
  --data @examples/bronze-gallery.json | jq
```

响应节选（`timeline` 为每个通道的指令序列；`preemptions` 描述每次抢占与恢复；`skipped_activations` 是被紧急场景跳过的普通场景）：

```json
{
  "hall_id": "bronze-gallery",
  "version": 1,
  "summary": {"lamps": 3, "scenes": 4, "normal_scenes": 3, "emergency_scenes": 1, "commands": 13, "preemptions": 2, "skipped_scene_activations": 0},
  "timeline": {
    "lamp-case-1": [
      {"type": "fade", "at": "2026-10-06T08:00:00+00:00", "from_level": 0, "to_level": 60.0, "fade_end_at": "2026-10-06T08:05:00+00:00", "fade_seconds": 300.0, "scene_id": "morning-open", "priority": "normal"},
      {"type": "emergency_enter", "at": "2026-10-06T10:00:00+00:00", "from_level": 60.0, "to_level": 5.0, "fade_end_at": "2026-10-06T10:00:00+00:00", "fade_seconds": 0.0, "scene_id": "conservation-alert", "priority": "emergency"},
      {"type": "emergency_exit", "at": "2026-10-06T10:30:00+00:00", "from_level": 5.0, "to_level": 60.0, "fade_end_at": "2026-10-06T10:33:00+00:00", "fade_seconds": 180.0, "scene_id": "conservation-alert", "priority": "emergency", "restored_scene_id": "morning-open"}
    ]
  },
  "preemptions": [
    {"emergency_scene_id": "conservation-alert", "channel": "lamp-case-1", "from": "2026-10-06T10:00:00+00:00", "to": "2026-10-06T10:30:00+00:00", "restored_scene_id": "morning-open", "restored_level": 60.0, "skipped_scene_ids": []}
  ]
}
```

说明：10:00 紧急场景把展柜灯从 **60%** 瞬切到保护值 **5%**；10:30 释放时，`morning-open`（08:00–12:00）在抢占前已开始且仍有效，因此用 180s 渐变恢复到 **60%**。若普通场景此时正在渐变中途，恢复目标是按抢占时刻插值出的亮度。

### 2. 查询某时刻的实际输出

```bash
# 紧急期间
curl -sS "http://localhost:8080/halls/bronze-gallery/state?at=2026-10-06T10:10:00Z" | jq '.channels'
# => { "lamp-case-1": {"level": 5, "active_scene_id": "conservation-alert", "priority": "emergency", "in_emergency": true},
#      "lamp-wash":   {"level": 35, "active_scene_id": "morning-open",       "priority": "normal",    "in_emergency": false}, ... }
# 注意：紧急场景只引用了两盏展柜灯，wall wash 通道不受影响，继续运行普通场景。

# 释放恢复后
curl -sS "http://localhost:8080/halls/bronze-gallery/state?at=2026-10-06T10:40:00Z" | jq '.channels."lamp-case-1"'
# => {"level": 60, "active_scene_id": "morning-open", ...}
```

### 3. 冲突：同一通道渐变区间重叠 → 422，且不保存

```bash
curl -sS -X POST http://localhost:8080/validate -H 'Content-Type: application/json' -d '{
  "id": "bad",
  "lamps": [{"id": "L1"}],
  "scenes": [
    {"id": "a", "start_at": "2026-10-06T08:00:00Z", "end_at": "2026-10-06T20:00:00Z",
     "items": [{"lamp_id": "L1", "level": 50, "fade_seconds": 600}]},
    {"id": "b", "start_at": "2026-10-06T08:09:59Z", "end_at": "2026-10-06T20:00:00Z",
     "items": [{"lamp_id": "L1", "level": 70, "fade_seconds": 10}]}
  ]
}'
```

```json
{
  "ok": false,
  "hall_id": "bad",
  "message": "Orchestration rejected; no timeline was saved.",
  "errors": [
    {
      "code": "fade_interval_overlap",
      "message": "Fade intervals on channel 'L1' overlap for scenes 'a' and 'b' between 2026-10-06T08:09:59+00:00 and 2026-10-06T08:10:00+00:00.",
      "scene_id": "b",
      "channel": "L1",
      "interval": ["2026-10-06T08:09:59+00:00", "2026-10-06T08:10:00+00:00"],
      "between_scenes": ["a", "b"]
    }
  ],
  "warnings": []
}
```

`a` 的渐变在 08:10:00 结束，`b` 在 08:09:59 开始——半开区间相交 1 秒即拒绝；若 `b` 从 **08:10:00** 开始则合法（端点相接）。此时 `GET /halls/bad` 返回 404，证明未保存。

### 4. 冲突：引用不存在的灯具 / 亮度越界

```bash
curl -sS -X PUT http://localhost:8080/halls/bad2 -H 'Content-Type: application/json' -d '{
  "id": "bad2",
  "lamps": [{"id": "L1"}],
  "scenes": [
    {"id": "s", "start_at": "2026-10-06T08:00:00Z", "end_at": "2026-10-06T09:00:00Z",
     "items": [{"lamp_id": "L9", "level": 120, "fade_seconds": 0}]}
  ]
}'
```

- `level: 120` 由 Pydantic 以 422 拦截（字段级错误）；
- 改为合法亮度但引用未声明灯具时，返回业务错误 `unknown_lamp`，同样 422 且不写入。

### 5. 抢占恢复边界语义

- 场景 B 在紧急窗口**之内**到点：B 出现在 `skipped_activations` 中，释放时不会执行 B，只恢复抢占前仍有效的场景。
- 抢占前已开始、但在释放时刻**之前结束**的场景不可恢复：`emergency_exit.restored_scene_id` 为 `null`，通道保持紧急亮度直到下一个已排定场景接管，指令带 `note` 说明。
- 新普通场景恰好在释放的同一时刻开始：它直接接管通道，`emergency_exit` 带 `taken_over_by`，不产生可见的亮度跳变。
- 紧急场景只影响自己 `items` 中列出的通道，其他通道时间线完全不变。

---

## 目录结构

```
app/
  models.py     # Pydantic 输入模型与时间归一化
  engine.py     # 纯函数：校验 + 时间线合成（抢占/恢复/跳过）
  simulator.py  # 任意时刻的通道状态模拟
  store.py      # 线程安全内存存储
  main.py       # FastAPI 路由
tests/
  test_api.py   # 16 个端到端用例
examples/
  bronze-gallery.json
Dockerfile
docker-compose.yml
requirements.txt
```
