# 家庭流量共享的中途迁组与账期确认

容量用演练单位表示，账期和流量使用区间是同一周期的相对日。补传记录缺少拆分明细时保留空值。

所有资料供本地联调，为合成记录。`samples/register.json` 是基础台账，`samples/intake.json` 是送达的业务事实，`samples/receipts.json` 是对端查询合同。

## 本地资料端

使用 Python 3.11.2 标准库。服务只监听回环地址。

```sh
python3 -m unittest -v
python3 partner_feed.py --port 8765
```

- `GET /register` 读取台账。
- `GET /intake?since=0&limit=50` 读取一页到达记录，将返回的 `cursor` 传给下一次查询的 `since`。消息含 `sequence`、`at`、`source`、`facts`，其中 `at` 是收到记录的时间，业务发生时间可在 `facts` 中明确报告。
- `GET /receipt?action=操作&desk=经办方&ref=编号` 以三项值匹配样例。对端返回 `evidence` 作为独立事实。标记 `delayed` 的样例首次查询返回 503，后续可以读取回执。未知编号返回 404。联调端不会自行办理业务操作。

通过 `--samples` 指定另一组同名资料可换样例。临时不可查的状态按联调进程保存。

## 家庭流量后端

新增的 `backend.py` 只用 Python 标准库，提供可直接调用的 `FamilyBackend` 和 JSON HTTP 服务。当前是带锁的本地内存事件台账；重启后可用 `--sample` 或 `--sync-partner` 重新装载事实。

```sh
python3 -m unittest -v
python3 backend.py --sample-report     # 输出样例的成员/两组成员管理员解释
python3 backend.py --sample --port 8800
```

另开资料端后，也可以让后端从到达记录同步，并重试第一次返回 503 的回执：

```sh
python3 partner_feed.py --port 8765
python3 backend.py --port 8800 \
  --sync-partner http://127.0.0.1:8765
```

### 身份与授权

本地合成身份通过 `X-Actor-Id` 传递：

- 经办：`ops-1`；账务：`billing-1`；客服：`support-1`。
- 组管理员：`admin-g-a`、`admin-g-b`，登记新组时会生成对应的 `admin-<组号>`。
- 成员直接使用成员号，例如 `m-2`。

客服必须先取得成员授权：

```sh
curl -sS -X POST http://127.0.0.1:8800/v1/authorizations \
  -H 'Content-Type: application/json' \
  -H 'X-Actor-Id: m-2' \
  -d '{"member_id":"m-2","agent_id":"support-1","period_id":"1"}'

curl -sS http://127.0.0.1:8800/v1/support/members/m-2/current \
  -H 'X-Actor-Id: support-1'
```

成员本人可查询实际用量区间、归属、抵扣的流量包、暂挂和差额：

```sh
curl -sS 'http://127.0.0.1:8800/v1/members/m-2/charge-basis?period=1' \
  -H 'X-Actor-Id: m-2'
```

组管理员只能看本组承诺差额、生效日期、汇总待核实量和下一次迁组状态；返回模型不含用量窗口或 `m-2-13-17` 这类通信明细引用：

```sh
curl -sS http://127.0.0.1:8800/v1/groups/g-a/admin-view \
  -H 'X-Actor-Id: admin-g-a'
```

### 主要 HTTP 接口

- 经办：`POST /v1/groups`、`POST /v1/packs`、`POST /v1/commitments`、`POST /v1/register/load`
- 迁组：`POST /v1/migrations`、`POST /v1/migrations/{id}/consents`、`POST /v1/migrations/{id}/activate`
- 账务：`POST /v1/usage`、`POST /v1/usage/{id}/evidence`、`POST /v1/periods/{period}/close`、`POST /v1/corrections`
- 事实接入：`POST /v1/intake`（可直接发送 `samples/intake.json` 中的单条 `facts`，也可发送完整 feed item）
- 视图：`GET /v1/members/{member}/charge-basis`、`GET /v1/support/members/{member}/current`、`GET /v1/groups/{group}/admin-view`
- 资料端同步：`POST /v1/sync/partner`，请求体可传 `{"base_url":"http://127.0.0.1:8765"}`

### 样例口径

`build_sample_backend()` 会得到以下可核对结果：

- `m-2` 的迁组生效点为相对日 `15`，`g-a`、`g-b` 两边同意后才安排成员关系切换。
- `g-b` 容量 70，已有承诺 61，因此只能接受 9；承诺 10 会返回 `new_group_capacity_exceeded`。
- `[13,17] / 12` 跨生效点且回执 `split=null`，整体作为 `cross_migration_without_quantitative_split` 暂挂，抵扣和扣费均为 0。
- `[13,14] / 3` 完全在生效点前，归属 `g-a`，从 `g-a` 对 `m-2` 的 20 单位承诺中抵扣，剩余承诺差额 17。
- `personal-4` 是成员自购包，可随成员继续使用；`shared-7` 是 `g-a` 组包，不默认转到 `g-b`，切换后对新用量不可用。
- 已签账快照保持为 3 单位；之后补传或证据拆分不再覆盖快照，而生成 `late_usage_difference` 或 `post_signing_difference` 差额记录。
- 旧组 20 单位承诺继续占用可用池，其中包括尚待核实的跨点用量，不会作为可再承诺额度“重卖”给新成员。

区间按半开时间点解释：`[13,14]` 在生效点 `15` 之前，`[13,17]` 内部包含生效点 `15`，因此属于跨点记录。
