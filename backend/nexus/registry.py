"""Failure boundaries and routing independent from provider-specific implementation."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from .adapters import PROTOCOL_VERSION


class AdapterRegistry:
    def __init__(self, adapters):
        self.adapters, self.faults = adapters, {}

    def snapshot(self, agent_id):
        adapter = self.adapters[agent_id]
        try:
            data = dict(adapter.probe())
            verified = data.get("verified_at") or data.get('last_verified_at')
            age = None
            fresh = False
            if verified:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(verified)).total_seconds()
                fresh = 0 <= age < 1800
            connected = data.get("status") == "connected" and bool(data.get("ready")) and fresh
            manual = data.get("execution_mode") == "manual" and data.get("manual_available") is True
            data.update(id=agent_id, status="manual_handoff" if manual else "connected" if connected else "disconnected",
                        verified_at=verified if connected else None, protocol_version=PROTOCOL_VERSION)
            if manual:
                data.update(ready=False, capabilities=[], verified_at=None)
            data.setdefault("supported_capabilities", list(getattr(adapter, "supported_capabilities", ()) or getattr(adapter, "capabilities", ())))
            data.setdefault("sources", data.get("official_sources", list(getattr(adapter, "sources", ()))))
            data.setdefault("blockers", list(getattr(adapter, "blockers", ())))
            data.setdefault("operations", ["detect", "connection_test", "submit", "status", "logs", "cancel"] if data.get("ready") else ["detect"])
            if agent_id in self.faults:
                data.update(status="manual_handoff" if manual else "disconnected", ready=False, verified_at=None, capabilities=[])
                data["blockers"] = [*data["blockers"], self.faults[agent_id]]
            # Keep the compatibility status used by dispatch fail-closed. These
            # additional fields explain why a route is unavailable, without
            # claiming an HTTP adapter owns a persistent gateway process.
            fault = agent_id in self.faults
            error = bool(data.get('last_error'))
            reason, label = (
                ('adapter_error', '适配器异常') if fault else
                ('manual_handoff', '人工交接') if manual else
                ('configuration_required', '配置未就绪') if not data.get('ready') else
                ('request_error', '请求异常') if error and not connected else
                ('verified', '已验证连接') if connected else
                ('verification_expired', '验证已过期') if age is not None and age >= 1800 else
                ('clock_skew', '验证时间异常') if age is not None and age < 0 else
                ('request_not_verified', '就绪待验证'))
            data.update(
                last_verified_at=verified,
                verification_state='fresh' if connected else 'expired' if reason == 'verification_expired' else 'unverified',
                status_reason=reason, status_label=label,
                connection_state='ERROR' if fault or (error and not connected) else 'ONLINE' if connected else 'CONNECTING' if data.get('busy') else 'OFFLINE',
                last_error_kind=getattr(adapter, 'last_error_kind', None) if error else None)
            if reason == 'verification_expired':
                data['detail'] = '本进程曾验证成功，但 30 分钟验证窗口已过期；未重新发起模型请求，不据此判断进程退出。'
                data['blockers'] = [b for b in data['blockers'] if not isinstance(b, dict) or b.get('code') != 'request_not_verified']
                data['blockers'].append({'code': reason, 'detail': data['detail'], 'requires_approval': False})
            return data
        except Exception as exc:
            return {"id": agent_id, "name": getattr(adapter, "name", agent_id), "model": None,
                    "transport": "unavailable", "installed": False, "executable_detected": False,
                    "status": "disconnected", "ready": False, "busy": False, "verified_at": None,
                    'connection_state': 'ERROR', 'verification_state': 'unverified',
                    'status_reason': 'adapter_error', 'status_label': '适配器异常',
                    "capabilities": [], "supported_capabilities": [], "sources": [],
                    "operations": [], "protocol_version": PROTOCOL_VERSION,
                    "description": "Adapter 故障已隔离", "detail": "环境探测失败，其余 Agent 不受影响",
                    "blockers": ["probe_error:" + type(exc).__name__]}

    def snapshots(self):
        return [self.snapshot(agent_id) for agent_id in self.adapters]

    async def detect(self, agent_id):
        try:
            await asyncio.wait_for(self.adapters[agent_id].detect(), timeout=30)
            self.faults.pop(agent_id, None)
        except Exception as exc:
            self.faults[agent_id] = "detect_error:" + type(exc).__name__
        return self.snapshot(agent_id)

    async def detect_all(self):
        return await asyncio.gather(*(self.detect(agent_id) for agent_id in self.adapters))

    def select(self, requested, requirements, busy_ids=(), policy=None):
        """Reserve one adapter. Discovery, readiness and policy are separate gates.

        An explicit request is a user decision for one task, so policy cannot
        silently redirect it to another vendor; the request is either honoured or
        refused with a reason. Automatic dispatch applies the policy: disabled
        Agents and unapproved billing are never candidates, and the remaining
        candidates are considered in policy priority order. ``policy=None``
        keeps the pre-policy behaviour for callers that have no policy object.
        """
        if requested != "auto":
            item = self.snapshot(requested)
            if not item.get("ready") or item.get("busy") or item["id"] in busy_ids:
                return None
            # Explicit submission is permission to *attempt* a bounded real task,
            # not a claim that a request already succeeded. This is particularly
            # important after a restart: keys can be restored while verification
            # remains absent. Automatic dispatch below still requires connected.
            supported = item.get('supported_capabilities', [])
            available = set(item.get('capabilities', [])) | set(supported)
            return self.adapters[item["id"]] if set(requirements).issubset(available) else None
        candidates = self.snapshots()
        if policy is not None:
            candidates = [item for item in candidates if policy.may_auto_dispatch(item["id"], item)]
            candidates = sorted(candidates, key=lambda item: (policy.rank(item["id"]), item["id"]))
        for item in candidates:
            if not item.get("ready") or item.get("busy") or item["id"] in busy_ids:
                continue
            if item["status"] != "connected":
                continue
            if set(requirements).issubset(item.get("capabilities", [])):
                return self.adapters[item["id"]]
        return None

    def auto_hold_reasons(self, requirements, policy, busy_ids=()):
        """Explain why automatic dispatch has no candidate, without inventing one."""
        notes = []
        for item in self.snapshots():
            blocked = policy.auto_blockers(item["id"], item) if policy is not None else []
            if blocked:
                codes = list(dict.fromkeys(reason["code"] for reason in blocked))
                notes.append(f"{item['id']}：调度策略停用自动派发（{'、'.join(codes)}）")
            elif item["status"] != "connected" or not item.get("ready"):
                notes.append(f"{item['id']}：尚未通过真实连接验证")
            elif item["id"] in busy_ids or item.get("busy"):
                notes.append(f"{item['id']}：正在执行其他任务，并发上限为 1")
            elif not set(requirements).issubset(item.get("capabilities", [])):
                missing = "、".join(sorted(set(requirements) - set(item.get("capabilities", []))))
                notes.append(f"{item['id']}：缺少能力 {missing}")
        return notes
