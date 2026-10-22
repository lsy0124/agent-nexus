"""Offline human handoff: no WorkBuddy login, external request, or test execution.

An import is a user's declaration, not authenticated WorkBuddy execution evidence.
Only an explicit human review may apply bounded text changes in a generated Worktree.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import subprocess
from typing import Literal
import uuid

from fastapi import HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from . import file_changes, worktrees
from .bridge_protocol import redact
from .storage import now

ACTIVE = ("prepared", "confirmed", "awaiting_review")
SNAPSHOT_FIELDS = ("id", "title", "description", "agent_id", "kind", "priority", "requires_skills", "input_files", "dependencies", "worktree")


class TextChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=220)
    content: str = Field(max_length=256_000)


class TestReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=160)
    status: Literal["passed", "failed", "not_run", "unknown"]
    command: str = Field(default="", max_length=1000)
    output: str = Field(default="", max_length=32000)


class Submission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    handoff_id: str = Field(pattern=r"^[a-f0-9]{12}$")
    task_id: str = Field(pattern=r"^[a-f0-9]{12}$")
    packet_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    summary: str = Field(min_length=1, max_length=16000)
    files: list[TextChange] = Field(default_factory=list, max_length=20)
    test_reports: list[TestReport] = Field(default_factory=list, max_length=20)


class ImportInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    submission: Submission
    sender: str = Field(min_length=1, max_length=100)


class ReviewInput(BaseModel):
    decision: Literal["approve", "reject"]
    reviewer: str = Field(min_length=1, max_length=100)
    note: str = Field(min_length=1, max_length=2000)
    approve_files: bool = False


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ManualHandoffs:
    def __init__(self, root: Path, store, mutation: asyncio.Lock):
        self.root, self.store, self.mutation = root, store, mutation

    def task(self, task_id):
        value = next((t for t in self.store.tasks() if t["id"] == task_id), None)
        if value is None:
            raise HTTPException(404, "任务不存在")
        return value

    def record(self, handoff_id):
        value = self.store.handoff(handoff_id)
        if value is None:
            raise HTTPException(404, "交接记录不存在")
        return value

    def check_task(self, task):
        if task["agent_id"] != "workbuddy" or task["kind"] != "agent":
            raise HTTPException(409, "人工交接只适用于明确选择 WorkBuddy 的任务")
        if task["status"] in ("running", "done"):
            raise HTTPException(409, "运行中或已完成任务不能交接")
        tasks = {t["id"]: t for t in self.store.tasks()}
        if any(tasks[d]["status"] != "done" for d in task["dependencies"]):
            raise HTTPException(409, "前置依赖尚未完成，不能交接或审核")

    def live_record(self, handoff_id, phases):
        record = self.record(handoff_id)
        if record["phase"] not in phases:
            raise HTTPException(409, "当前交接阶段不允许此操作")
        task = self.task(record["task_id"])
        self.check_task(task)
        if (task.get("latest_handoff") or {}).get("id") != handoff_id:
            raise HTTPException(409, "交接记录已被新一轮替代")
        snapshot = {key: task[key] for key in SNAPSHOT_FIELDS}
        if snapshot != record["task_snapshot"]["task"]:
            raise HTTPException(409, "任务已变更，请重新准备交接")
        try:
            target = worktrees.validate(self.root, task["id"], task["worktree"])
            _, hashes = file_changes.context_files(target, task["input_files"])
            if hashes != record["input_hashes"]:
                raise ValueError("交接输入文件已变化，请重新准备")
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            raise HTTPException(409, redact(str(exc))) from None
        return record, task, target

    @staticmethod
    def packet(record_id, snapshot, fingerprint):
        task = snapshot["task"]
        template = {"handoff_id": record_id, "task_id": task["id"], "packet_sha256": fingerprint,
                    "summary": "填写实际完成内容及限制；不要宣称没有执行过的操作",
                    "files": [{"path": "hello.txt", "content": "填写文件的完整 UTF-8 内容；无需变更时 files=[]"}],
                    "test_reports": [{"name": "测试名称", "status": "not_run", "command": "", "output": "未执行，请保留 not_run"}]}
        return ("Agent Nexus → WorkBuddy 人工任务交接\n"
                "仅由用户确认后手工复制粘贴；Nexus 没有自动发送或连接 WorkBuddy。\n"
                f"交接ID：{record_id}\n任务ID：{task['id']}\n任务快照指纹（canonical JSON SHA-256）：{fingerprint}\n\n"
                f"任务目标：{task['title']}\n目标、约束与验收标准：\n{task['description'] or '请按任务目标完成，并明确说明验证方式与未完成内容。'}\n\n"
                "权限边界：只处理下列显式上下文，提交新增/修改文本文件的完整内容；不要直接写入 Nexus 审阅 Worktree。"
                "禁止获取账号凭证、修改系统、安装依赖、删除文件、远程推送或发送外部消息。需要额外权限时停止并在 summary 说明。"
                "只能报告实际执行过的测试；未执行填 not_run，失败填 failed，不能编造通过。测试命令仅作记录，不授权 Nexus 执行。\n"
                "所需能力交给 WorkBuddy 核对，未满足请如实说明；此交接不会在 Nexus 安装或激活任何 Skills。\n"
                "Nexus 审阅目标（不要直接改写）：" + task["worktree"] + "\n\n"
                "冻结任务与明确选择的输入文件：\n" + json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n\n"
                "交付格式：只返回下方 JSON 对象；绑定字段原样保留，移除示例文件并填写真实变更。最多20个文本文件。"
                "文件和报告将由用户导入 Nexus，待人工审核后校验落盘。此 JSON 不是自动完成或身份认证证明。\n"
                + json.dumps(template, ensure_ascii=False, indent=2))

    async def prepare(self, task_id):
        async with self.mutation:
            task = self.task(task_id)
            self.check_task(task)
            previous = task.get("latest_handoff")
            if previous and previous["phase"] in ACTIVE:
                self.live_record(previous["id"], ACTIVE)
                return {"handoff": previous}
            try:
                if not task["worktree"]:
                    task["worktree"] = await asyncio.to_thread(worktrees.create, self.root, task_id)
                    # Preserve the owned Worktree even if a selected input is missing.
                    with self.store.connect() as db:
                        db.execute("UPDATE tasks SET worktree=?,updated_at=? WHERE id=?", (task["worktree"], now(), task_id))
                        self.store.event(db, "worktree.created", {"task_id": task_id, "path": task["worktree"]})
                target = worktrees.validate(self.root, task_id, task["worktree"])
                inputs, hashes = file_changes.context_files(target, task["input_files"])
            except (ValueError, OSError, subprocess.SubprocessError) as exc:
                raise HTTPException(409, redact(str(exc))) from None
            handoff_id, stamp = uuid.uuid4().hex[:12], now()
            snapshot = {"task": {key: task[key] for key in SNAPSHOT_FIELDS}, "input_files": inputs, "protocol": "manual-handoff/1.0", "handoff_id": handoff_id}
            fingerprint = file_changes.sha(encode(snapshot).encode("utf-8"))
            packet = self.packet(handoff_id, snapshot, fingerprint)
            with self.store.connect() as db:
                db.execute("INSERT INTO handoffs(id,task_id,phase,task_snapshot,input_hashes,packet,packet_sha256,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                           (handoff_id, task_id, "prepared", encode(snapshot), encode(hashes), packet, fingerprint, stamp, stamp))
                db.execute("UPDATE tasks SET status='blocked',result=?,updated_at=? WHERE id=?", ("人工交接待确认；尚未发送到 WorkBuddy", stamp, task_id))
                self.store.log(db, task_id, "已生成 WorkBuddy 人工交接任务快照；等待用户确认，未自动复制或发送", source="manual-handoff")
                self.store.event(db, "handoff.prepared", {"task_id": task_id, "handoff_id": handoff_id})
            return {"handoff": self.record(handoff_id)}

    async def confirm(self, handoff_id):
        async with self.mutation:
            record, task, _ = self.live_record(handoff_id, ("prepared", "confirmed"))
            if record["phase"] == "prepared":
                with self.store.connect() as db:
                    db.execute("UPDATE handoffs SET phase='confirmed',updated_at=? WHERE id=?", (now(), handoff_id))
                    db.execute("UPDATE tasks SET result=?,updated_at=? WHERE id=?", ("人工交接已确认；请由用户复制任务至 WorkBuddy，等待导入结果", now(), task["id"]))
                    self.store.log(db, task["id"], "用户已确认交接任务包；复制/粘贴由用户完成，Nexus 未发送外部消息", source="manual-handoff")
                    self.store.event(db, "handoff.confirmed", {"task_id": task["id"], "handoff_id": handoff_id})
            value = self.record(handoff_id)
            return {"handoff": value, "packet": value["packet"]}

    async def import_result(self, handoff_id, data: ImportInput):
        async with self.mutation:
            record, task, target = self.live_record(handoff_id, ("confirmed", "awaiting_review"))
            submission = data.submission.model_dump()
            if not data.sender.strip() or not submission["summary"].strip():
                raise HTTPException(422, "导入人和结果说明不能为空白")
            if (submission["handoff_id"] != handoff_id or submission["task_id"] != task["id"]
                    or submission["packet_sha256"] != record["packet_sha256"]):
                raise HTTPException(409, "导入结果不属于当前任务快照，拒绝串单")
            try:
                if len(encode(submission).encode("utf-8")) > 1_200_000:
                    raise ValueError("结果包超过1.2MB限制")
                file_changes.validate_files(target, submission["files"], record["input_hashes"])
            except (ValueError, OSError, RecursionError) as exc:
                raise HTTPException(422, redact(str(exc))) from None
            for report in submission["test_reports"]:
                report["output"] = redact(report["output"])
            with self.store.connect() as db:
                db.execute("UPDATE handoffs SET phase='awaiting_review',submission=?,sender=?,review=NULL,updated_at=? WHERE id=?",
                           (encode(submission), data.sender.strip(), now(), handoff_id))
                db.execute("UPDATE tasks SET status='blocked',result=?,updated_at=? WHERE id=?", ("人工导入结果待审核；文件尚未写入，测试为外部申报、未经 Nexus 执行验证", now(), task["id"]))
                self.store.log(db, task["id"], "已接收人工结果和文本文件/测试报告申报；未确认 WorkBuddy 身份或执行，未落盘变更", source="manual-handoff")
                self.store.event(db, "handoff.imported", {"task_id": task["id"], "handoff_id": handoff_id})
            return {"handoff": self.record(handoff_id)}

    async def review(self, handoff_id, data: ReviewInput):
        async with self.mutation:
            record, task, target = self.live_record(handoff_id, ("awaiting_review",))
            if not data.reviewer.strip() or not data.note.strip():
                raise HTTPException(422, "审核人和说明不能为空白")
            proposals = record["submission"]["files"]
            if data.decision == "approve" and proposals and not data.approve_files:
                raise HTTPException(403, "需要明确批准 Nexus 校验并写入所列文件")
            artifacts = []
            if data.decision == "approve":
                try:
                    artifacts = file_changes.apply_files(target, proposals, record["input_hashes"])
                    for artifact in artifacts:
                        artifact["generated_by"] = "human-import:workbuddy-unverified"
                except (ValueError, OSError, subprocess.SubprocessError) as exc:
                    raise HTTPException(409, "审核落盘失败，结果仍待审核：" + redact(str(exc))) from None
            review = {"decision": data.decision, "reviewer": data.reviewer.strip(), "note": data.note.strip(), "approve_files": data.approve_files, "time": now()}
            approved = data.decision == "approve"
            result = ("人工审核已接受外部交接结果；Nexus 已校验并写入 " + str(len(artifacts)) + " 个文件。测试报告仍为外部申报，未经 Nexus 执行验证。\n" + record["summary"] if approved
                      else "人工审核已拒绝；未写入任何文件。" + data.note.strip())
            with self.store.connect() as db:
                db.execute("UPDATE handoffs SET phase=?,review=?,artifacts=?,updated_at=? WHERE id=?", ("approved" if approved else "rejected", encode(review), encode(artifacts), now(), handoff_id))
                db.execute("UPDATE tasks SET status=?,result=?,updated_at=? WHERE id=?", ("done" if approved else "blocked", result, now(), task["id"]))
                self.store.log(db, task["id"], result, source="manual-review")
                self.store.event(db, "handoff.reviewed", {"task_id": task["id"], "handoff_id": handoff_id, "decision": data.decision})
            return {"handoff": self.record(handoff_id)}


def install_routes(app, service: ManualHandoffs):
    @app.post("/api/tasks/{task_id}/handoff/prepare")
    async def prepare(task_id: str):
        return await service.prepare(task_id)

    @app.get("/api/handoffs/{handoff_id}")
    async def get_record(handoff_id: str):
        return {"handoff": service.record(handoff_id)}

    @app.post("/api/handoffs/{handoff_id}/confirm")
    async def confirm(handoff_id: str):
        return await service.confirm(handoff_id)

    @app.get("/api/handoffs/{handoff_id}/packet")
    async def download(handoff_id: str):
        record = service.record(handoff_id)
        if record["phase"] in ("prepared", "cancelled"):
            raise HTTPException(409, "请先确认交接；已失效的任务包不能下载")
        return PlainTextResponse(record["packet"], headers={"Content-Disposition": f'attachment; filename="workbuddy-{handoff_id}.txt"'})

    @app.post("/api/handoffs/{handoff_id}/import")
    async def import_result(handoff_id: str, data: ImportInput):
        return await service.import_result(handoff_id, data)

    @app.post("/api/handoffs/{handoff_id}/review")
    async def review(handoff_id: str, data: ReviewInput):
        return await service.review(handoff_id, data)
