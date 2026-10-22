"""Worktree review: real Git diff, read-only conflict check, human-approved merge.

A task's generated Worktree is code isolation, not an OS sandbox. This module is
the review gate in front of the user's own working tree:

* ``review`` reports the **real** diff between the task branch and the branch it
  was cut from, the physical files it would change, and whether the project has
  moved on since the Worktree was created.
* ``preflight`` answers "would this merge conflict?" using Git's own merge
  machinery in a temporary throwaway worktree, so it never touches the user's
  files, index or branch. If the check cannot be performed it reports
  ``not_checked`` rather than guessing.
* ``approve`` performs the merge only after an explicit human decision, and only
  when a fresh preflight says the merge is clean. The merge is a normal
  ``--no-ff`` commit, so ``git revert`` undoes it and nothing is lost.
* ``reject`` records the decision without touching any file.

Nothing here stages, discards or deletes a user's work, and nothing is ever
pushed to a remote.
"""
from __future__ import annotations

import re
import json
from pathlib import Path
import subprocess
import uuid

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .bridge_protocol import redact
from .storage import now

MAX_DIFF_BYTES = 400_000
MAX_FILES = 500
TASK_ID = re.compile(r"^[a-f0-9]{12}$")
BRANCH = re.compile(r"^nexus/task-[a-f0-9]{12}$")
SAFE_REF = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")


class MergeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reviewer: str = Field(min_length=1, max_length=100)
    note: str = Field(min_length=1, max_length=2000)
    approve_merge: bool = False
    acknowledge_unpushed: bool = False


class RejectInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    reviewer: str = Field(min_length=1, max_length=100)
    note: str = Field(min_length=1, max_length=2000)


