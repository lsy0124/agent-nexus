from pathlib import Path
import hashlib
import json
import re
import subprocess


def command(root: Path, args: list[str]) -> str:
    process = subprocess.run(["git", '-c', 'core.hooksPath=NUL', '-c', 'core.fsmonitor=false', "-C", str(root), *args], capture_output=True,
                             encoding="utf-8", errors="replace", timeout=30,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if process.returncode:
        raise ValueError(process.stderr.strip() or "Git command failed")
    return process.stdout.strip()


def repository_state(root: Path) -> dict:
    try:
        top = Path(command(root, ["rev-parse", "--show-toplevel"])).resolve()
        if top != root.resolve():
            return {"ready": False, "detail": "需要当前项目独立的 Git 仓库；不会操作父目录仓库"}
        head = command(root, ["rev-parse", "--verify", "HEAD"])
        branch = command(root, ["branch", "--show-current"])
        return {"ready": True, "head": head[:12], "branch": branch, "detail": "任务分支限定在 work/worktrees"}
    except (ValueError, OSError, subprocess.TimeoutExpired):
        return {"ready": False, "detail": "当前项目尚无 Git 基线提交"}


def create(root: Path, task_id: str) -> str:
    if not re.fullmatch(r"[a-f0-9]{12}", task_id):
        raise ValueError("无效任务 ID")
    if not repository_state(root)["ready"]:
        raise ValueError("需要当前项目的 Git 仓库及基线提交")
    parent = root / "work" / "worktrees"
    resolved = parent.resolve()
    # resolve() also follows Windows junctions; disallow redirects outside this workspace.
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("Worktree 路径超出项目工作区")
    parent.mkdir(parents=True, exist_ok=True)
    target = resolved / task_id
    if target.exists():
        raise ValueError("目标 Worktree 已存在；请检查已有记录")
    command(root, ["worktree", "add", "-b", f"nexus/task-{task_id}", str(target), "HEAD"])
    bases = root / 'work' / 'worktree-bases'
    if not bases.resolve().is_relative_to(root.resolve()):
        raise ValueError('Worktree 基线目录越界')
    bases.mkdir(exist_ok=True)
    base = command(target, ['rev-parse', 'HEAD'])
    with (bases / (task_id + '.json')).open('x', encoding='utf-8') as stream:
        json.dump({'task_id': task_id, 'base': base}, stream)
    return str(target)


def snapshot(root: Path, task_id: str, artifacts: list[dict]) -> str:
    """Commit only validated generated artifacts in their task branch; never merge."""
    from .file_changes import checked_path
    target = validate(root, task_id, str(root / 'work' / 'worktrees' / task_id))
    if command(target, ['diff', '--cached', '--name-only']):
        raise ValueError('任务索引已有其他暂存文件，拒绝混入自动审核快照')
    names = []
    for artifact in artifacts:
        path = checked_path(target, artifact['path'])
        if hashlib.sha256(path.read_bytes()).hexdigest() != artifact['sha256']:
            raise ValueError('任务文件在生成后发生变化；拒绝创建不一致的审核快照')
        names.append(artifact['path'])
    if not names:
        raise ValueError('没有可供审核的文件')
    if command(target, ['branch', '--show-current']) != f'nexus/task-{task_id}':
        raise ValueError('任务分支与生成的分支名不符')
    command(target, ['add', '--', *names])
    if command(target, ['diff', '--cached', '--name-only']):
        command(target, ['-c', 'core.hooksPath=NUL', '-c', 'commit.gpgsign=false',
                         '-c', 'user.name=Agent Nexus', '-c', 'user.email=nexus@localhost',
                         'commit', '--quiet', '--no-gpg-sign', '--only', '-m',
                         f'Nexus task {task_id}: generated files pending human review', '--', *names])
    return command(target, ['rev-parse', 'HEAD'])


def validate(root: Path, task_id: str, stored: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{12}", task_id):
        raise ValueError("无效任务 ID")
    expected = root.resolve() / "work" / "worktrees" / task_id
    path = Path(stored)
    if path != expected or path.resolve() != expected or not path.is_dir():
        raise ValueError("任务目录不在生成的 Worktree 白名单中")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("任务目录逃逸项目工作区")
    top = Path(command(path, ["rev-parse", "--show-toplevel"])).resolve()
    common = Path(command(path, ["rev-parse", "--path-format=absolute", "--git-common-dir"])).resolve()
    if top != path or common != (root / ".git").resolve():
        raise ValueError("任务目录不属于当前项目 Git Worktree")
    return path
