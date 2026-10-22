"""Worktree review, conflict detection and human-approved merge, on real Git.

Every test builds a real temporary Git repository and real Worktrees through the
production code path. Nothing is mocked: a passing test here means Git actually
produced that diff, that conflict or that merge commit.
"""
import asyncio
import json
from pathlib import Path
import subprocess
import uuid

import pytest

from nexus import review as review_module
from nexus.review import MergeInput, RejectInput, WorktreeReviews
from nexus.storage import Store


def git(root: Path, *args: str, check=True) -> str:
    process = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                             encoding="utf-8", errors="replace", timeout=60)
    if check and process.returncode:
        raise AssertionError((process.stderr or process.stdout).strip())
    return process.stdout


def repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q")
    git(root, "config", "user.name", "Local Review Fixture")
    git(root, "config", "user.email", "review-fixture@localhost")
    # The project's own .gitignore keeps runtime data out of Git; the fixture
    # mirrors that so "clean working tree" means the same thing here as in Nexus.
    (root / ".gitignore").write_text("work/\n", encoding="utf-8")
    (root / "shared.txt").write_text("base\n", encoding="utf-8")
    (root / "other.txt").write_text("base\n", encoding="utf-8")
    git(root, "add", ".")
    git(root, "commit", "-qm", "fixture baseline")
    return root


