"""家庭共享流量迁组、用量归属与差额账务后端。

仅使用 Python 标准库。核心 :class:`FamilyBackend` 可直接调用，也可通过
:class:`FamilyBackendServer` 暴露成本地 JSON HTTP 服务。
"""

from __future__ import annotations

import argparse
import copy
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import RLock
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

SAMPLES = Path(__file__).resolve().parent / "samples"
UNSET = object()


class ApiError(Exception):
    """可转换为 HTTP 响应的业务错误。"""

    def __init__(self, status: int, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


def clone(value: Any) -> Any:
    return copy.deepcopy(value)


def _pair(value: Any) -> list[float | int] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        value = [value.get("start", value.get("from")), value.get("end", value.get("to"))]
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ApiError(400, "invalid_window", "区间必须是两个相对日/时间点")
    if not isinstance(value[0], (int, float)) or not isinstance(value[1], (int, float)):
        raise ApiError(400, "invalid_window", "区间端点必须是数字")
    if value[0] > value[1]:
        raise ApiError(400, "invalid_window", "区间起点不能晚于终点")
    return [value[0], value[1]]


class PartnerClient:
    """partner_feed.py 的小型同步客户端。"""

    def __init__(self, base_url: str, timeout: float = 2.0):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def get_json(self, path: str) -> tuple[int, Any]:
        request = Request(self.base_url + path, headers={"Accept": "application/json"})
        try:
            with urlopen(request, timeout=self.timeout) as response:  # noqa: S310 - local loopback
                return response.status, json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except Exception:
                payload = {"error": "http_error"}
            return exc.code, payload
        except URLError as exc:
            raise ApiError(502, "partner_unreachable", str(exc)) from exc

    def register(self) -> dict:
        status, payload = self.get_json("/register")
        if status != 200:
            raise ApiError(502, "partner_register_error", "资料端台账读取失败", payload)
        return payload

    def intake(self, since: int = 0, limit: int = 50) -> dict:
        status, payload = self.get_json(f"/intake?since={since}&limit={limit}")
        if status != 200:
            raise ApiError(502, "partner_intake_error", "资料端到达记录读取失败", payload)
        return payload

    def receipt(self, action: str, desk: str, ref: str) -> dict | None:
        query = urlencode({"action": action, "desk": desk, "ref": ref})
        status, payload = self.get_json("/receipt?" + query)
        if status == 200:
            return payload.get("evidence", {})
        if status == 503:
            raise ApiError(503, "receipt_pending", "合作方回执暂时不可查，应稍后重试", payload)
        if status == 404:
            raise ApiError(404, "receipt_not_found", "合作方无此回执", payload)
        raise ApiError(502, "receipt_error", "回执查询失败", payload)


class FamilyBackend:
    """线程安全的内存业务后端。

    设计为事件台账：原始事实、分配、暂挂和差额记录均保留；已签账快照不会被
    后续补传直接改写。
    """

    def __init__(self, register: dict | None = None):
        self.lock = RLock()
        self._counter = 0
        self._register_loaded = False
        self._register_source: dict | None = None

        self.principals: dict[str, dict] = {
            "system": {"id": "system", "roles": ["*"]},
            "ops-1": {"id": "ops-1", "roles": ["provisioning"]},
            "billing-1": {"id": "billing-1", "roles": ["billing"]},
            "support-1": {"id": "support-1", "roles": ["support"]},
        }
        self.groups: dict[str, dict] = {}
        self.members: dict[str, dict] = {}
        self.memberships: list[dict] = []
        self.funds: dict[str, dict] = {}
        self.commitments: dict[str, dict] = {}
        self.migrations: dict[str, dict] = {}
        self.usage: dict[str, dict] = {}
        self.allocations: list[dict] = []
        self.holds: dict[str, dict] = {}
        self.charge_lines: list[dict] = []
        self.corrections: list[dict] = []
        self.periods: dict[str, dict] = {}
        self.authorizations: dict[str, dict] = {}
        self.processed_sequences: set[int] = set()
        self.audit: list[dict] = []

        if register is not None:
            self.load_register(register)

    # ---------- 基础与鉴权 ----------

    def _next_id(self, prefix: str) -> str:
        self._counter += 1
        return f"{prefix}-{self._counter}"

    def _audit(self, action: str, actor_id: str, resource: str, details: dict | None = None) -> None:
        self.audit.append(
            {"id": self._next_id("audit"), "action": action, "actor_id": actor_id,
             "resource": resource, "details": clone(details or {})}
        )

    def principal(self, actor_id: str | None) -> dict:
        return self.principals.get(actor_id or "anonymous", {
            "id": actor_id or "anonymous", "roles": []
        })

    def require_role(self, actor_id: str, role: str) -> dict:
        principal = self.principal(actor_id)
        roles = set(principal.get("roles", []))
        if "*" in roles or role in roles:
            return principal
        raise ApiError(403, "forbidden", f"缺少角色 {role}")

    def require_provisioning(self, actor_id: str) -> dict:
        return self.require_role(actor_id, "provisioning")

    def require_billing(self, actor_id: str) -> dict:
        return self.require_role(actor_id, "billing")

    def require_group_admin(self, actor_id: str, group_id: str) -> dict:
        principal = self.principal(actor_id)
        roles = set(principal.get("roles", []))
        if "*" in roles:
            return principal
        if "group_admin" in roles and group_id in principal.get("group_ids", []):
            return principal
        raise ApiError(403, "group_admin_required", f"需要 {group_id} 管理员权限")

    def _ensure_member(self, member_id: str) -> dict:
        if member_id not in self.members:
            self.members[member_id] = {"id": member_id}
            self.principals[member_id] = {
                "id": member_id, "roles": ["member"], "member_id": member_id
            }
        return self.members[member_id]

    def grant_authorization(
        self,
        member_id: str,
        agent_id: str,
        *,
        period_id: str = "current",
        scopes: list[str] | None = None,
        expires_at: int | float | None = None,
        actor_id: str = "system",
    ) -> dict:
        with self.lock:
            p = self.principal(actor_id)
            if "*" not in p.get("roles", []) and not (
                "member" in p.get("roles", []) and p.get("member_id") == member_id
            ):
                raise ApiError(403, "member_consent_required", "只有成员本人可以授权客服")
            auth_id = self._next_id("auth")
            row = {
                "id": auth_id,
                "member_id": member_id,
                "agent_id": agent_id,
                "period_id": str(period_id),
                "scopes": scopes or ["charge_basis:current"],
                "expires_at": expires_at,
                "revoked": False,
            }
            self.authorizations[auth_id] = row
            self._audit("authorization.grant", actor_id, member_id, {"agent_id": agent_id})
            return clone(row)

    def _support_authorized(self, agent_id: str, member_id: str, period_id: str) -> dict | None:
        for grant in self.authorizations.values():
            if (
                not grant["revoked"]
                and grant["agent_id"] == agent_id
                and grant["member_id"] == member_id
                and grant["period_id"] in ("*", "current", str(period_id))
            ):
                if grant.get("expires_at") is None:
                    return grant
        return None

    def _member_access(self, actor_id: str, member_id: str, period_id: str) -> dict:
        principal = self.principal(actor_id)
        roles = set(principal.get("roles", []))
        if "*" in roles:
            return {"kind": "system"}
        if "member" in roles and principal.get("member_id") == member_id:
            return {"kind": "member"}
        if "support" in roles:
            grant = self._support_authorized(principal["id"], member_id, period_id)
            if grant:
                return {"kind": "support", "grant": grant}
            raise ApiError(403, "authorization_required", "客服查询需要成员对当期口径的授权")
        raise ApiError(403, "member_access_required", "成员账务明细仅本人或已授权客服可查")

    # ---------- 账期与经办台账 ----------

    def _ensure_period(self, period_id: str | int, start: int | float = 1,
                       end: int | float = 30) -> dict:
        pid = str(period_id)
        if pid not in self.periods:
            self.periods[pid] = {
                "id": pid, "start": start, "end": end, "status": "open",
                "closed": False, "signed": False, "locked": False,
                "closed_at": None, "signed_at": None, "statement": None,
                "closing_evidence": None,
            }
        return self.periods[pid]

    def _period_id_for_time(self, at: int | float) -> str:
        for pid, period in self.periods.items():
            if period["start"] <= at <= period["end"]:
                return pid
        if self.periods:
            return sorted(self.periods)[0]
        return "1"

    def _period_id_for_window(self, window: list, group_id: str | None = None) -> str:
        if group_id and group_id in self.groups:
            return self.groups[group_id]["period_id"]
        return self._period_id_for_time(window[0])

    def current_period_id(self, at: int | float | None = None) -> str:
        if at is not None:
            return self._period_id_for_time(at)
        open_periods = [p for p in self.periods.values() if not p["closed"]]
        if open_periods:
            return open_periods[0]["id"]
        return sorted(self.periods)[-1] if self.periods else "1"

    def register_group(
        self,
        group_id: str | None = None,
        period: list | tuple = (1, 30),
        capacity: int | float = 0,
        members: list[str] | None = None,
        member_caps: dict | None = None,
        *,
        period_id: str | int | None = None,
        actor_id: str = "ops-1",
        **extra: Any,
    ) -> dict:
        group_id = group_id or extra.pop("id", None)
        if group_id is None:
            raise ApiError(400, "invalid_group", "共享组必须包含 group_id 或 id")
        self.require_provisioning(actor_id)
        interval = _pair(period)
        assert interval is not None
        with self.lock:
            if group_id in self.groups:
                raise ApiError(409, "group_exists", f"共享组 {group_id} 已登记")
            pid = str(period_id or ("1" if interval == [1, 30] else f"{interval[0]}-{interval[1]}"))
            self._ensure_period(pid, interval[0], interval[1])
            group = {
                "id": group_id,
                "period_id": pid,
                "period": interval,
                "capacity": capacity,
                "member_caps": clone(member_caps or {}),
                "members": list(members or []),
                "status": "active",
            }
            self.groups[group_id] = group
            self.principals[f"admin-{group_id}"] = {
                "id": f"admin-{group_id}", "roles": ["group_admin"], "group_ids": [group_id]
            }
            for member_id in group["members"]:
                self._ensure_member(member_id)
                self.memberships.append({
                    "member_id": member_id, "group_id": group_id,
                    "start": interval[0], "end": None, "migration_id": None,
                })
            self.funds[f"base:{group_id}"] = {
                "id": f"base:{group_id}",
                "type": "base_pool",
                "owner_type": "group",
                "owner_id": group_id,
                "scope_group_id": group_id,
                "beneficiary_id": None,
                "capacity": capacity,
                "used": 0,
                "priority": 40,
                "valid_from": interval[0],
                "valid_to": interval[1],
                "portable": False,
                "transferable": False,
                "share": True,
                "status": "active",
            }
            self._audit("group.register", actor_id, group_id, group)
            return clone(group)

    def register_pack(
        self,
        pack_id: str | dict | None = None,
        owner_id: str | None = None,
        capacity: int | float = 0,
        *,
        share: bool | None = None,
        scope: str | None = None,
        period: list | tuple | None = None,
        portable: bool | None = None,
        transferable: bool | None = None,
        beneficiary_id: str | None = None,
        allowed_member_ids: list[str] | None = None,
        priority: int | None = None,
        actor_id: str = "ops-1",
        **extra: Any,
    ) -> dict:
        if isinstance(pack_id, dict):
            data = pack_id
            return self.register_pack(
                data.get("id") or data.get("pack_id"),
                data.get("owner") or data.get("owner_id"),
                data.get("capacity", 0),
                share=data.get("share"),
                scope=data.get("scope") or data.get("allowed_scope"),
                period=data.get("period"),
                portable=data.get("portable"),
                transferable=data.get("transferable"),
                beneficiary_id=data.get("beneficiary_id"),
                allowed_member_ids=data.get("allowed_member_ids"),
                priority=data.get("priority"),
                actor_id=actor_id,
                **{k: v for k, v in data.items() if k not in {
                    "id", "pack_id", "owner", "owner_id", "capacity", "share", "scope",
                    "allowed_scope", "period", "portable", "transferable",
                    "beneficiary_id", "allowed_member_ids", "priority"
                }},
            )
        pack_id = pack_id or extra.pop("id", None)
        owner_id = owner_id or extra.pop("owner", None)
        scope = scope or extra.pop("allowed_scope", None)
        self.require_provisioning(actor_id)
        if not pack_id or owner_id is None:
            raise ApiError(400, "invalid_pack", "流量包必须有 id 和 owner")
        with self.lock:
            if pack_id in self.funds:
                raise ApiError(409, "pack_exists", f"流量包 {pack_id} 已登记")
            owner_is_group = owner_id in self.groups or str(owner_id).startswith("g-")
            if period is not None:
                interval = _pair(period)
            elif owner_is_group and owner_id in self.groups:
                interval = self.groups[owner_id]["period"]
            else:
                first = next(iter(self.periods.values()), {"start": 1, "end": 30})
                interval = [first["start"], first["end"]]
            assert interval is not None
            if share is None:
                share = owner_is_group
            if scope is None:
                scope = "group_members" if owner_is_group and share else "owner_only"
            if portable is None:
                portable = not owner_is_group
            if transferable is None:
                transferable = False
            if not owner_is_group:
                self._ensure_member(owner_id)
            fund = {
                "id": pack_id,
                "type": "group_pack" if owner_is_group else "personal_pack",
                "owner_type": "group" if owner_is_group else "member",
                "owner_id": owner_id,
                "scope_group_id": owner_id if owner_is_group else extra.get("scope_group_id"),
                "beneficiary_id": beneficiary_id or (None if owner_is_group else owner_id),
                "capacity": capacity,
                "used": 0,
                "priority": priority if priority is not None else (30 if owner_is_group else 20),
                "valid_from": interval[0],
                "valid_to": interval[1],
                "portable": portable,
                "transferable": transferable,
                "share": share,
                "scope": scope,
                "allowed_member_ids": allowed_member_ids,
                "status": extra.get("status", "active"),
                "terms": extra.get("terms", {}),
            }
            self.funds[pack_id] = fund
            self._audit("pack.register", actor_id, pack_id, {"owner": owner_id, "capacity": capacity})
            return clone(fund)

    def add_commitment(
        self,
        group_id: str | None = None,
        member_id: str | None = None,
        amount: int | float | None = None,
        *,
        commitment_id: str | None = None,
        kind: str = "reserved",
        period_id: str | int | None = None,
        effective_at: int | float | None = None,
        expires_at: int | float | None = None,
        migration_id: str | None = None,
        create_fund: bool = True,
        portable: bool = True,
        actor_id: str = "ops-1",
        **extra: Any,
    ) -> dict:
        group_id = group_id or extra.pop("group", None)
        member_id = member_id or extra.pop("member", None)
        amount = amount if amount is not None else extra.pop("units", extra.pop("amount", None))
        if not group_id or not member_id or amount is None:
            raise ApiError(400, "invalid_commitment", "承诺必须包含 group/group_id、member/member_id 和 amount/units")
        self.require_provisioning(actor_id)
        cid = commitment_id or self._next_id("commit")
        with self.lock:
            if group_id not in self.groups:
                raise ApiError(404, "group_not_found", group_id)
            self._ensure_member(member_id)
            group = self.groups[group_id]
            pid = str(period_id or group["period_id"])
            self._ensure_period(pid, group["period"][0], group["period"][1])
            if self.available_for_commitment(group_id, pid) < amount and not migration_id:
                raise ApiError(409, "pool_capacity_exceeded", "承诺量超过共享组可用池", {
                    "group_id": group_id,
                    "available": self.available_for_commitment(group_id, pid),
                    "requested": amount,
                })
            fund_id = None
            if create_fund:
                fund_id = "promise:" + cid
                self.funds[fund_id] = {
                    "id": fund_id,
                    "type": "commitment",
                    "owner_type": "group",
                    "owner_id": group_id,
                    "scope_group_id": group_id,
                    "beneficiary_id": member_id,
                    "capacity": amount,
                    "used": 0,
                    "priority": 10,
                    "valid_from": effective_at if effective_at is not None else group["period"][0],
                    "valid_to": expires_at if expires_at is not None else group["period"][1],
                    "portable": portable,
                    "survives_migration": portable,
                    "transferable": False,
                    "share": False,
                    "scope": "named_beneficiary",
                    "status": "active",
                    "migration_id": migration_id,
                }
            row = {
                "id": cid,
                "group_id": group_id,
                "member_id": member_id,
                "period_id": pid,
                "amount": amount,
                "used_units": 0,
                "kind": kind,
                "status": "active" if effective_at is None else "scheduled",
                "effective_at": effective_at if effective_at is not None else group["period"][0],
                "expires_at": expires_at if expires_at is not None else group["period"][1],
                "migration_id": migration_id,
                "fund_id": fund_id,
            }
            self.commitments[cid] = row
            self._audit("commitment.add", actor_id, cid, clone(row))
            return clone(row)

    def load_register(self, data: dict, *, actor_id: str = "system") -> dict:
        with self.lock:
            if self._register_loaded:
                if self._register_source == data:
                    return {"idempotent": True}
                raise ApiError(409, "register_already_loaded", "台账已装载且内容不一致")
            if not isinstance(data, dict) or "pools" not in data:
                raise ApiError(400, "invalid_register", "台账必须包含 pools")
            for group_id, pool in data.get("pools", {}).items():
                self.register_group(
                    group_id,
                    pool.get("period", [1, 30]),
                    pool.get("capacity", 0),
                    pool.get("members", []),
                    pool.get("member_caps"),
                    period_id=pool.get("period_id"),
                    actor_id=actor_id,
                )
            for pack_id, pack in data.get("packs", {}).items():
                pack = dict(pack)
                pack.setdefault("id", pack_id)
                self.register_pack(pack, actor_id=actor_id)
            for arrangement in data.get("arrangements", []):
                member_id = arrangement["member"]
                explicit_group = arrangement.get("group")
                for key, value in arrangement.items():
                    if key == "member" or key == "group":
                        continue
                    if key.endswith("_reserved"):
                        group_id = explicit_group or key[: -len("_reserved")]
                        self.add_commitment(
                            group_id, member_id, value, kind="reserved",
                            commitment_id=f"arrangement:{group_id}:{member_id}",
                            actor_id=actor_id,
                        )
                    elif key.endswith("_promised"):
                        group_id = explicit_group or key[: -len("_promised")]
                        self.add_commitment(
                            group_id, member_id, value, kind="promised",
                            commitment_id=f"arrangement:{group_id}:{member_id}:promised",
                            actor_id=actor_id,
                        )
                    elif key in ("amount", "units") and explicit_group:
                        self.add_commitment(explicit_group, member_id, value, actor_id=actor_id)
            self._register_loaded = True
            self._register_source = clone(data)
            return {"loaded": True, "groups": len(self.groups), "funds": len(self.funds)}

    # ---------- 容量视图 ----------

    def commitments_for_group(self, group_id: str, period_id: str | None = None) -> list[dict]:
        return [
            c for c in self.commitments.values()
            if c["group_id"] == group_id
            and c["status"] in ("scheduled", "active", "committed")
            and (period_id is None or c["period_id"] == str(period_id))
        ]

    def available_for_commitment(self, group_id: str, period_id: str | None = None,
                                 exclude_commitment_id: str | None = None) -> int | float:
        group = self.groups[group_id]
        pid = str(period_id or group["period_id"])
        total = sum(
            c["amount"] for c in self.commitments_for_group(group_id, pid)
            if c["id"] != exclude_commitment_id
        )
        return group["capacity"] - total

    def pool_view(self, group_id: str, actor_id: str = "system") -> dict:
        with self.lock:
            group = self.groups[group_id]
            commitments = self.commitments_for_group(group_id)
            committed = sum(c["amount"] for c in commitments)
            used = sum(f["used"] for f in self.funds.values()
                       if f.get("scope_group_id") == group_id and f["type"] != "base_pool")
            base_used = self.funds[f"base:{group_id}"]["used"]
            return {
                "group_id": group_id,
                "period_id": group["period_id"],
                "period": group["period"],
                "capacity": group["capacity"],
                "member_caps": clone(group.get("member_caps", {})),
                "committed_units": committed,
                "available_for_commitment_units": group["capacity"] - committed,
                "commitment_used_units": used,
                "base_used_units": base_used,
                "total_used_units": used + base_used,
                "physical_remaining_units": group["capacity"] - used - base_used,
            }

    def member_cap_view(self, group_id: str, member_id: str,
                        period_id: str | None = None) -> dict:
        with self.lock:
            group = self.groups[group_id]
            pid = str(period_id or group["period_id"])
            cap = group.get("member_caps", {}).get(member_id)
            used = 0
            for allocation in self.allocations:
                if allocation["member_id"] != member_id:
                    continue
                if allocation.get("context_group_id") != group_id or allocation["period_id"] != pid:
                    continue
                if allocation["status"] not in ("active", "delta"):
                    continue
                sign = -1 if allocation["direction"] == "credit" else 1
                used += sign * allocation["signed_units"]
            return {
                "group_id": group_id,
                "member_id": member_id,
                "period_id": pid,
                "member_cap_units": cap,
                "used_units": used,
                "remaining_units": None if cap is None else cap - used,
                "exceeded": cap is not None and used > cap,
            }

    # ---------- 迁组 ----------

    def _group_at(self, member_id: str, at: int | float) -> str | None:
        candidates = [m for m in self.memberships if m["member_id"] == member_id]
        for membership in candidates:
            if membership["start"] <= at and (membership["end"] is None or at < membership["end"]):
                return membership["group_id"]
        if candidates:
            return sorted(candidates, key=lambda m: m["start"])[-1]["group_id"]
        return None

    def request_migration(
        self,
        member_id: str | None = None,
        from_group_id: str | None = None,
        to_group_id: str | None = None,
        effective_at: int | float | None = None,
        *,
        migration_id: str | None = None,
        member_consent: bool = True,
        promised_units: int | float | None = None,
        at: int | float | None = None,
        actor_id: str = "system",
        **extra: Any,
    ) -> dict:
        member_id = member_id or extra.get("member")
        from_group_id = from_group_id or extra.get("from") or extra.get("from_group")
        to_group_id = to_group_id or extra.get("to") or extra.get("to_group")
        effective_at = effective_at if effective_at is not None else extra.get("requested_at")
        if not member_id or not from_group_id or not to_group_id or effective_at is None:
            raise ApiError(400, "invalid_migration",
                           "迁组必须包含 member/member_id、from/from_group_id、to/to_group_id 和 effective_at/requested_at")
        principal = self.principal(actor_id)
        roles = set(principal.get("roles", []))
        is_system = "*" in roles or "provisioning" in roles
        is_member = "member" in roles and principal.get("member_id") == member_id
        is_group_admin = "group_admin" in roles and bool(
            {from_group_id, to_group_id} & set(principal.get("group_ids", []))
        )
        if actor_id != "system" and not (is_system or is_member or is_group_admin):
            raise ApiError(403, "migration_request_forbidden",
                           "只有成员本人、相关组管理员或经办可以发起迁组")
        with self.lock:
            self._ensure_member(member_id)
            if from_group_id not in self.groups or to_group_id not in self.groups:
                raise ApiError(404, "group_not_found", "迁组涉及的共享组必须已登记")
            mid = migration_id or f"mig:{member_id}:{from_group_id}:{to_group_id}:{effective_at}"
            if mid in self.migrations:
                return clone(self.migrations[mid])
            active_pending = [
                m for m in self.migrations.values()
                if m["member_id"] == member_id and m["status"] not in
                ("effective", "cancelled", "rejected", "capacity_failed")
            ]
            if active_pending:
                raise ApiError(409, "migration_pending", "该成员已有待完成迁组", {
                    "migration_ids": [m["id"] for m in active_pending]
                })
            current = self._group_at(member_id, effective_at if False else (at or self.groups[from_group_id]["period"][0]))
            if current is not None and current != from_group_id:
                # effective_at 可能是未来；仍允许管理员指定 from，但要保留真实台账。
                if not any(m["group_id"] == from_group_id for m in self.memberships
                           if m["member_id"] == member_id):
                    raise ApiError(409, "not_group_member", "成员不属于原共享组")
            row = {
                "id": mid,
                "member_id": member_id,
                "from_group_id": from_group_id,
                "to_group_id": to_group_id,
                "effective_at": effective_at,
                "requested_at": extra.get("requested_at", effective_at),
                "received_at": at,
                "status": "awaiting_consents",
                "member_consent_required": not member_consent,
                "consents": {
                    "member": {"granted": member_consent, "by": actor_id if member_consent else None, "at": at},
                    "from_group": None,
                    "to_group": None,
                },
                "promised_units": promised_units,
                "commitment_id": None,
                "membership_planned": False,
                "activated_at": None,
                "capacity_error": None,
            }
            self.migrations[mid] = row
            self._audit("migration.request", actor_id, mid, clone(row))
            return clone(row)

    def _consent_key(self, party: str, mig: dict) -> str:
        if party in ("member", mig["member_id"]):
            return "member"
        if party in ("from", "old", "old_group", mig["from_group_id"]):
            return "from_group"
        if party in ("to", "new", "new_group", mig["to_group_id"]):
            return "to_group"
        raise ApiError(400, "unknown_consent_party", f"无法识别同意方 {party}")

    def accept_migration(
        self,
        migration_id: str,
        party: str,
        *,
        at: int | float | None = None,
        promised_units: Any = UNSET,
        actor_id: str = "system",
        **extra: Any,
    ) -> dict:
        with self.lock:
            if migration_id not in self.migrations:
                raise ApiError(404, "migration_not_found", migration_id)
            mig = self.migrations[migration_id]
            key = self._consent_key(party, mig)
            if key == "from_group":
                self.require_group_admin(actor_id, mig["from_group_id"])
            elif key == "to_group":
                self.require_group_admin(actor_id, mig["to_group_id"])
            else:
                principal = self.principal(actor_id)
                if "*" not in principal.get("roles", []) and principal.get("member_id") != mig["member_id"]:
                    raise ApiError(403, "member_required", "需要成员本人同意")

            previous_promise = mig.get("promised_units", UNSET)
            if promised_units is not UNSET:
                mig["promised_units"] = promised_units
            if mig["consents"][key]:
                corrected_failed_promise = (
                    key == "to_group"
                    and mig["status"] == "capacity_failed"
                    and promised_units is not UNSET
                    and promised_units != previous_promise
                )
                if not corrected_failed_promise:
                    return self.migration_view(migration_id)
            else:
                mig["consents"][key] = {"granted": True, "by": actor_id, "at": at}
            self._audit("migration.consent", actor_id, migration_id, {"party": key, "at": at})
            try:
                self._finalize_migration_locked(mig)
            except ApiError as exc:
                mig["status"] = "capacity_failed"
                mig["capacity_error"] = {"code": exc.code, "message": exc.message, "details": exc.details}
                self._audit("migration.capacity_failed", actor_id, migration_id, exc.details)
                raise
            return self.migration_view(migration_id)

    def member_accept_migration(self, migration_id: str, *, at=None, actor_id: str | None = None):
        mig = self.migrations[migration_id]
        actor_id = actor_id or mig["member_id"]
        return self.accept_migration(migration_id, "member", at=at, actor_id=actor_id)

    def _finalize_migration_locked(self, mig: dict) -> None:
        if mig.get("promised_units") is not None and not mig.get("commitment_id"):
            group = self.groups[mig["to_group_id"]]
            available = self.available_for_commitment(mig["to_group_id"], group["period_id"])
            if available < mig["promised_units"]:
                raise ApiError(409, "new_group_capacity_exceeded",
                               "新组当期承诺超过自身可用池，不能生效", {
                                   "group_id": mig["to_group_id"],
                                   "available_units": available,
                                   "requested_promise_units": mig["promised_units"],
                               })
        if not mig["consents"]["from_group"] or not mig["consents"]["to_group"]:
            mig["status"] = "partially_accepted"
            return
        if mig["member_consent_required"] and not (mig["consents"]["member"] or {}).get("granted"):
            mig["status"] = "accepted_pending_member"
            return

        if mig.get("promised_units") is not None and not mig.get("commitment_id"):
            group = self.groups[mig["to_group_id"]]
            commitment = self.add_commitment(
                mig["to_group_id"], mig["member_id"], mig["promised_units"],
                kind="migration_promise",
                period_id=group["period_id"],
                effective_at=mig["effective_at"],
                migration_id=mig["id"],
                commitment_id=f"commit:{mig['id']}",
                actor_id="system",
            )
            mig["commitment_id"] = commitment["id"]

        if not mig["membership_planned"]:
            open_memberships = [
                m for m in self.memberships
                if m["member_id"] == mig["member_id"] and m["end"] is None
                and m["group_id"] == mig["from_group_id"]
            ]
            if not open_memberships:
                # 兼容重复装载：取最近一段未结束关系。
                open_memberships = [
                    m for m in self.memberships
                    if m["member_id"] == mig["member_id"] and m["end"] is None
                ]
            if open_memberships:
                old = sorted(open_memberships, key=lambda m: m["start"])[-1]
                old["end"] = mig["effective_at"]
            self.memberships.append({
                "member_id": mig["member_id"],
                "group_id": mig["to_group_id"],
                "start": mig["effective_at"],
                "end": None,
                "migration_id": mig["id"],
            })
            mig["membership_planned"] = True
        mig["status"] = "accepted"
        mig["capacity_error"] = None

    def activate_migration(self, migration_id: str, *, at: int | float | None = None,
                           actor_id: str = "system") -> dict:
        with self.lock:
            mig = self.migrations[migration_id]
            if mig["status"] == "effective":
                return self.migration_view(migration_id)
            if mig["status"] not in ("accepted",) and not (
                mig["consents"]["from_group"] and mig["consents"]["to_group"]
            ):
                raise ApiError(409, "migration_not_accepted", "迁组尚未取得两边同意")
            if at is not None and at < mig["effective_at"]:
                raise ApiError(409, "before_effective_at", "尚未到生效时间", {
                    "effective_at": mig["effective_at"], "at": at
                })
            self._finalize_migration_locked(mig)
            mig["status"] = "effective"
            mig["activated_at"] = at if at is not None else mig["effective_at"]
            if mig.get("commitment_id"):
                self.commitments[mig["commitment_id"]]["status"] = "active"
            self._audit("migration.effective", actor_id, migration_id, {"at": mig["activated_at"]})
            return self.migration_view(migration_id)

    def activate_due(self, at: int | float) -> list[dict]:
        result = []
        with self.lock:
            for mid, mig in list(self.migrations.items()):
                if mig["status"] == "accepted" and mig["effective_at"] <= at:
                    result.append(self.activate_migration(mid, at=at))
        return result

    def migration_view(self, migration_id: str, actor_id: str = "system") -> dict:
        with self.lock:
            mig = clone(self.migrations[migration_id])
            mig["both_group_consented"] = bool(mig["consents"]["from_group"] and mig["consents"]["to_group"])
            mig["member_consented"] = bool((mig["consents"].get("member") or {}).get("granted"))
            mig["effective"] = mig["status"] == "effective" or (
                mig["status"] == "accepted" and mig["effective_at"] <= 30
            )
            return mig

    # ---------- 用量与归属 ----------

    def report_usage(
        self,
        member_id: str | dict | None = None,
        window: list | tuple | None = None,
        units: int | float | None = None,
        *,
        usage_id: str | None = None,
        received_at: int | float | None = None,
        at: int | float | None = None,
        source: str = "meter",
        can_split: bool | None = None,
        slices: list[dict] | None = None,
        sequence: int | None = None,
        actor_id: str = "billing-1",
        **extra: Any,
    ) -> dict:
        if isinstance(member_id, dict):
            event = member_id
            return self.report_usage(
                event.get("member_id") or event.get("member"),
                event.get("window"),
                event.get("units"),
                usage_id=event.get("usage_id") or event.get("ref"),
                received_at=event.get("received_at") or event.get("at"),
                source=event.get("source", "meter"),
                can_split=event.get("can_split"),
                slices=event.get("slices") or event.get("split"),
                sequence=event.get("sequence"),
                actor_id=actor_id,
                **{k: v for k, v in event.items() if k not in {
                    "member", "member_id", "window", "units", "usage_id", "ref",
                    "received_at", "at", "source", "can_split", "slices", "split", "sequence"
                }},
            )
        member_id = member_id or extra.get("member")
        usage_id = usage_id or extra.get("ref")
        self.require_billing(actor_id)
        if not member_id or window is None or units is None:
            raise ApiError(400, "invalid_usage", "用量记录必须包含 member/window/units")
        interval = _pair(window)
        assert interval is not None
        if not isinstance(units, (int, float)) or units < 0:
            raise ApiError(400, "invalid_units", "units 必须是非负数字")
        uid = usage_id or extra.get("ref") or f"{member_id}-{interval[0]}-{interval[1]}"
        received_at = received_at if received_at is not None else at
        with self.lock:
            self._ensure_member(member_id)
            if uid in self.usage:
                old = self.usage[uid]
                if old["units"] != units or old["window"] != interval:
                    raise ApiError(409, "usage_conflict", "同一用量编号的区间或数量不一致；纠正必须使用差额记录", {
                        "usage_id": uid
                    })
                return self.usage_view(uid)
            if received_at is not None:
                self.activate_due(received_at)
            rec = {
                "id": uid,
                "member_id": member_id,
                "window": interval,
                "units": units,
                "source": source,
                "received_at": received_at,
                "sequence": sequence,
                "can_split": can_split,
                "requested_slices": clone(slices),
                "evidence": None,
                "evidence_status": "not_checked",
                "split_status": "unknown",
                "status": "received",
                "attribution": {"slices": [], "hold_ids": []},
                "allocation_ids": [],
                "correction_ids": [],
            }
            self.usage[uid] = rec
            self._audit("usage.report", actor_id, uid, {"units": units, "window": interval})
            self._process_usage_locked(rec)
            period_id = self._period_id_for_window(interval, None)
            period = self.periods.get(period_id)
            if period and (period.get("closed") or period.get("signed")):
                self._append_late_usage_correction_locked(rec, period_id)
            return self.usage_view(uid)

    def _append_late_usage_correction_locked(self, rec: dict, period_id: str) -> dict:
        impact = self._impact_by_fund_locked(rec["id"])
        correction = {
            "id": self._next_id("corr"),
            "type": "late_usage_difference",
            "original_period_id": period_id,
            "period_id": period_id,
            "posting_period_id": self._next_period_id(period_id),
            "member_id": rec["member_id"],
            "usage_id": rec["id"],
            "reason": "late_usage_after_period_lock",
            "delta_units": sum(impact.values()),
            "delta_by_fund": impact,
            "allocation_ids": list(rec["allocation_ids"]),
            "charge_line_ids": [],
            "created_at": rec.get("received_at"),
            "status": "posted_delta",
            "explanation_zh": "用量在签账后补传；不修改已签账快照，以本差额记录进入后续结算。",
        }
        self.corrections.append(correction)
        rec["correction_ids"].append(correction["id"])
        for allocation in self.allocations:
            if allocation["usage_id"] == rec["id"] and allocation.get("correction_id") is None:
                allocation["status"] = "delta"
                allocation["correction_id"] = correction["id"]
        for line in self.charge_lines:
            if line["usage_id"] == rec["id"] and line.get("correction_id") is None:
                line["correction_id"] = correction["id"]
                correction["charge_line_ids"].append(line["id"])
        return correction

    def _migration_boundaries(self, member_id: str) -> list[dict]:
        boundaries = []
        for m in self.migrations.values():
            if m["member_id"] == member_id and m.get("membership_planned"):
                boundaries.append({"at": m["effective_at"], "migration_id": m["id"],
                                   "from": m["from_group_id"], "to": m["to_group_id"]})
        return sorted(boundaries, key=lambda x: x["at"])

    def _crossing_boundary(self, member_id: str, window: list) -> dict | None:
        for boundary in self._migration_boundaries(member_id):
            if window[0] < boundary["at"] < window[1]:
                return boundary
        return None

    def _make_hold_locked(self, rec: dict, units: float | int, window: list, reason: str,
                         boundary: dict | None = None) -> str:
        hid = self._next_id("hold")
        hold = {
            "id": hid,
            "usage_id": rec["id"],
            "member_id": rec["member_id"],
            "window": clone(window),
            "units": units,
            "reason": reason,
            "boundary": clone(boundary),
            "billable_units": 0,
            "status": "held_unverified",
            "created_at": rec.get("received_at"),
        }
        self.holds[hid] = hold
        rec["attribution"]["hold_ids"].append(hid)
        return hid

    def _process_usage_locked(self, rec: dict) -> None:
        slices: list[dict] = []
        if rec.get("requested_slices"):
            raw_parts = rec["requested_slices"]
            total_part_units = 0
            for index, part in enumerate(raw_parts):
                if not isinstance(part, dict):
                    continue
                units = part.get("units", part.get("amount"))
                window = _pair(part.get("window") or [part.get("start"), part.get("end")])
                if units is None or window is None:
                    continue
                total_part_units += units
                gid = part.get("group_id") or part.get("group")
                crosses = self._crossing_boundary(rec["member_id"], window)
                slice_row = {
                    "id": f"{rec['id']}-slice-{index + 1}",
                    "window": window,
                    "units": units,
                    "group_id": gid,
                    "explicit": True,
                    "attribution": "explicit",
                }
                if not gid and crosses:
                    hid = self._make_hold_locked(rec, units, window,
                                                 "explicit_part_still_crosses_boundary", crosses)
                    slice_row["hold_id"] = hid
                else:
                    slice_row["group_id"] = gid or self._group_at(rec["member_id"], window[0])
                    if not slice_row["group_id"]:
                        hid = self._make_hold_locked(rec, units, window, "membership_not_found")
                        slice_row["hold_id"] = hid
                    else:
                        slices.append(slice_row)
                rec["attribution"]["slices"].append(slice_row)
            residual = rec["units"] - total_part_units
            if residual > 0:
                self._make_hold_locked(rec, residual, rec["window"], "unexplained_residual_after_clear_parts",
                                       self._crossing_boundary(rec["member_id"], rec["window"]))
            elif residual < 0:
                raise ApiError(400, "split_exceeds_usage", "拆分明细数量超过原始用量")
        else:
            boundary = self._crossing_boundary(rec["member_id"], rec["window"])
            if boundary is None:
                gid = self._group_at(rec["member_id"], rec["window"][0])
                slice_row = {
                    "id": f"{rec['id']}-slice-1",
                    "window": clone(rec["window"]),
                    "units": rec["units"],
                    "group_id": gid,
                    "explicit": False,
                    "attribution": "whole_window",
                }
                rec["attribution"]["slices"].append(slice_row)
                if gid:
                    slices.append(slice_row)
                else:
                    self._make_hold_locked(rec, rec["units"], rec["window"], "membership_not_found")
            else:
                # 没有合作方/经办确认的数量拆分时，即使知道时间边界，也不能推算比例。
                rec["attribution"]["boundary"] = boundary
                self._make_hold_locked(
                    rec, rec["units"], rec["window"],
                    "cross_migration_without_quantitative_split", boundary
                )

        for slice_row in slices:
            self._allocate_slice_locked(rec, slice_row)

    def _fund_remaining_locked(self, fund: dict) -> int | float:
        if fund["type"] == "base_pool":
            group = self.groups[fund["owner_id"]]
            committed = sum(
                c["amount"] for c in self.commitments_for_group(fund["owner_id"], group["period_id"])
            )
            uncommitted = group["capacity"] - committed
            return uncommitted - fund["used"]
        return fund["capacity"] - fund["used"]

    def _fund_eligible(self, fund: dict, member_id: str, group_id: str, at: int | float) -> bool:
        if fund.get("status") != "active":
            return False
        if not (fund["valid_from"] <= at <= fund["valid_to"]):
            return False
        if fund.get("allowed_member_ids") and member_id not in fund["allowed_member_ids"]:
            return False
        ftype = fund["type"]
        if ftype == "personal_pack":
            return fund.get("owner_id") == member_id or fund.get("beneficiary_id") == member_id
        if ftype == "commitment":
            if fund.get("beneficiary_id") != member_id:
                return False
            return fund.get("owner_id") == group_id or bool(fund.get("portable"))
        if ftype in ("group_pack", "base_pool"):
            return fund.get("scope_group_id") == group_id or fund.get("owner_id") == group_id
        return False

    def _allocation_priority(self, fund: dict, context_group: str) -> tuple:
        explicit = fund.get("priority", 100)
        cross_group_penalty = 0
        if fund["type"] == "commitment" and fund.get("owner_id") != context_group:
            cross_group_penalty = 15
        return (explicit + cross_group_penalty, fund.get("valid_to", 0), fund["id"])

    def _allocate_slice_locked(self, rec: dict, slice_row: dict, *, correction: dict | None = None) -> list[dict]:
        member_id = rec["member_id"]
        group_id = slice_row["group_id"]
        at = slice_row["window"][0]
        period_id = self._period_id_for_window(slice_row["window"], group_id)
        candidates = [
            f for f in self.funds.values()
            if self._fund_eligible(f, member_id, group_id, at)
            and self._fund_remaining_locked(f) > 0
        ]
        candidates.sort(key=lambda f: self._allocation_priority(f, group_id))
        remaining = slice_row["units"]
        created = []
        for fund in candidates:
            if remaining <= 0:
                break
            available = self._fund_remaining_locked(fund)
            quantity = min(remaining, available)
            group = self.groups.get(group_id, {})
            member_cap = group.get("member_caps", {}).get(member_id)
            subject_to_cap = fund.get(
                "counts_toward_member_cap",
                fund["type"] in ("base_pool", "group_pack")
            )
            cap_remaining = None
            if member_cap is not None and subject_to_cap:
                cap_view = self.member_cap_view(group_id, member_id, period_id)
                cap_remaining = max(0, member_cap - cap_view["used_units"])
                quantity = min(quantity, cap_remaining)
            if quantity <= 0:
                continue
            allocation = {
                "id": self._next_id("alloc"),
                "usage_id": rec["id"],
                "member_id": member_id,
                "period_id": period_id,
                "window": clone(slice_row["window"]),
                "units": quantity,
                "signed_units": quantity,
                "direction": "debit",
                "fund_id": fund["id"],
                "fund_type": fund["type"],
                "owner_group_id": fund.get("owner_id") if fund.get("owner_type") == "group" else None,
                "context_group_id": group_id,
                "explicit_slice": slice_row.get("explicit", False),
                "status": "delta" if correction else "active",
                "correction_id": correction["id"] if correction else None,
                "created_at": rec.get("received_at"),
            }
            self.allocations.append(allocation)
            rec["allocation_ids"].append(allocation["id"])
            fund["used"] += quantity
            if correction:
                correction["allocation_ids"].append(allocation["id"])
            created.append(allocation)
            remaining -= quantity

        if remaining > 0:
            group = self.groups.get(group_id, {})
            price = group.get("overage_unit_price")
            member_cap = group.get("member_caps", {}).get(member_id)
            cap_view = self.member_cap_view(group_id, member_id, period_id) if member_cap is not None else None
            cap_limited = bool(cap_view and cap_view["remaining_units"] <= 0)
            line = {
                "id": self._next_id("charge"),
                "usage_id": rec["id"],
                "member_id": member_id,
                "period_id": period_id,
                "context_group_id": group_id,
                "units": remaining,
                "covered": False,
                "charge_units": remaining if not cap_limited else 0,
                "unit_price": price,
                "amount": 0 if cap_limited else (remaining * price if isinstance(price, (int, float)) else None),
                "rating_status": "member_cap_review" if cap_limited else (
                    "rated" if isinstance(price, (int, float)) else "rate_not_configured"
                ),
                "reason": "member_cap_limit_reached" if cap_limited else "eligible_entitlements_exhausted",
                "correction_id": correction["id"] if correction else None,
            }
            self.charge_lines.append(line)
            if correction:
                correction["charge_line_ids"].append(line["id"])
        return created

    def usage_view(self, usage_id: str, actor_id: str = "system") -> dict:
        with self.lock:
            rec = self.usage[usage_id]
            data = clone(rec)
            data["allocations"] = [
                clone(a) for a in self.allocations if a["usage_id"] == usage_id
            ]
            data["holds"] = [clone(self.holds[hid]) for hid in rec["attribution"]["hold_ids"]]
            data["charge_lines"] = [clone(c) for c in self.charge_lines if c["usage_id"] == usage_id]
            data["explanation_zh"] = self._usage_explanation_locked(rec, data["allocations"], data["holds"])
            return data

    def _usage_explanation_locked(self, rec: dict, allocations: list[dict], holds: list[dict]) -> list[str]:
        lines = []
        for hold in holds:
            if hold["reason"] == "cross_migration_without_quantitative_split":
                b = hold.get("boundary") or {}
                lines.append(
                    f"用量 {hold['units']} 单位的区间 {hold['window']} 跨越迁组生效点 "
                    f"{b.get('at')}；缺少可核实的数量拆分，已暂挂且本期不形成扣费。"
                )
            else:
                lines.append(f"用量 {hold['units']} 单位因 {hold['reason']} 暂挂，暂不扣费。")
        for allocation in allocations:
            if allocation["direction"] == "credit":
                continue
            fund = self.funds.get(allocation["fund_id"], {})
            lines.append(
                f"区间 {allocation['window']} 中 {allocation['units']} 单位明确归属 "
                f"{allocation['context_group_id']}，由 {allocation['fund_id']}"
                f"（{fund.get('type', 'fund')}）抵扣，可作为实际扣费依据。"
            )
        if not lines:
            lines.append("用量已接收，尚无可解释的归属结果。")
        return lines

    # ---------- 合作方证据与差额 ----------

    def attach_usage_evidence(
        self,
        usage_id: str,
        evidence: dict,
        *,
        actor_id: str = "billing-1",
        source: str = "partner_receipt",
    ) -> dict:
        self.require_billing(actor_id)
        with self.lock:
            if usage_id not in self.usage:
                raise ApiError(404, "usage_not_found", usage_id)
            rec = self.usage[usage_id]
            rec["evidence"] = clone(evidence)
            rec["evidence_status"] = "verified"
            if evidence.get("units") is not None and evidence["units"] != rec["units"]:
                rec["evidence_status"] = "conflict"
            if evidence.get("window") is not None and list(evidence["window"]) != rec["window"]:
                rec["evidence_status"] = "conflict"
            split = evidence.get("split", UNSET)
            if split is None:
                rec["split_status"] = "unavailable"
                rec["can_split"] = False
                self._audit("usage.evidence_unavailable_split", actor_id, usage_id)
                return self.usage_view(usage_id)
            if split is not UNSET:
                rec["split_status"] = "available"
                rec["requested_slices"] = split
                period_id = self._period_id_for_window(rec["window"], None)
                period = self.periods.get(period_id, {})
                if period.get("signed") or period.get("closed"):
                    self._reassess_usage_by_delta_locked(rec, split, "verified_split_after_close")
                else:
                    for allocation in self.allocations:
                        if allocation["usage_id"] == usage_id and allocation["status"] == "active":
                            self.funds[allocation["fund_id"]]["used"] -= allocation["units"]
                            allocation["status"] = "superseded"
                    for hid in rec["attribution"]["hold_ids"]:
                        self.holds[hid]["status"] = "superseded"
                    rec["attribution"] = {"slices": [], "hold_ids": []}
                    rec["allocation_ids"] = []
                    rec["requested_slices"] = split
                    self._process_usage_locked(rec)
                self._audit("usage.evidence_split", actor_id, usage_id)
            return self.usage_view(usage_id)

    def _impact_by_fund_locked(self, usage_id: str) -> dict[str, float | int]:
        impact: dict[str, float | int] = {}
        for allocation in self.allocations:
            if allocation["usage_id"] == usage_id and allocation["status"] in ("active", "delta"):
                sign = -1 if allocation["direction"] == "credit" else 1
                impact[allocation["fund_id"]] = impact.get(allocation["fund_id"], 0) + sign * allocation["signed_units"]
        return impact

    def _reassess_usage_by_delta_locked(self, rec: dict, split: list, reason: str) -> dict:
        period_id = self._period_id_for_window(rec["window"], None)
        old_impact = self._impact_by_fund_locked(rec["id"])
        correction = {
            "id": self._next_id("corr"),
            "type": "post_signing_difference",
            "original_period_id": period_id,
            "period_id": period_id,
            "posting_period_id": self._next_period_id(period_id),
            "member_id": rec["member_id"],
            "usage_id": rec["id"],
            "reason": reason,
            "delta_units": 0,
            "delta_by_fund": {},
            "old_impact_by_fund": old_impact,
            "new_impact_by_fund": {},
            "allocation_ids": [],
            "charge_line_ids": [],
            "created_at": rec.get("received_at"),
            "status": "posted_delta",
        }
        self.corrections.append(correction)
        rec["correction_ids"].append(correction["id"])
        for hid in rec["attribution"].get("hold_ids", []):
            self.holds[hid]["status"] = "resolved_by_delta"
            self.holds[hid]["correction_id"] = correction["id"]

        # 对旧影响补反向差额，不修改已签账快照。
        for fund_id, units in old_impact.items():
            if units:
                credit = {
                    "id": self._next_id("alloc"),
                    "usage_id": rec["id"],
                    "member_id": rec["member_id"],
                    "period_id": period_id,
                    "window": clone(rec["window"]),
                    "units": units,
                    "signed_units": -units,
                    "direction": "credit",
                    "fund_id": fund_id,
                    "fund_type": self.funds[fund_id]["type"],
                    "owner_group_id": self.funds[fund_id].get("owner_id"),
                    "context_group_id": self.funds[fund_id].get("scope_group_id"),
                    "status": "delta",
                    "correction_id": correction["id"],
                }
                self.allocations.append(credit)
                self.funds[fund_id]["used"] = max(0, self.funds[fund_id]["used"] - units)
                correction["allocation_ids"].append(credit["id"])

        # 按新证据生成正向差额。
        previous_slices = rec.get("requested_slices")
        previous_attribution = clone(rec["attribution"])
        previous_allocation_ids = list(rec["allocation_ids"])
        rec["requested_slices"] = split
        rec["attribution"] = {"slices": [], "hold_ids": []}
        rec["allocation_ids"] = []
        self._process_usage_locked(rec)
        # _process 不知道 correction id；把刚生成的记录标记到本差额。
        for allocation in self.allocations:
            if allocation["usage_id"] == rec["id"] and allocation.get("correction_id") is None \
                    and allocation["id"] in rec["allocation_ids"]:
                allocation["status"] = "delta"
                allocation["correction_id"] = correction["id"]
                correction["allocation_ids"].append(allocation["id"])
        for line in self.charge_lines:
            if line["usage_id"] == rec["id"] and line.get("correction_id") is None:
                line["correction_id"] = correction["id"]
                correction["charge_line_ids"].append(line["id"])

        new_impact = self._impact_by_fund_locked(rec["id"])
        fund_ids = set(old_impact) | set(new_impact)
        delta_by_fund = {fid: new_impact.get(fid, 0) - old_impact.get(fid, 0) for fid in fund_ids}
        correction["delta_by_fund"] = {fid: v for fid, v in delta_by_fund.items() if v}
        correction["new_impact_by_fund"] = new_impact
        correction["delta_units"] = sum(correction["delta_by_fund"].values())
        correction["explanation_zh"] = (
            f"原账期 {period_id} 已签账；依据补传证据生成后续差额 "
            f"{correction['id']}，不直接改写已签账记录。"
        )
        return correction

    def _next_period_id(self, period_id: str) -> str:
        try:
            return str(int(period_id) + 1)
        except ValueError:
            return period_id + ":adjustment"

    def create_correction(
        self,
        period_id: str,
        member_id: str,
        *,
        delta_units: int | float = 0,
        delta_by_fund: dict | None = None,
        reason: str = "manual_correction",
        usage_id: str | None = None,
        actor_id: str = "billing-1",
    ) -> dict:
        self.require_billing(actor_id)
        with self.lock:
            pid = str(period_id)
            correction = {
                "id": self._next_id("corr"),
                "type": "manual_difference",
                "original_period_id": pid,
                "period_id": pid,
                "posting_period_id": self._next_period_id(pid),
                "member_id": member_id,
                "usage_id": usage_id,
                "reason": reason,
                "delta_units": delta_units,
                "delta_by_fund": clone(delta_by_fund or {}),
                "allocation_ids": [],
                "charge_line_ids": [],
                "status": "posted_delta",
                "explanation_zh": f"账期 {pid} 签账后的纠正以后续差额记录体现。",
            }
            self.corrections.append(correction)
            return clone(correction)

    # ---------- 关账/签账 ----------

    def close_period(self, period_id: str | int, *, at: int | float | None = None,
                     evidence: dict | None = None, sign: bool = True,
                     actor_id: str = "billing-1") -> dict:
        self.require_billing(actor_id)
        pid = str(period_id)
        with self.lock:
            period = self._ensure_period(pid)
            if period["closed"]:
                return self.period_view(pid)
            if at is not None:
                self.activate_due(at)
            period["closed"] = True
            period["status"] = "closed"
            period["closed_at"] = at
            if evidence:
                period["closing_evidence"] = clone(evidence)
                if evidence.get("locked"):
                    period["locked"] = True
            if sign:
                period["signed"] = True
                period["signed_at"] = at
                period["locked"] = True
            period["statement"] = self._statement_locked(pid)
            self._audit("period.close", actor_id, pid, {"signed": sign, "at": at})
            return self.period_view(pid)

    def sign_bill(self, period_id: str | int, *, at=None, actor_id="billing-1") -> dict:
        self.require_billing(actor_id)
        with self.lock:
            period = self._ensure_period(period_id)
            period["signed"] = True
            period["signed_at"] = at
            period["locked"] = True
            if period["statement"] is None:
                period["statement"] = self._statement_locked(period["id"])
            return self.period_view(period["id"])

    def _statement_locked(self, period_id: str) -> dict:
        allocations = [a for a in self.allocations if a["period_id"] == period_id]
        by_fund: dict[str, float | int] = {}
        by_group: dict[str, float | int] = {}
        total = 0
        for a in allocations:
            if a["direction"] != "debit" or a["status"] == "superseded":
                continue
            by_fund[a["fund_id"]] = by_fund.get(a["fund_id"], 0) + a["units"]
            gid = a.get("context_group_id") or "unknown"
            by_group[gid] = by_group.get(gid, 0) + a["units"]
            total += a["units"]
        period = self.periods[period_id]
        held = 0
        for hold in self.holds.values():
            window = hold.get("window") or []
            if hold["status"] == "held_unverified" and window and period["start"] <= window[0] <= period["end"]:
                held += hold["units"]
        return {
            "period_id": period_id,
            "allocated_units": total,
            "held_unverified_units": held,
            "by_fund": by_fund,
            "by_group": by_group,
            "charge_lines": len([c for c in self.charge_lines if c["period_id"] == period_id]),
            "note": "已签账快照不可被补传记录直接改写；后续变化以 correction 差额体现。",
        }

    def period_view(self, period_id: str, actor_id: str = "system") -> dict:
        with self.lock:
            period = clone(self.periods[str(period_id)])
            period["corrections"] = [clone(c) for c in self.corrections if c.get("period_id") == str(period_id)]
            return period

    # ---------- 客服/成员/管理员视图 ----------

    def _entitlement_views_locked(self, member_id: str, period_id: str, as_of: int | float) -> list[dict]:
        period = self.periods[period_id]
        current_group = self._group_at(member_id, as_of)
        views = []
        for fund in self.funds.values():
            related = fund.get("beneficiary_id") == member_id or fund.get("owner_id") == member_id
            member_in_scope_group = False
            if fund.get("scope_group_id"):
                member_in_scope_group = any(
                    m["member_id"] == member_id and m["group_id"] == fund["scope_group_id"]
                    for m in self.memberships
                )
            if not related and not (fund["type"] in ("base_pool", "group_pack") and member_in_scope_group):
                continue
            remaining = self._fund_remaining_locked(fund)
            eligible_now = self._fund_eligible(fund, member_id, current_group or "", as_of)
            reason = None if eligible_now else self._ineligibility_reason(fund, member_id, current_group, as_of)
            views.append({
                "fund_id": fund["id"],
                "type": fund["type"],
                "owner_type": fund.get("owner_type"),
                "owner_id": fund.get("owner_id"),
                "capacity": fund["capacity"],
                "used_units": fund["used"],
                "remaining_units": remaining,
                "portable": fund.get("portable", False),
                "transferable": fund.get("transferable", False),
                "scope": fund.get("scope"),
                "valid_window": [fund["valid_from"], fund["valid_to"]],
                "usable_at_as_of": eligible_now,
                "as_of": as_of,
                "context_group_id": current_group,
                "ineligibility_reason_zh": reason,
            })
        return views

    def _ineligibility_reason(self, fund: dict, member_id: str, current_group: str | None,
                              at: int | float) -> str:
        if not fund["valid_from"] <= at <= fund["valid_to"]:
            return "不在流量包有效期内"
        if fund["type"] in ("group_pack", "base_pool") and fund.get("scope_group_id") != current_group:
            return "该权益仅限原共享组内在网成员使用，不随迁组默认转移"
        if fund["type"] == "commitment" and fund.get("owner_id") != current_group and not fund.get("portable"):
            return "原组承诺仅限原组上下文使用"
        if self._fund_remaining_locked(fund) <= 0:
            return "权益额度已用完"
        return "当前不在允许使用范围内"

    def member_charge_basis(
        self,
        member_id: str,
        period_id: str | int = "current",
        *,
        as_of: int | float | None = None,
        actor_id: str = "system",
    ) -> dict:
        with self.lock:
            pid = self.current_period_id(as_of) if str(period_id) == "current" else str(period_id)
            self._member_access(actor_id, member_id, pid)
            if pid not in self.periods:
                self._ensure_period(pid)
            period = self.periods[pid]
            as_of = period["end"] if as_of is None else as_of
            usage_ids = [uid for uid, rec in self.usage.items() if rec["member_id"] == member_id]
            usage_views = [self.usage_view(uid) for uid in usage_ids]
            allocations = [
                clone(a) for a in self.allocations
                if a["member_id"] == member_id and a["period_id"] == pid
            ]
            holds = [clone(h) for h in self.holds.values() if h["member_id"] == member_id]
            corrections = [clone(c) for c in self.corrections if c.get("member_id") == member_id]
            entitlements = self._entitlement_views_locked(member_id, pid, as_of)
            deducted = sum(a["units"] if a["direction"] == "debit" else -a["units"]
                           for a in allocations if a["status"] in ("active", "delta"))
            held = sum(h["units"] for h in holds if h["status"] == "held_unverified")
            timeline = [
                {"group_id": m["group_id"], "start": m["start"], "end": m["end"],
                 "migration_id": m["migration_id"]}
                for m in self.memberships if m["member_id"] == member_id
            ]
            mig_views = [self.migration_view(mid) for mid, m in self.migrations.items()
                         if m["member_id"] == member_id]
            explanations = [
                "跨迁组时点但无可核实拆分的用量只暂挂，不会错误扣费。",
                "成员自购且有效的流量包按其范围继续适用；原组共享包不默认转入新组。",
                "已签账后的变化不覆盖原账单，而以后续差额记录体现。",
            ]
            return {
                "member_id": member_id,
                "period_id": pid,
                "period_window": [period["start"], period["end"]],
                "caliber": "signed_with_subsequent_differences" if period["signed"] else "current",
                "as_of": as_of,
                "period_status": {
                    "closed": period["closed"], "signed": period["signed"], "locked": period["locked"]
                },
                "membership_timeline": timeline,
                "migrations": mig_views,
                "entitlements": entitlements,
                "usage_records": usage_views,
                "allocations": allocations,
                "holds": holds,
                "corrections": corrections,
                "totals": {
                    "reported_units": sum(self.usage[u]["units"] for u in usage_ids),
                    "net_deducted_units": deducted,
                    "held_unverified_units": held,
                    "allocation_records": len(allocations),
                    "correction_records": len(corrections),
                },
                "signed_statement": clone(period.get("statement")),
                "allocation_policy_zh": (
                    "默认顺序：当前组对该成员的专项承诺、成员自购可携带包、跨组仍有效的原承诺、"
                    "组内加油包、未承诺基础池；登记流量包时可用 priority 覆盖顺序。"
                ),
                "explanations_zh": explanations,
            }

    def support_current_view(self, member_id: str, *, agent_id: str = "support-1",
                             as_of: int | float | None = None) -> dict:
        pid = self.current_period_id(as_of)
        view = self.member_charge_basis(member_id, pid, as_of=as_of, actor_id=agent_id)
        grant = self._support_authorized(agent_id, member_id, pid)
        view["support_authorization"] = clone(grant)
        view["service_caliber_zh"] = "已按成员授权展示当期口径；不得用于授权范围外查询。"
        return view

    def group_admin_view(self, group_id: str, *, period_id: str | int = "current",
                         actor_id: str | None = None) -> dict:
        actor_id = actor_id or f"admin-{group_id}"
        self.require_group_admin(actor_id, group_id)
        with self.lock:
            pid = self.current_period_id() if str(period_id) == "current" else str(period_id)
            group = self.groups[group_id]
            pool = self.pool_view(group_id, actor_id="system")
            commitments = []
            for c in self.commitments_for_group(group_id, pid):
                used = sum(
                    a["units"] if a["direction"] == "debit" else -a["units"]
                    for a in self.allocations
                    if a.get("fund_id") == c.get("fund_id") and a["status"] in ("active", "delta")
                )
                row = clone(c)
                row["used_units"] = used
                row["difference_units"] = c["amount"] - used
                row["effective_date"] = c["effective_at"]
                commitments.append(row)

            migration_rows = []
            for mig in self.migrations.values():
                if group_id not in (mig["from_group_id"], mig["to_group_id"]):
                    continue
                side = "outgoing" if mig["from_group_id"] == group_id else "incoming"
                own_commitment = next((c for c in commitments if c.get("migration_id") == mig["id"]), None)
                if own_commitment is None:
                    # 台账中的历史承诺可能先于迁组存在，没有 migration_id；按成员匹配。
                    own_commitment = next((c for c in commitments if c.get("member_id") == mig["member_id"]), None)
                related_holds = []
                for h in self.holds.values():
                    boundary = h.get("boundary") or {}
                    if boundary.get("migration_id") == mig["id"] and h["status"] == "held_unverified":
                        related_holds.append(h["units"])
                settled = own_commitment["used_units"] if own_commitment else 0
                promised = own_commitment["amount"] if own_commitment else mig.get("promised_units")
                migration_rows.append({
                    "migration_id": mig["id"],
                    "member_id": mig["member_id"],
                    "direction": side,
                    "status": mig["status"],
                    "both_group_consented": bool(mig["consents"]["from_group"] and mig["consents"]["to_group"]),
                    "effective_at": mig["effective_at"],
                    "effective_date": mig["effective_at"],
                    "own_promised_units": promised,
                    "own_settled_units": settled,
                    "own_commitment_difference_units": (promised - settled) if promised is not None else None,
                    "pending_verification_units": sum(related_holds),
                    "next_migration_allowed": mig["status"] == "effective",
                })

            other_members = []
            pending_member_ids = {
                m["member_id"] for m in self.migrations.values()
                if m["status"] not in ("effective", "cancelled", "rejected")
            }
            for member_id in group["members"]:
                cap = self.member_cap_view(group_id, member_id, pid)
                other_members.append({
                    "member_id": member_id,
                    "active_in_group": self._group_at(member_id, group["period"][1]) == group_id,
                    "has_pending_migration": member_id in pending_member_ids,
                    "member_cap_units": cap["member_cap_units"],
                    "member_cap_used_units": cap["used_units"],
                    "member_cap_remaining_units": cap["remaining_units"],
                    "member_cap_exceeded": cap["exceeded"],
                    "service_continuity": "unaffected_by_other_member_migration",
                })
            return {
                "group_id": group_id,
                "period_id": pid,
                "pool": pool,
                "commitments": commitments,
                "migrations": migration_rows,
                "members": other_members,
                "can_start_next_migration": True,
                "commitment_resale_protection": {
                    "locked_against_new_commitment_units": pool["committed_units"],
                    "policy_zh": "已承诺给成员、尤其尚待用量核实的额度继续占用可用池，不释放给新成员或再次承诺。",
                },
                "privacy": {
                    "communication_details": "redacted",
                    "policy_zh": "管理员仅可见承诺差额、生效日期和汇总待核实量；未获成员授权时不提供通信/用量明细。",
                },
            }

    # ---------- 资料端 intake / receipt 编排 ----------

    def ingest_feed_item(self, item: dict, *, actor_id: str = "system") -> dict:
        sequence = item.get("sequence")
        if sequence is not None:
            with self.lock:
                if sequence in self.processed_sequences:
                    return {"idempotent_sequence": sequence}
        facts = item.get("facts", {})
        at = item.get("at")
        source = item.get("source")
        result: dict[str, Any] = {"sequence": sequence, "effects": []}

        if ("from" in facts or "from_group" in facts) and ("to" in facts or "to_group" in facts):
            member_id = facts["member"]
            from_gid = facts.get("from", facts.get("from_group"))
            to_gid = facts.get("to", facts.get("to_group"))
            effective_at = facts.get("effective_at", facts.get("requested_at", at))
            mig = self.request_migration(
                member_id, from_gid, to_gid, effective_at,
                at=at, actor_id=actor_id,
                promised_units=facts.get("promised"),
                requested_at=facts.get("requested_at"),
            )
            mid = mig["id"]
            result["effects"].append({"migration_requested": mid})
            if facts.get("accept"):
                view = self.accept_migration(
                    mid, facts["accept"], at=at,
                    promised_units=facts.get("promised", UNSET), actor_id=actor_id
                )
                result["effects"].append({"accepted_by": facts["accept"], "status": view["status"]})
        elif facts.get("accept") and facts.get("member"):
            member_id = facts["member"]
            candidates = [
                m for m in self.migrations.values()
                if m["member_id"] == member_id
                and m["status"] not in ("effective", "cancelled", "rejected")
            ]
            if not candidates:
                raise ApiError(409, "migration_not_found", "收到单边同意，但找不到该成员的待办迁组")
            mig = sorted(candidates, key=lambda m: m["effective_at"])[-1]
            view = self.accept_migration(
                mig["id"], facts["accept"], at=at,
                promised_units=facts.get("promised", UNSET), actor_id=actor_id
            )
            result["effects"].append({"accepted_by": facts["accept"], "status": view["status"]})
        elif "window" in facts and "units" in facts:
            view = self.report_usage(
                facts.get("member", facts.get("member_id")),
                facts["window"], facts["units"],
                received_at=at, source=source or "meter",
                can_split=facts.get("can_split"),
                slices=facts.get("slices") or facts.get("split"),
                sequence=sequence, actor_id="billing-1",
            )
            result["effects"].append({"usage_reported": view["id"], "status": view["status"]})
        elif "closed" in facts or source == "billing":
            pid = facts.get("period", self.current_period_id(at))
            view = self.close_period(pid, at=at, sign=bool(facts.get("closed")), actor_id="billing-1")
            result["effects"].append({"period_closed": view["id"], "signed": view["signed"]})
        else:
            result["effects"].append({"ignored": "unrecognized_fact"})

        if sequence is not None:
            with self.lock:
                self.processed_sequences.add(sequence)
        return result

    def sync_partner_feed(self, base_url: str, *, retry_delayed: bool = True,
                          max_pages: int = 100) -> dict:
        client = PartnerClient(base_url)
        register = client.register()
        self.load_register(register)
        since = 0
        sequences = []
        for _ in range(max_pages):
            page = client.intake(since=since, limit=50)
            for item in page.get("items", []):
                self.ingest_feed_item(item)
                sequences.append(item.get("sequence"))
            cursor = page.get("cursor", since)
            if not page.get("items") or cursor == since:
                break
            since = cursor

        receipt_results = []
        for uid in list(self.usage):
            try:
                evidence = client.receipt("use-slice", "metering", uid)
                self.attach_usage_evidence(uid, evidence)
                receipt_results.append({"ref": uid, "status": "applied"})
            except ApiError as exc:
                if exc.code == "receipt_pending" and retry_delayed:
                    try:
                        evidence = client.receipt("use-slice", "metering", uid)
                        self.attach_usage_evidence(uid, evidence)
                        receipt_results.append({"ref": uid, "status": "applied_after_retry"})
                    except ApiError as second:
                        receipt_results.append({"ref": uid, "status": second.code})
                else:
                    receipt_results.append({"ref": uid, "status": exc.code})
        for pid, period in list(self.periods.items()):
            ref = f"month-{pid}"
            try:
                evidence = client.receipt("closing", "billing", ref)
                if period["closed"]:
                    with self.lock:
                        period["closing_evidence"] = evidence
                        if evidence.get("locked"):
                            period["locked"] = True
                receipt_results.append({"ref": ref, "status": "applied"})
            except ApiError as exc:
                receipt_results.append({"ref": ref, "status": exc.code})
        return {"sequences": sequences, "receipts": receipt_results}

    def sample_report(self) -> dict:
        return {
            "member_m2": self.member_charge_basis("m-2", "1", actor_id="system"),
            "admin_g_a": self.group_admin_view("g-a", actor_id="system"),
            "admin_g_b": self.group_admin_view("g-b", actor_id="system"),
        }


def build_sample_backend(*, apply_receipts: bool = True) -> FamilyBackend:
    """构造 samples 中完整业务情形的内存后端。"""
    register = json.loads((SAMPLES / "register.json").read_text(encoding="utf-8"))
    intake = json.loads((SAMPLES / "intake.json").read_text(encoding="utf-8"))
    receipts = json.loads((SAMPLES / "receipts.json").read_text(encoding="utf-8"))
    backend = FamilyBackend(register)
    for item in intake:
        backend.ingest_feed_item(item)
    if apply_receipts:
        for row in receipts:
            if row["action"] == "use-slice":
                # 样例标记 delayed：真实对端第一次 503，第二次返回同一 evidence。
                backend.attach_usage_evidence(row["ref"], row["evidence"])
            elif row["action"] == "closing":
                pid = row["ref"].split("-")[-1]
                with backend.lock:
                    period = backend.periods.get(pid)
                    if period:
                        period["closing_evidence"] = row["evidence"]
                        period["locked"] = bool(row["evidence"].get("locked"))
    return backend


# ---------- HTTP 层 ----------

class FamilyBackendServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, port: int = 8800, backend: FamilyBackend | None = None,
                 host: str = "127.0.0.1"):
        self.backend = backend or FamilyBackend()
        super().__init__((host, port), BackendHandler)