def _git(root: Path, args: list[str], *, timeout: int = 60, check: bool = True) -> str:
    process = subprocess.run(["git", '-c', 'core.hooksPath=NUL', '-c', 'core.fsmonitor=false', "-C", str(root), *args], capture_output=True,
                             encoding="utf-8", errors="replace", timeout=timeout,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if check and process.returncode:
        raise ValueError((process.stderr or process.stdout).strip() or "Git 命令失败")
    return process.stdout


def _git_ok(root: Path, args: list[str], *, timeout: int = 60) -> tuple[int, str]:
    process = subprocess.run(["git", '-c', 'core.hooksPath=NUL', '-c', 'core.fsmonitor=false', "-C", str(root), *args], capture_output=True,
                             encoding="utf-8", errors="replace", timeout=timeout,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return process.returncode, (process.stdout or "") + (process.stderr or "")


def _branch_state(root: Path) -> dict:
    head = _git(root, ["rev-parse", "HEAD"]).strip()
    branch = _git(root, ["branch", "--show-current"]).strip()
    dirty = bool(_git(root, ["status", "--porcelain"]).strip())
    return {"head": head, "branch": branch, "working_tree_dirty": dirty}


def list_changed_files(root: Path, worktree: Path, base: str) -> list[dict]:
    """Files that differ between the task branch and its base commit."""
    raw = _git(worktree, ["diff", "--numstat", "--no-renames", f"{base}..HEAD"])
    files = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) != 3:
            continue
        added, removed, path = parts
        if len(files) >= MAX_FILES:
            files.append({"path": "…", "note": "文件数超过报告上限，已截断"})
            break
        files.append({
            "path": path.strip(),
            "added": None if added == "-" else int(added),
            "removed": None if removed == "-" else int(removed),
            "binary": added == "-",
        })
    return files


def diff_text(root: Path, worktree: Path, base: str, limit: int = MAX_DIFF_BYTES) -> tuple[str, bool]:
    raw = _git(worktree, ["diff", "--no-color", "--no-renames", f"{base}..HEAD"], timeout=120)
    encoded = raw.encode("utf-8")
    if len(encoded) <= limit:
        return raw, False
    return encoded[:limit].decode("utf-8", "ignore") + "\n… 差异超出报告上限，已截断", True


def _behind_commits(root: Path, base: str, head: str) -> int:
    """How many commits the project has that the task branch does not have."""
    code, output = _git_ok(root, ["rev-list", "--count", f"{head}..HEAD"])
    if code:
        return 0
    try:
        return int(output.strip() or "0")
    except ValueError:
        return 0


def _changed_since(root: Path, worktree: Path, base: str) -> list[str]:
    """Files the project changed since the task branch was cut, per file list.

    This is a *file-level* warning, not a conflict verdict: it names the overlap
    a reviewer should look at.
    """
    task_files = {item["path"] for item in list_changed_files(root, worktree, base) if item["path"] != "…"}
    if not task_files:
        return []
    branch_files = {line.strip() for line in _git(root, ["diff", "--name-only", f"{base}..HEAD"]).splitlines() if line.strip()}
    return sorted(task_files & branch_files)


def preflight(root: Path, worktree: Path, base: str) -> dict:
    """Would merging the task branch into the current branch conflict?

    Runs Git's own merge in a throwaway detached worktree under
    ``work/review-preflight``. The user's working tree, index and branches are
    untouched, and the temporary worktree is always removed again.
    """
    try:
        current = _branch_state(root)
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        return {"status": "not_checked", "detail": "合并预检不可用：" + redact(str(exc))[:300]}
    target_branch = current["branch"]
    if target_branch in ("", "HEAD"):
        return {"status": "not_checked", "detail": "当前处于游离 HEAD，未执行合并预检"}
    task_branch = f"nexus/task-{worktree.name}"
    if not TASK_ID.fullmatch(worktree.name):
        raise ValueError("无效任务 Worktree")
    parent = root / "work" / "review-preflight"
    if not parent.resolve().is_relative_to(root.resolve()):
        raise ValueError("预检目录超出项目工作区")
    parent.mkdir(parents=True, exist_ok=True)
    probe = parent / uuid.uuid4().hex[:12]
    result = {"status": "not_checked", "detail": "未执行合并预检", "branch": task_branch, "target": target_branch}
    try:
        _git(root, ["worktree", "add", "--detach", str(probe), target_branch])
        code, output = _git_ok(probe, ["merge", "--no-commit", "--no-ff", task_branch], timeout=120)
        conflicts = [line.strip() for line in _git(probe, ["diff", "--name-only", "--diff-filter=U"]).splitlines() if line.strip()]
        if code == 0 and not conflicts:
            result.update(status="clean", detail="合并预检通过：无冲突")
        elif conflicts:
            result.update(status="conflict", conflicts=conflicts,
                          detail="合并预检发现冲突文件：" + "、".join(conflicts[:20]))
        else:
            result.update(status="not_checked", detail="合并预检未能完成：" + redact(output.strip())[:300])
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        result.update(status="not_checked", detail="合并预检不可用：" + redact(str(exc))[:300])
    finally:
        _git_ok(root, ["worktree", "remove", "--force", str(probe)])
        _git_ok(root, ["worktree", "prune"])
    return result


class WorktreeReviews:
    """Review service bound to one project root and its store."""

    def __init__(self, root: Path, store, lock):
        self.root, self.store, self.lock = root, store, lock

    def task(self, task_id):
        value = next((item for item in self.store.tasks() if item["id"] == task_id), None)
        if value is None:
            raise HTTPException(404, "任务不存在")
        return value

    def _worktree(self, task_id: str) -> Path:
        from . import worktrees
        task = self.task(task_id)
        if not task["worktree"]:
            raise HTTPException(409, "该任务还没有隔离 Worktree")
        try:
            return worktrees.validate(self.root, task_id, task["worktree"])
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            raise HTTPException(409, redact(str(exc))) from None

    def _base(self, worktree: Path) -> str:
        """The commit to diff the task branch against.

        A branch Nexus has just created carries no commit of its own yet, so its
        first parent does not exist. Git created it from the current HEAD, which
        is therefore the base, and the review correctly reports zero changes.
        """
        path = self.root / 'work' / 'worktree-bases' / (worktree.name + '.json')
        if path.exists():
            if path.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()):
                raise HTTPException(409, 'Worktree 基线文件越界')
            document = json.loads(path.read_text(encoding='utf-8'))
            base = document.get('base', '')
            if document.get('task_id') == worktree.name and re.fullmatch(r'[a-f0-9]{40,64}', base):
                return base
            raise HTTPException(409, 'Worktree 基线记录损坏')
        # Legacy tasks: branch creation reflog records the exact original base.
        entries = _git(worktree, ['reflog', 'show', '--format=%H %gs', f'nexus/task-{worktree.name}']).splitlines()
        if entries and 'branch: Created from ' in entries[-1]:
            return entries[-1].split()[0]
        raise HTTPException(409, '无法确定任务创建时的真实基线；禁止用 HEAD~1 猜测')

    def status(self, task_id: str) -> dict:
        worktree = self._worktree(task_id)
        base = self._base(worktree)
        branch = f"nexus/task-{task_id}"
        current = _branch_state(self.root)
        files = list_changed_files(self.root, worktree, base)
        task_head = _git(worktree, ["rev-parse", "HEAD"]).strip()
        has_changes = task_head != base
        return {
            "task_id": task_id, "branch": branch, "base": base[:12],
            "task_head": task_head[:12],
            "files": files, "file_count": len([item for item in files if item["path"] != "…"]),
            # A branch with no commit of its own is not "already merged"; it is
            # simply empty, and the two states must read differently.
            "has_changes": has_changes,
            "project": {"branch": current["branch"], "head": current["head"][:12],
                        "working_tree_dirty": current["working_tree_dirty"],
                        "commits_behind": _behind_commits(self.root, base, branch)},
            "overlapping_files": _changed_since(self.root, worktree, base),
            "already_merged": has_changes and self._is_ancestor(worktree, branch, current["branch"] or "HEAD"),
            "merge_identity": self._merge_identity(),
        }

    def _merge_identity(self) -> dict:
        """Whether Git has an identity configured; a merge commit needs one."""
        name = _git_ok(self.root, ["config", "user.name"])[1].strip()
        email = _git_ok(self.root, ["config", "user.email"])[1].strip()
        return {"configured": bool(name and email), "name": name or None, "email": email or None}

    @staticmethod
    def _is_ancestor(worktree: Path, branch: str, target: str) -> bool:
        code, _ = _git_ok(worktree, ["merge-base", "--is-ancestor", branch, target])
        return code == 0

    def diff(self, task_id: str) -> dict:
        worktree = self._worktree(task_id)
        base = self._base(worktree)
        # Read the bound at call time so a deployment can raise or lower the
        # report limit without editing this function's default.
        text, truncated = diff_text(self.root, worktree, base, limit=MAX_DIFF_BYTES)
        return {"task_id": task_id, "base": base[:12], "diff": text, "truncated": truncated,
                "bytes": len(text.encode("utf-8")), "limit": MAX_DIFF_BYTES}

    def preflight(self, task_id: str) -> dict:
        worktree = self._worktree(task_id)
        return {"task_id": task_id, **preflight(self.root, worktree, self._base(worktree))}

    async def approve(self, task_id: str, data: MergeInput) -> dict:
        async with self.lock:
            task = self.task(task_id)
            if task["status"] == "running":
                raise HTTPException(409, "任务正在运行，不能合并未完成的分支")
            if not data.approve_merge:
                raise HTTPException(403, "需要明确批准将任务分支合并到当前分支")
            if not self._worktree(task_id).is_dir():
                raise HTTPException(409, "任务 Worktree 不存在")
            state = self.status(task_id)
            if not state["has_changes"]:
                raise HTTPException(409, "任务分支相对当前分支没有任何提交或文件改动，没有可合并的内容")
            if state["already_merged"]:
                raise HTTPException(409, "该任务分支已合并到当前分支")
            current = _branch_state(self.root)
            if current["working_tree_dirty"] and not data.acknowledge_unpushed:
                raise HTTPException(409, "当前工作区有未提交改动；合并会与它们叠加。请先提交或明确确认")
            identity = self._merge_identity()
            if not identity["configured"]:
                # Checked before the preflight, because Git's own merge machinery
                # also refuses a merge without a committer identity.
                raise HTTPException(409, "Git 未配置 user.name / user.email，无法创建合并提交；请先配置 Git 身份")
            check = preflight(self.root, self._worktree(task_id), self._base(self._worktree(task_id)))
            if check["status"] != "clean":
                with self.store.connect() as db:
                    self.store.log(db, task_id, "合并被拒绝：" + check["detail"], "warning", "git-review")
                    self.store.event(db, "worktree.merge_blocked", {"task_id": task_id, "detail": check["detail"]})
                raise HTTPException(409, "合并预检未通过，未修改任何文件：" + check["detail"])
            branch = state["branch"]
            target_branch = _branch_state(self.root)["branch"]
            try:
                code, output = _git_ok(self.root, ["merge", "--no-ff", "-m",
                                                   f"nexus: merge task {task_id} (reviewed by {data.reviewer.strip()})",
                                                   branch], timeout=120)
            except (OSError, subprocess.SubprocessError) as exc:
                raise HTTPException(409, "Git 合并执行失败：" + redact(str(exc))) from None
            if code:
                _git_ok(self.root, ["merge", "--abort"])
                with self.store.connect() as db:
                    self.store.log(db, task_id, "合并失败，已回滚：" + redact(output.strip())[:300], "error", "git-review")
                raise HTTPException(409, "合并失败，已回滚到合并前状态")
            head = _git(self.root, ["rev-parse", "HEAD"]).strip()
            record = {"decision": "merged", "reviewer": data.reviewer.strip(), "note": data.note.strip(),
                      "branch": branch, "target_branch": target_branch, "merge_commit": head,
                      "base": state["base"],
                      "files": [item["path"] for item in state["files"] if item["path"] != "…"],
                      "preflight": check["status"], "pushed_to_remote": False, "time": now()}
            with self.store.connect() as db:
                self.store.log(db, task_id,
                               f"已人工批准并合并 {branch} → {target_branch}；合并提交 {head[:12]}；"
                               f"文件 {len(record['files'])} 个；未推送远程",
                               source="git-review")
                self.store.event(db, "worktree.merged", {"task_id": task_id, "merge_commit": head,
                                                         "reviewer": record["reviewer"],
                                                         "target_branch": target_branch})
            return {"task_id": task_id, "merged": True, "record": record}

    async def reject(self, task_id: str, data: RejectInput) -> dict:
        async with self.lock:
            self.task(task_id)
            self._worktree(task_id)
            record = {"decision": "rejected", "reviewer": data.reviewer.strip(), "note": data.note.strip(), "time": now()}
            with self.store.connect() as db:
                self.store.log(db, task_id, f"人工审核拒绝合并：{record['note']}；未修改任何文件", source="git-review")
                self.store.event(db, "worktree.rejected", {"task_id": task_id, "reviewer": record["reviewer"]})
            return {"task_id": task_id, "merged": False, "record": record}


def install_routes(app, service: WorktreeReviews):
    @app.get("/api/tasks/{task_id}/review")
    async def review(task_id: str):
        try:
            return {"status": service.status(task_id)}
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            raise HTTPException(409, redact(str(exc))) from None

    @app.get("/api/tasks/{task_id}/review/diff")
    async def review_diff(task_id: str):
        try:
            return service.diff(task_id)
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            raise HTTPException(409, redact(str(exc))) from None

    @app.post("/api/tasks/{task_id}/review/preflight")
    async def review_preflight(task_id: str):
        try:
            return service.preflight(task_id)
        except (ValueError, OSError, subprocess.SubprocessError) as exc:
            raise HTTPException(409, redact(str(exc))) from None

    @app.post("/api/tasks/{task_id}/review/merge")
    async def review_merge(task_id: str, data: MergeInput):
        return await service.approve(task_id, data)

    @app.post("/api/tasks/{task_id}/review/reject")
    async def review_reject(task_id: str, data: RejectInput):
        return await service.reject(task_id, data)
