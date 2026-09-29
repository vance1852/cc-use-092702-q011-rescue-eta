# 修复巡护救援到达时间的单位错误基础平台

本项目是一套可离线运行的 Python 服务端平台，供国家森林公园管理局、保护站、生态监测人员和巡护队管理野生动植物观察、采样标本、保护站资源、巡护路线、风险告警与处置工单。业务状态、角色权限、幂等结果和审计事件保存在 SQLite 中，可在单个 Linux 应用容器内运行。

## 目录

- src/collection_logistics/：保护站、巡护路线、应急资源、调拨计划和治理情景；
- src/taxonomy_lab/：调查协议、观察记录、异常复核、分析任务租约和生态结论；
- src/biosafety_ops/：园区监测、风险告警、处置工单和资源分配；
- fixtures/：离线验收使用的调查协议与结构化观察记录；
- tests/：领域规则、事务边界、权限、HTTP API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时只依赖 Python 标准库与 SQLite

## 测试

~~~bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
~~~

## 构建检查

~~~bash
python3 -m compileall -q src tests
~~~

## 离线验收

~~~bash
PYTHONPATH=src python3 -m collection_logistics.acceptance --workspace .
PYTHONPATH=src python3 -m taxonomy_lab.acceptance --workspace .
PYTHONPATH=src python3 -m biosafety_ops.acceptance
~~~

三条命令会在临时 SQLite 数据库中完成保护站和路线登记、资源调拨、生态观察分析与风险处置，不访问外部网络。

## HTTP 服务

~~~bash
PYTHONPATH=src python3 -m collection_logistics.api --database park.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m taxonomy_lab.api --database ecology.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m biosafety_ops.api --database safety.sqlite3 --host 127.0.0.1 --port 8082
~~~

服务提供浏览器无关的 JSON 接口和健康检查。进程重启后可以继续读取 SQLite 中的业务状态与审计历史。

## 巡护路线响应时长口径

- 路线的 `response_minutes` 在登记、调度、审计与历史读取各层统一表示**整数分钟**；写入前会拒绝零值、负值、非整数和超过 7 天（10080 分钟）的超长值。
- 预计到达时刻由带时区的实际出发时刻先归一到 UTC 再增加分钟计算（`clock.arrival_after_minutes`），跨日自动进位，夏令时拨快/回拨结果均唯一确定。
- 升级前已存在的路线无法从数据中证明单位，启动迁移时会自动标为 `duration_unit = minutes_legacy_unknown`，路线接口返回 `duration_pending_confirmation: true`，且调度申请、运力分配和资源发车都会被拒绝。
- 旧部署记录的预计到达时刻在迁移后置空，历史读取（`GET /deployments/history/{id}`）不会按当前路线值补算。
- 人工核对（必要时纠正分钟数）后调用 `POST /road_corridors/{id}/confirm_duration`（planner 角色，body 可带 `response_minutes`）解除待确认状态；操作记入审计链。审计摘要见 `GET /audit/summary`，其中时长事件一律显式携带 `response_minutes_unit: "minutes"`。
