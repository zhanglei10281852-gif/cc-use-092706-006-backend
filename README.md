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

## 维护窗口编排

`POST /api/windows/*` 系列接口面向湿地年度维护：运营人员登记区域（芦苇、旱柳、滩涂等）、作业类型与物种敏感期规则（繁殖、迁徙，按 `MM-DD` 年度区间，支持跨年），系统按注入的基准日 `as_of` 计算可执行窗口；冲突时返回逐条规则原因并保留候选窗口。计划延期（如临时降雨）与取消都会写入 `plan_revisions` 版本化历史，携带幂等键的重复请求返回首次结果而不产生重复记录。接口需要 `windows.read` / `windows.write` 权限，计划与历史持久化在 SQLite 中，服务重启后自动恢复。

## 测试

```bash
python -m pytest
```

测试覆盖参数校验、身份权限、事务边界、任务状态、失败恢复、审计写入和现有生态计算接口。

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
