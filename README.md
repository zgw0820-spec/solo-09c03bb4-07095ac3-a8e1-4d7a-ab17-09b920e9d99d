# 磁带跨带恢复排程 API

用 FastAPI 实现的排程服务：给定中转盘空闲容量和磁带目录，求出把全部文件从磁带恢复到新存储所需的**最少装带次数**，并给出每次装带的带号、读取顺序和交付时点。

- 最多 **6 盘带**、**12 个分片**、**48 份副本**
- 每片只选一份副本；一次装带内可跳过分片，但磁头只能**向前**读
- 回到带头重读 = 另一次装带
- 中转盘必须先容纳所有暂存分片；文件读齐即校验、交付，并立刻释放它的空间
- 无解时明确指出缺失副本的分片，以及必须同时暂存却超出中转盘容量的文件

## 启动

```bash
docker compose up --build
```

打开 <http://localhost:8000/docs>，展开 `POST /schedule` → **Try it out** → **Execute**，即可用内置样例试跑；也可以直接：

```bash
curl -s http://localhost:8000/example | \
  curl -s -X POST http://localhost:8000/schedule \
       -H 'Content-Type: application/json' --data-binary @-
```

其它端点：

| 方法 | 路径        | 说明                               |
| ---- | ----------- | ---------------------------------- |
| GET  | `/docs`     | Swagger UI（可交互试跑）           |
| GET  | `/example`  | 返回可直接提交的样例请求           |
| POST | `/schedule` | 计算最少装带次数的排程             |
| GET  | `/healthz`  | 健康检查（容器 HEALTHCHECK 使用）  |

也可不用 Docker：`pip install -r requirements.txt && uvicorn app.main:app --port 8000`。

## 磁带外送审批

库管员收到磁带外送单时，用这组接口确认“送走后仍满足灾备恢复要求”：

1. `PUT /library` 按排程请求格式登记**单库目录**（`staging_capacity` + `files`），
   另设 `max_dr_mounts`（灾备装带次数上限）；重复登记会整体替换并重置外送记录。
2. `POST /library/approvals` 每次申请送走**一盘**在库磁带（`{"tape": "T3"}`）。
   扣除该盘后，**无故障**以及**库内再坏任意一盘**的每个场景，都必须能在上限
   次数内恢复全部文件，才予批准。
3. 批准：该盘从在库目录移除并**原子落盘**（重启不会把它算回来）；拒绝：目录
   不变，响应给出一个未通过场景——或附无解原因（缺失副本的分片 / 超出中转盘
   容量的文件），或附该场景**所需的最少装带次数**。
4. `GET /library` 随时可查当前在库目录（试算期间也不阻塞），响应格式与
   `/schedule` 请求完全一致，可直接提交；`GET /library/status` 给出上限、
   在库磁带与已外送审计记录。

| 方法 | 路径                  | 说明                                       |
| ---- | --------------------- | ------------------------------------------ |
| PUT  | `/library`            | 登记/重设在库目录与灾备装带次数上限        |
| GET  | `/library`            | 查询当前在库目录（可直接提交 `/schedule`） |
| GET  | `/library/status`     | 在库概览：上限、在库磁带、已外送记录       |
| POST | `/library/approvals`  | 审批一盘在库磁带的外送申请                 |

可调用样例（服务启动后）：

```bash
# 登记：目录沿用排程格式，另设灾备装带次数上限为 2
curl -s -X PUT http://localhost:8000/library \
     -H 'Content-Type: application/json' \
     --data-binary @examples/library_register.json

# 申请外送 T3：3 个场景（无故障、再坏 T1、再坏 T2）最坏 2 次装带 ≤ 上限，批准
curl -s -X POST http://localhost:8000/library/approvals \
     -H 'Content-Type: application/json' \
     --data-binary @examples/approval_request.json

# 查询当前在库目录，直接提交给原 /schedule
curl -s http://localhost:8000/library | \
  curl -s -X POST http://localhost:8000/schedule \
       -H 'Content-Type: application/json' --data-binary @-

# 再申请外送 T2：库内只剩 T1，T1 再坏则全部文件无解 → 拒绝，目录不变
curl -s -X POST http://localhost:8000/library/approvals \
     -H 'Content-Type: application/json' -d '{"tape": "T2"}'
```

完整演示脚本：`bash examples/approval_demo.sh`；批准/拒绝的真实响应见
`examples/approval_response_approved.json` 与 `examples/approval_response_rejected.json`。

**持久化与并发**：状态文件由 `LIBRARY_STATE_PATH` 指定（compose 中默认为
`/data/library_state.json`，挂载命名卷 `library-data`）；写入采用临时文件 +
fsync + 原子替换，崩溃不会留下半个文件；状态文件损坏时服务拒绝启动而不是
静默重置。审批的“试算 + 落盘 + 生效”在同一把锁内完成，并发审批不会凭过时
目录放行；查询只读已生效快照，无需等待试算。注意 `docker compose down -v`
会删除命名卷（即销毁在库台账）。

