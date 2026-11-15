# 05-proofpack（核证匣）

Proofpack — 举证放行：采样抽取 → 证据打包 → 校验清单 → 放行/驳回闭环。

与 Feedstitch（多源归并）、Batchgate（作业编排）、Syncledger（双端对账）、Changecast（CDC 投影）主链路不同。夹具全本地，不连活库。

## 启动

```bash
docker compose up --build
```

| 入口 | 地址 |
| --- | --- |
| 前端 | http://localhost:5500 |
| API | http://localhost:10500 |

## 主链

选举证对象 → 按固定种子采样 → 生成证据包（含指纹）→ 跑基础校验清单 → 全过才可放行，否则驳回并留痕

## 指纹核验（放行前置闸门）

指纹在生成证据包时按包内条目一次性写入。放行前必须按**现行包内条目**重算指纹并与落库指纹比对：

- 一致：允许继续放行。
- 不一致（条目增、删、改任一发生）：拒绝放行，落 `fingerprint_audits` 留痕（落库指纹、重算指纹、不符原因、策略）。

失配处置策略二选一（`settings.fingerprint_mismatch_policy`，互斥）：

| 策略 | 处置 |
| --- | --- |
| `reject_only`（默认） | 仅拒绝，包保持 `fingerprint_mismatch` 态；旧清单作废；必须显式重新打包生成新指纹与新清单后才可再申请。 |
| `rebuild_and_recheck` | 按现行条目重建指纹并作废旧清单，包转 `fingerprint_rebuilt`；重建后须强制重跑校验清单，不得沿用旧通过态放行。 |

相关接口：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/fingerprint/{pack_id}` | 核验视图：落库/重算指纹、条目数、不符原因、可放行性、留痕 |
| POST | `/api/fingerprint/{pack_id}/verify` | 显式核验（只比对与留痕，不重建） |
| POST | `/api/fingerprint/{pack_id}/rebuild` | 重建指纹（仅 `rebuild_and_recheck` 策略下允许） |
| GET | `/api/fingerprint/audits` | 全量指纹留痕 |
| PUT | `/api/settings` | 切换 `fingerprint_mismatch_policy` |

算法与生成时一致：`app/engines/checksum.py`，条目字段 `business_key/title/amount/qty/event_ts/revision`，按 `business_key` 排序后取各条目 SHA256 再整体 SHA256。

## 种子举证对象

- 门店日结抽检
- 渠道结算抽检
- 价目变更核证

底座内置基础校验：条数一致、键唯一、必填非空。影子试跑 / 规则版本表空挂，深逻辑留给后题。

## 技术栈

Python 3.12 + FastAPI + SQLite；Vue 3 + Vite + Nginx。

## 本地后端

```bash
cd backend
pip install -r requirements.txt
set DATA_DIR=%TEMP%\proofpack-data
uvicorn app.main:app --host 127.0.0.1 --port 10500
```