def new_task(store: Store, root: Path, task_id: str) -> str:
    """Create a real task row plus a real Worktree through the production path."""
    from nexus import worktrees
    with store.connect() as db:
        db.execute("INSERT INTO tasks(id,title,description,agent_id,kind,status,priority,requires_skills,"
                   "result,worktree,created_at,updated_at,input_files) "
                   "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (task_id, "review fixture", "", "local-check", "diagnostic", "done", "normal", "[]",
                    None, None, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00", "[]"))
    path = worktrees.create(root, task_id)
    with store.connect() as db:
        db.execute("UPDATE tasks SET worktree=? WHERE id=?", (path, task_id))
    return path


def service(root: Path) -> WorktreeReviews:
    return WorktreeReviews(root, Store(root), asyncio.Lock())


def commit_in(worktree: str, name: str, content: str, message: str):
    path = Path(worktree)
    (path / name).write_text(content, encoding="utf-8")
    git(path, "add", "--", name)
    git(path, "commit", "-qm", message)


def test_review_reports_the_real_diff_of_the_task_branch(tmp_path):
    root = repo(tmp_path / "project")
    reviews = service(root)
    worktree = new_task(reviews.store, root, "aaaaaaaaaaaa")
    commit_in(worktree, "hello.txt", "Hello Agent Nexus\n", "add hello")

    status = reviews.status("aaaaaaaaaaaa")
    assert status["branch"] == "nexus/task-aaaaaaaaaaaa"
    assert [item["path"] for item in status["files"]] == ["hello.txt"]
    assert status["files"][0]["added"] == 1 and status["files"][0]["removed"] == 0
    assert status["already_merged"] is False
    assert status["merge_identity"]["configured"] is True

    payload = reviews.diff("aaaaaaaaaaaa")
    assert payload["truncated"] is False
    assert "Hello Agent Nexus" in payload["diff"]
    assert payload["diff"].startswith("diff --git")


def test_review_preserves_creation_base_after_multiple_commits(tmp_path):
    root = repo(tmp_path / 'project')
    commit_in(str(root), 'prior.txt', 'prior\n', 'project second baseline commit')
    reviews = service(root)
    tree = new_task(reviews.store, root, '111111111111')
    assert reviews.status('111111111111')['has_changes'] is False
    commit_in(tree, 'one.txt', 'one\n', 'task first commit')
    commit_in(tree, 'two.txt', 'two\n', 'task second commit')
    state = reviews.status('111111111111')
    assert {item['path'] for item in state['files']} == {'one.txt', 'two.txt'}


def test_preflight_detects_a_real_conflict_without_touching_the_project(tmp_path):
    root = repo(tmp_path / "project")
    reviews = service(root)
    worktree = new_task(reviews.store, root, "bbbbbbbbbbbb")
    commit_in(worktree, "shared.txt", "task version\n", "task edits shared")

    # The project changes the same file after the branch was cut.
    (root / "shared.txt").write_text("project version\n", encoding="utf-8")
    git(root, "commit", "-qam", "project edits shared")

    check = reviews.preflight("bbbbbbbbbbbb")
    assert check["status"] == "conflict"
    assert check["conflicts"] == ["shared.txt"]
    # The user's working tree is untouched and no temporary worktree is left.
    assert (root / "shared.txt").read_text(encoding="utf-8") == "project version\n"
    assert git(root, "status", "--porcelain").strip() == ""
    assert not list((root / "work" / "review-preflight").glob("*"))
    status = reviews.status("bbbbbbbbbbbb")
    assert status["overlapping_files"] == ["shared.txt"]
    assert status["project"]["commits_behind"] == 1


def test_clean_preflight_then_human_approved_merge_creates_a_real_merge_commit(tmp_path):
    root = repo(tmp_path / "project")
    reviews = service(root)
    worktree = new_task(reviews.store, root, "cccccccccccc")
    commit_in(worktree, "hello.txt", "Hello Agent Nexus\n", "add hello")

    assert reviews.preflight("cccccccccccc")["status"] == "clean"

    async def approve():
        return await reviews.approve("cccccccccccc", MergeInput(reviewer="Local Review Fixture",
                                                                note="reviewed the diff", approve_merge=True))
    result = asyncio.run(approve())
    assert result["merged"] is True
    record = result["record"]
    assert record["files"] == ["hello.txt"] and record["preflight"] == "clean"
    assert record["target_branch"] == review_module._branch_state(root)["branch"]
    assert (root / "hello.txt").read_text(encoding="utf-8") == "Hello Agent Nexus\n"
    # A real merge commit has two parents and names the reviewer.
    parents = git(root, "rev-list", "--parents", "-n", "1", "HEAD").split()
    assert len(parents) == 3
    assert "reviewed by Local Review Fixture" in git(root, "log", "-1", "--format=%B")
    assert record["pushed_to_remote"] is False
    assert git(root, "remote").strip() == ""
    with pytest.raises(Exception):
        asyncio.run(approve())


def test_merge_requires_an_explicit_decision_and_a_clean_preflight(tmp_path):
    root = repo(tmp_path / "project")
    reviews = service(root)
    worktree = new_task(reviews.store, root, "dddddddddddd")
    commit_in(worktree, "shared.txt", "task version\n", "task edits shared")
    (root / "shared.txt").write_text("project version\n", encoding="utf-8")
    git(root, "commit", "-qam", "project edits shared")

    async def attempt(payload):
        return await reviews.approve("dddddddddddd", payload)
    with pytest.raises(Exception) as refused:
        asyncio.run(attempt(MergeInput(reviewer="r", note="n", approve_merge=False)))
    assert "明确批准" in str(refused.value)
    with pytest.raises(Exception) as conflicted:
        asyncio.run(attempt(MergeInput(reviewer="r", note="n", approve_merge=True)))
    assert "预检未通过" in str(conflicted.value)
    # A refused merge changes nothing at all.
    assert (root / "shared.txt").read_text(encoding="utf-8") == "project version\n"
    assert git(root, "status", "--porcelain").strip() == ""
    assert not (root / "hello.txt").exists()


def test_merge_refuses_a_dirty_working_tree_until_explicitly_acknowledged(tmp_path):
    root = repo(tmp_path / "project")
    reviews = service(root)
    worktree = new_task(reviews.store, root, "eeeeeeeeeeee")
    commit_in(worktree, "hello.txt", "hello\n", "add hello")
    (root / "other.txt").write_text("uncommitted local edit\n", encoding="utf-8")

    with pytest.raises(Exception) as refused:
        asyncio.run(reviews.approve("eeeeeeeeeeee", MergeInput(reviewer="r", note="n", approve_merge=True)))
    assert "未提交改动" in str(refused.value)
    assert not (root / "hello.txt").exists()

    async def acknowledged():
        return await reviews.approve("eeeeeeeeeeee", MergeInput(reviewer="r", note="n", approve_merge=True,
                                                               acknowledge_unpushed=True))
    assert asyncio.run(acknowledged())["merged"] is True
    # The user's uncommitted work survived the merge.
    assert (root / "other.txt").read_text(encoding="utf-8") == "uncommitted local edit\n"


def test_reject_records_a_decision_and_writes_nothing(tmp_path):
    root = repo(tmp_path / "project")
    reviews = service(root)
    worktree = new_task(reviews.store, root, "ffffffffffff")
    commit_in(worktree, "hello.txt", "hello\n", "add hello")

    result = asyncio.run(reviews.reject("ffffffffffff", RejectInput(reviewer="Local Review Fixture", note="not wanted")))
    assert result["merged"] is False and result["record"]["decision"] == "rejected"
    assert not (root / "hello.txt").exists()
    assert git(root, "status", "--porcelain").strip() == ""
    logs = [item for item in reviews.store.logs() if item["task_id"] == "ffffffffffff"]
    assert any("拒绝合并" in item["message"] for item in logs)


def test_review_refuses_a_task_without_a_worktree_and_a_foreign_directory(tmp_path):
    root = repo(tmp_path / "project")
    reviews = service(root)
    with reviews.store.connect() as db:
        db.execute("INSERT INTO tasks(id,title,description,agent_id,kind,status,priority,requires_skills,"
                   "result,worktree,created_at,updated_at,input_files) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   ("111111111111", "no worktree", "", "local-check", "diagnostic", "backlog", "normal", "[]",
                    None, None, "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00", "[]"))
    with pytest.raises(Exception) as missing:
        reviews.status("111111111111")
    assert "还没有隔离 Worktree" in str(missing.value)

    new_task(reviews.store, root, "222222222222")
    outside = tmp_path / "outside"
    outside.mkdir()
    with reviews.store.connect() as db:
        db.execute("UPDATE tasks SET worktree=? WHERE id=?", (str(outside), "222222222222"))
    with pytest.raises(Exception) as foreign:
        reviews.status("222222222222")
    assert "白名单" in str(foreign.value)