class BackendHandler(BaseHTTPRequestHandler):
    server: FamilyBackendServer

    def send_json(self, status: int, data: Any) -> None:
        payload = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0") or "0")
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {"_raw": data}
        except json.JSONDecodeError as exc:
            raise ApiError(400, "invalid_json", str(exc)) from exc

    def actor(self, query: dict) -> str:
        return self.headers.get("X-Actor-Id") or query.get("actor_id", ["anonymous"])[0]

    def do_GET(self) -> None:  # noqa: N802
        try:
            url = urlsplit(self.path)
            query = parse_qs(url.query, keep_blank_values=True)
            path = [p for p in url.path.split("/") if p]
            backend = self.server.backend
            actor = self.actor(query)
            if path == ["health"]:
                self.send_json(200, {"ok": True, "service": "family-backend"})
            elif path in ([], ["v1"]):
                self.send_json(200, {
                    "service": "family-backend",
                    "health": "/health",
                    "endpoints": [
                        "/v1/groups", "/v1/packs", "/v1/commitments", "/v1/migrations",
                        "/v1/usage", "/v1/periods/{period}/close",
                        "/v1/members/{member}/charge-basis",
                        "/v1/support/members/{member}/current",
                        "/v1/groups/{group}/admin-view",
                        "/v1/sync/partner",
                    ],
                })
            elif path == ["v1", "groups"]:
                self.send_json(200, [backend.pool_view(g, actor_id=actor) for g in backend.groups])
            elif len(path) == 4 and path[:2] == ["v1", "groups"] and path[2] and path[3] == "admin-view":
                self.send_json(200, backend.group_admin_view(path[2], actor_id=actor))
            elif len(path) == 3 and path[:2] == ["v1", "migrations"]:
                self.send_json(200, backend.migration_view(path[2], actor_id=actor))
            elif len(path) == 4 and path[:2] == ["v1", "members"] and path[3] == "charge-basis":
                period = query.get("period", ["current"])[0]
                self.send_json(200, backend.member_charge_basis(path[2], period, actor_id=actor))
            elif len(path) == 5 and path[:3] == ["v1", "support", "members"] and path[4] == "current":
                self.send_json(200, backend.support_current_view(path[3], agent_id=actor))
            elif len(path) == 3 and path[:2] == ["v1", "periods"]:
                self.send_json(200, backend.period_view(path[2], actor_id=actor))
            elif path == ["v1", "sample", "report"]:
                self.send_json(200, backend.sample_report())
            else:
                self.send_json(404, {"error": "not_found"})
        except ApiError as exc:
            self.send_json(exc.status, {"error": exc.code, "message": exc.message, "details": exc.details})
        except Exception as exc:  # pragma: no cover - HTTP 兜底
            self.send_json(500, {"error": "internal_error", "message": str(exc)})

    def do_POST(self) -> None:  # noqa: N802
        try:
            url = urlsplit(self.path)
            query = parse_qs(url.query, keep_blank_values=True)
            path = [p for p in url.path.split("/") if p]
            body = self.read_body()
            backend = self.server.backend
            body_actor = body.pop("actor_id", None)
            actor = self.headers.get("X-Actor-Id") or body_actor or \
                query.get("actor_id", ["anonymous"])[0]
            if path == ["v1", "groups"]:
                self.send_json(201, backend.register_group(actor_id=actor, **body))
            elif path == ["v1", "packs"]:
                self.send_json(201, backend.register_pack(actor_id=actor, **body))
            elif path == ["v1", "commitments"]:
                self.send_json(201, backend.add_commitment(actor_id=actor, **body))
            elif path == ["v1", "register", "load"]:
                self.send_json(200, backend.load_register(body, actor_id=actor))
            elif path == ["v1", "migrations"]:
                self.send_json(201, backend.request_migration(actor_id=actor, **body))
            elif len(path) == 4 and path[:2] == ["v1", "migrations"] and path[3] in ("consent", "consents"):
                self.send_json(200, backend.accept_migration(path[2], actor_id=actor, **body))
            elif len(path) == 3 and path[:2] == ["v1", "migrations"] and path[2] == "due":
                self.send_json(200, {"activated": backend.activate_due(body.get("at", 0))})
            elif len(path) == 4 and path[:2] == ["v1", "migrations"] and path[3] == "activate":
                self.send_json(200, backend.activate_migration(path[2], actor_id=actor, **body))
            elif path == ["v1", "usage"]:
                self.send_json(201, backend.report_usage(actor_id=actor, **body))
            elif len(path) == 4 and path[:2] == ["v1", "usage"] and path[3] == "evidence":
                self.send_json(200, backend.attach_usage_evidence(path[2], body.get("evidence", body), actor_id=actor))
            elif len(path) == 4 and path[:2] == ["v1", "periods"] and path[3] == "close":
                self.send_json(200, backend.close_period(path[2], actor_id=actor, **body))
            elif len(path) == 4 and path[:2] == ["v1", "periods"] and path[3] == "sign":
                self.send_json(200, backend.sign_bill(path[2], actor_id=actor, **body))
            elif path == ["v1", "corrections"]:
                self.send_json(201, backend.create_correction(actor_id=actor, **body))
            elif path == ["v1", "authorizations"]:
                self.send_json(201, backend.grant_authorization(actor_id=actor, **body))
            elif path in (["v1", "intake"], ["v1", "facts"], ["v1", "feed-items"]):
                if "facts" in body:
                    item = body
                else:
                    item = {
                        "sequence": body.pop("sequence", None),
                        "at": body.pop("at", None),
                        "source": body.pop("source", "api"),
                        "facts": body,
                    }
                self.send_json(200, backend.ingest_feed_item(item))
            elif path == ["v1", "sync", "partner"]:
                base_url = body.get("base_url", "http://127.0.0.1:8765")
                self.send_json(200, backend.sync_partner_feed(base_url))
            else:
                self.send_json(404, {"error": "not_found"})
        except ApiError as exc:
            self.send_json(exc.status, {"error": exc.code, "message": exc.message, "details": exc.details})
        except TypeError as exc:
            self.send_json(400, {"error": "invalid_request", "message": str(exc)})
        except Exception as exc:  # pragma: no cover - HTTP 兜底
            self.send_json(500, {"error": "internal_error", "message": str(exc)})

    def log_message(self, fmt: str, *args: Any) -> None:
        return


FamilyPlanBackend = FamilyBackend
ProvisioningService = FamilyBackend
UsageBillingService = FamilyBackend
CustomerService = FamilyBackend


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8800, type=int)
    parser.add_argument("--sample", action="store_true", default=True,
                        help="装载并处理 samples 合成案例（默认）")
    parser.add_argument("--empty", action="store_true", help="启动空内存后端，不装载样例")
    parser.add_argument("--sample-report", action="store_true", help="输出样例账务解释 JSON 后退出")
    parser.add_argument("--sync-partner", default=None, help="启动时同步指定 partner_feed 地址")
    args = parser.parse_args()

    if args.sample_report:
        print(json.dumps(build_sample_backend().sample_report(), ensure_ascii=False, indent=2))
        return

    backend = FamilyBackend() if args.empty else build_sample_backend()
    if args.sync_partner:
        backend.sync_partner_feed(args.sync_partner)
    server = FamilyBackendServer(args.port, backend, args.host)
    print(f"家庭流量后端 http://{args.host}:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
