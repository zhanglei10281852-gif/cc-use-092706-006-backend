# 城市生态运营服务

这是一个面向城市湿地保护团队的 Python 后端服务。项目提供本地 HTTP 接口、SQLite 持久化、身份与角色管理、审计记录、任务编排和可扩展的生态数据处理边界，便于在单机环境中保存运营状态并复核业务决定。

## 运行环境

- Python 3.11 或更高版本
- SQLite 3（使用 Python 标准库）

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

默认数据文件位于 `data/compute-operations.db`，可以复制 `.env.example` 后调整本地路径。

## 初始化与启动

```bash
python -m app.cli init-db
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康接口为 `GET /api/system/health`。所有状态变化都写入 SQLite，并由应用内事务保证关联记录的一致性。

## 测试

```bash
python -m pytest
```

测试覆盖参数校验、身份权限、事务边界、任务状态、失败恢复、审计写入、现有生态计算接口，以及奥森湿地维护窗口编排（跨月窗口、重叠敏感期、延期/取消版本化与幂等、权限边界、重启恢复）。

## 湿地维护窗口编排（/api/wetlands）

面向芦苇、旱柳、滩涂三类区域的年度维护编排：

- `POST /api/wetlands/zones`、`POST /api/wetlands/work-types`：登记区域与作业类型（写入需要 `wetlands.write`）。
- `POST /api/wetlands/rules`：登记物种敏感期，支持周年规则（`MM-DD`，可跨年，如越冬期）与固定日期规则，敏感级别分为 `avoid`（建议避让）与 `forbid`（严禁作业），规则可限定作业类型。
- `POST /api/wetlands/windows/preview`：按请求的日期范围与持续天数试算候选窗口，返回每个候选的冲突规则、命中日期、是否在请求范围内。
- `POST /api/wetlands/plans`：编排计划，自动选择请求范围内最早的可执行窗口；无窗口时返回 `409` 与原因，并保留全部候选（含范围外备选）。创建请求支持 `idempotency_key` 幂等。
- `POST /api/wetlands/plans/{id}/postpone|cancel|complete`：临时降雨延期会以注入日期次日为新起点重算窗口；每次延期/取消/完成都产生递增版本的历史记录；支持 `Idempotency-Key` 请求头重复请求幂等。延期无窗口时可用 `force=true` 采用范围外候选。
- `POST /api/wetlands/recovery`：服务重启后按 SQLite 中持久化的计划、候选与版本历史恢复，并标出逾期未闭环计划。
- 所有接口通过 Bearer 会话鉴权，读取需要 `wetlands.read`、编排/登记需要 `wetlands.write`；计划编排日期通过请求体 `today` 或 `X-As-Of-Date` 请求头注入，便于确定性验收。

## 编译检查

```bash
python -m compileall -q app tests
```

## 本地验收

```bash
python -m app.cli check-db
python -m app.cli smoke
```

`check-db` 检查 SQLite 完整性和外键设置，`smoke` 在进程内调用健康接口并验证基础路由。项目不依赖外部数据库、消息队列或网络服务。