## 请求格式

```json
{
  "staging_capacity": 10,
  "files": [
    {
      "id": "file-A",
      "shards": [
        {
          "id": "a1",
          "size": 2,
          "replicas": [
            {"tape": "T1", "position": 10},
            {"tape": "T2", "position": 5}
          ]
        }
      ]
    }
  ]
}
```

- `staging_capacity`：中转盘当前空闲量（单位任意，前后一致即可）
- `files[].shards[].size`：该分片需要的中转空间
- `replicas[].tape` / `position`：副本所在带号与位置；同一装带内的读取位置必须非降序
- 分片的 `replicas` 为空数组表示该分片缺失

样例文件见 `examples/sample_request.json` 与 `examples/sample_response.json`。

## 响应格式

```json
{
  "feasible": true,
  "min_mounts": 2,
  "mounts": [
    {
      "mount": 1,
      "tape": "T1",
      "read_from": 30,
      "read_to": 30,
      "reads": [
        {
          "shard_id": "a2",
          "file_id": "file-A",
          "tape": "T1",
          "position": 30,
          "size": 3,
          "delivers_file": null
        }
      ],
      "delivered_files": [],
      "staging_before": 0,
      "staging_peak": 3,
      "staging_after": 3
    }
  ],
  "files_restored": ["file-A", "file-B"],
  "total_bytes_read": 10,
  "missing_shards": [],
  "capacity_issues": [],
  "message": "最少需要 2 次装带",
  "explanation": {
    "lower_bound": 2,
    "constructive_value": 2,
    "lower_bound_achieved": true,
    "detail": "..."
  }
}
```

每 `mounts` 项即一次装带：`tape` 带号，`reads` 按位置升序给出要读的分片，
`delivers_file` 标记读到哪一片时哪个文件读齐并被交付；`staging_before/peak/after`
给出该次装带前后及峰值的中转盘占用。无解时 `feasible=false`，`mounts` 为空，
`missing_shards` 与 `capacity_issues` 给出原因。

## 算法

1. **静态判定**：任一分片无副本 → 缺失；任一文件分片总量 > 中转盘容量 → 无法校验交付。
   这两种情况即为全部不可行原因（逐文件恢复即可构造出可行解，见下）。
2. **状态**：只记录“已读分片集合”。已交付文件集合和当前占用都由该集合唯一确定，
   因为每片只读一次。
3. **搜索**：以“已读集合”为节点、一次装带为一条边做广度优先搜索；层数即装带数，
   因此首次到达全集时的层数就是最少装带次数（精确最优）。
4. **单次装带的可行读集**：对某盘带，枚举其上可读分片的所有子集。给定读集后，
   占用只取决于集合本身，与读取顺序无关；对同盘重复副本取“不小于当前磁头位置的
   最小位置”即可保留最大的后续选择空间，于是用一个按子集求最小磁头位置的动态规划
   精确判定该读集能否在容量约束下一次装带完成。
5. **下界解释**：按“每盘带一次能覆盖的分片”计算集合覆盖下界；若构造值等于下界则
   直接证明最优，否则在 `explanation` 中说明是容量约束导致需要重复装带，并由 BFS
   穷尽更少次数证明最优。

## 测试

```bash
python -m pytest -q
```

测试包含两类独立校验：

- `replay()`：不依赖求解器内部记账，按请求逐条重放排程，检查每片只读一次、
  所选副本确实存在、位置非降序、容量峰值不超限、文件读齐即时交付；
- `brute_force_min()`：对小型实例穷举所有装带序列，与求解器的最少装带数对比
  （含大量随机实例），验证最优性。

外送审批另有 `tests/test_library.py`：登记/查询回环（`GET /library` 结果直接
提交 `/schedule`）、批准、两类拒绝（故障场景无解、超装带上限）、拒绝后目录
不变且不落盘、重启后已外送磁带不算回来、顺序与并发交错审批（并发下恰好一个
批准）、以及试算期间查询不阻塞。

## 文件结构

```
app/
  main.py       FastAPI 应用与路由（排程 + 外送审批）
  models.py     Pydantic 请求/响应模型与上限校验
  solver.py     最少装带次数求解器
  library.py    在库目录存储：登记、审批、原子落盘与并发控制
tests/
  test_solver.py  求解器、重放校验、暴力对拍
  test_api.py     HTTP 接口测试
  test_library.py 外送审批：故障场景、交错审批、持久化
examples/         样例请求与响应、外送审批演示脚本
Dockerfile / docker-compose.yml
```