def test_already_merged_branch_is_reported_and_cannot_be_merged_twice(tmp_path):
    root = repo(tmp_path / "project")
    reviews = service(root)
    worktree = new_task(reviews.store, root, "333333333333")
    commit_in(worktree, "hello.txt", "hello\n", "add hello")
    asyncio.run(reviews.approve("333333333333", MergeInput(reviewer="r", note="n", approve_merge=True)))
    assert reviews.status("333333333333")["already_merged"] is True
    with pytest.raises(Exception) as twice:
        asyncio.run(reviews.approve("333333333333", MergeInput(reviewer="r", note="n", approve_merge=True)))
    assert "已合并" in str(twice.value)


def test_unconfigured_git_identity_blocks_the_merge_instead_of_failing_opaquely(tmp_path, monkeypatch):
    root = repo(tmp_path / "project")
    reviews = service(root)
    worktree = new_task(reviews.store, root, "444444444444")
    commit_in(worktree, "hello.txt", "hello\n", "add hello")
    # Isolate from any machine-wide Git identity as well as the repository one.
    empty = tmp_path / "empty-gitconfig"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(empty))
    git(root, "config", "--unset", "user.name", check=False)
    git(root, "config", "--unset", "user.email", check=False)
    resolved = git(root, "config", "user.name", check=False).strip()
    if resolved:
        pytest.skip("this machine forces a user.name this fixture cannot clear")
    assert reviews.status("444444444444")["merge_identity"]["configured"] is False
    with pytest.raises(Exception) as blocked:
        # Unsetting the local identity dirties .git/config, so the dirty-tree
        # guard is acknowledged to reach the identity check under test.
        asyncio.run(reviews.approve("444444444444", MergeInput(reviewer="r", note="n", approve_merge=True,
                                                              acknowledge_unpushed=True)))
    assert "Git 身份" in str(blocked.value)
    assert not (root / "hello.txt").exists()


def test_large_diff_is_truncated_with_an_explicit_marker(tmp_path, monkeypatch):
    root = repo(tmp_path / "project")
    reviews = service(root)
    worktree = new_task(reviews.store, root, "555555555555")
    commit_in(worktree, "big.txt", ("line\n" * 4000), "add big file")
    monkeypatch.setattr(review_module, "MAX_DIFF_BYTES", 500)
    payload = reviews.diff("555555555555")
    assert payload["truncated"] is True and "已截断" in payload["diff"]


def test_a_branch_with_no_changes_reports_zero_and_refuses_to_merge(tmp_path):
    root = repo(tmp_path / "project")
    reviews = service(root)
    new_task(reviews.store, root, "777777777777")
    status = reviews.status("777777777777")
    # A freshly created branch has no commit of its own; the review says so
    # instead of inventing a diff, an error or a false "already merged" claim.
    assert status["has_changes"] is False and status["file_count"] == 0
    assert status["task_head"] == status["project"]["head"]
    assert reviews.diff("777777777777")["diff"].strip() == ""
    assert reviews.preflight("777777777777")["status"] == "clean"
    with pytest.raises(Exception) as empty:
        asyncio.run(reviews.approve("777777777777", MergeInput(reviewer="r", note="n", approve_merge=True)))
    assert "没有可合并的内容" in str(empty.value)
    assert git(root, "rev-list", "--count", "HEAD").strip() == "1"


def test_preflight_is_not_claimed_when_the_check_cannot_run(tmp_path, monkeypatch):
    root = repo(tmp_path / "project")
    reviews = service(root)
    worktree = new_task(reviews.store, root, "666666666666")
    commit_in(worktree, "hello.txt", "hello\n", "add hello")

    def broken(*args, **kwargs):
        raise OSError("LOCAL FIXTURE GIT UNAVAILABLE")
    monkeypatch.setattr(review_module, "_git", broken)
    check = reviews.preflight("666666666666")
    assert check["status"] == "not_checked"
    assert "不可用" in check["detail"] or "未执行" in check["detail"]
