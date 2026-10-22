from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

from .adapters import AgentAdapter, AgentEvent, ExecutionContext
from .process_tree import ProcessTree
from .storage import now

from .bridge_protocol import SCHEMA, BridgeError, redact, error_kind, toml, validate_plan

TESTED_CLI_VERSION = "codex-cli 0.162.0-alpha.17.2"
# Flags the bridge requires. A CLI missing any of them is refused rather than
# invoked with an unexpected argument combination.
REQUIRED_FLAGS = ("--json", "--ephemeral", "--output-schema", "--ignore-user-config")


def discover_codex() -> str | None:
    """Locate the Codex CLI without a global install or a PATH change.

    ``shutil.which`` is tried first, so an intentional PATH install wins. When it
    fails, the standard per-user npm locations are checked: an ``@openai/codex``
    package can be present with its bin shim missing, which is exactly the case
    on this machine. Nothing is installed, moved or copied here — only read.

    An executable shim (``.cmd`` / ``.exe``) is returned as-is. A bare ``.js``
    entrypoint cannot be executed directly on Windows, so it is returned with the
    Node interpreter prefixed; :func:`codex_argv` rebuilds the command line.
    """
    found = shutil.which("codex")
    if found:
        return found
    candidates: list[Path] = []
    project = Path(__file__).resolve().parents[2]
    # A project-local install is preferred over a machine-wide one: it is pinned
    # by integrations/codex-cli/package-lock.json, and installing it never
    # modifies the user's global npm state.
    candidates += [
        project / "node_modules" / ".bin" / "codex.cmd",
        project / "work" / "agent-tools" / "codex" / "node_modules" / "@openai" / "codex" / "bin" / "codex.js",
    ]
    appdata = os.environ.get("APPDATA")
    if appdata:
        npm = Path(appdata) / "npm"
        candidates += [
            npm / "codex.cmd",
            npm / "codex.exe",
            npm / "node_modules" / "@openai" / "codex" / "bin" / "codex.js",
        ]
    for candidate in candidates:
        try:
            if candidate.is_file():
                if candidate.suffix.lower() == ".js":
                    return f"node:{candidate}"
                return str(candidate)
        except OSError:
            continue
    return None


def codex_argv(binary: str, args: list[str]) -> list[str]:
    """Build the command line for a discovered binary.

    A ``node:<path>`` marker means the CLI is a JavaScript entrypoint; it is run
    through Node rather than executed directly, which Windows cannot do.
    """
    if binary.startswith("node:"):
        node = shutil.which("node") or "node"
        return [node, binary[5:], *args]
    return [binary, *args]


class CodexAdapter(AgentAdapter):
    id, name, executable = "codex", "Codex", "codex"
    transport = "official-cli-jsonl"
    description = "真实官方 CLI · Codex 生成变更，Nexus 白名单落盘；通用 Shell 关闭"
    capabilities = ("file.generate", "file.edit")
    supported_capabilities = capabilities
    sources = ({"title": "OpenAI Codex non-interactive mode", "url": "https://developers.openai.com/codex/noninteractive"},)

    def __init__(self, root: Path, enabled=True, *, executable: str | None = None):
        self.root, self.enabled = root.resolve(), enabled
        # An explicit path is how a caller tests the version gate without a
        # machine-wide Codex installation, and how a user points Nexus at a
        # non-PATH install. Automatic dispatch of Codex is governed by
        # nexus.policy, not by this adapter.
        self.binary = executable or discover_codex()
        self.explicit_binary = executable
        self.version, self.authenticated, self.compatible = None, False, False
        self.flags_present, self.version_matches = False, False
        self.help_text = ""
        self.verified_at, self.last_error = None, None
        self.active: dict[str, tuple] = {}

    def child_env(self):
        # No API keys, cookies or bearer tokens are collected from the parent environment.
        allowed = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "SYSTEMDRIVE", "COMSPEC",
                   "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA", "APPDATA", "CODEX_HOME"}
        env = {k: v for k, v in os.environ.items() if k.upper() in allowed}
        temp = self.root / "work" / "codex-runtime" / "temp"
        if not temp.resolve().is_relative_to(self.root):
            raise BridgeError("permission", "运行临时目录超出项目范围")
        temp.mkdir(parents=True, exist_ok=True)
        env.update({"TEMP": str(temp), "TMP": str(temp), "TMPDIR": str(temp), "NO_COLOR": "1"})
        return env

    async def detect(self):
        self.binary = self.explicit_binary or discover_codex()
        if not self.enabled or not self.binary:
            return self.probe()

        def inspect(args):
            result = subprocess.run(codex_argv(self.binary, args), capture_output=True,
                                    encoding="utf-8", errors="replace",
                                    timeout=30, cwd=self.root, env=self.child_env(),
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            return result.returncode, result.stdout, result.stderr
        try:
            # Three independent read-only CLI calls, each paying its own Node
            # startup. Run them together instead of ~1.3 s x 3 in sequence.
            # return_exceptions so one failure cannot cancel the others mid-flight.
            version, help_result, login = await asyncio.gather(
                *(asyncio.to_thread(inspect, args)
                  for args in (["--version"], ["exec", "--help"], ["login", "status"])),
                return_exceptions=True)
            for outcome in (version, help_result, login):
                if isinstance(outcome, BaseException):
                    raise outcome
            discovered = version[1].strip()[:150] if version[0] == 0 else None
            if self.version != discovered:
                self.verified_at = None
            self.version = discovered
            # Two independent gates: the exact tested version, and the presence
            # of every flag the bridge passes. Both are stated so the console can
            # explain which one failed instead of a bare "incompatible".
            self.flags_present = all(flag in help_result[1] for flag in REQUIRED_FLAGS)
            self.version_matches = discovered == TESTED_CLI_VERSION
            self.compatible = self.version_matches and self.flags_present
            self.help_text = help_result[1] or ""
            # Only the boolean is retained. Never log login-status stdout or any credential fragment.
            self.authenticated = login[0] == 0 and "logged in" in (login[1] + login[2]).lower()
            if not self.authenticated:
                self.verified_at = None
        except (OSError, subprocess.TimeoutExpired):
            self.compatible, self.authenticated, self.verified_at = False, False, None
            self.flags_present = self.version_matches = False
            self.help_text = ""
        return self.probe()

    @property
    def ready(self):
        return bool(self.enabled and self.binary and self.version and self.compatible and self.authenticated)

    def probe(self):
        connected = self.ready and self.verified_at is not None and (
            datetime.now(timezone.utc) - datetime.fromisoformat(self.verified_at)).total_seconds() < 1800
        version_matches = getattr(self, "version_matches", False)
        flags_present = getattr(self, "flags_present", False)
        # A version mismatch and a missing flag are different problems with
        # different fixes, so they are reported separately rather than as one
        # generic "incompatible". Both still refuse to run the CLI.
        detail = ("真实请求已验证 · 文件生成与受限修改" if connected else
                  "测试实例禁止真实调用" if not self.enabled else
                  f"未发现 CLI（已查找 PATH 与 {os.environ.get('APPDATA', 'APPDATA')}\\npm）"
                  if not self.binary else
                  f"缺少必要参数：{', '.join(f for f in REQUIRED_FLAGS if f not in self.help_text)}"
                  if self.version and not flags_present else
                  f"CLI 版本 {self.version} 未通过 Nexus 审查（要求 {TESTED_CLI_VERSION}）；"
                  "参数已核对但版本闸门未放行，需人工确认后再启用"
                  if self.version and not version_matches else
                  "需要通过官方 CLI 登录；Nexus 不管理凭证" if not self.authenticated else
                  "官方登录可用；尚未验证真实请求")
        return {"id": self.id, "name": self.name, "model": None, "transport": self.transport,
                "installed": bool(self.binary), "executable_detected": bool(self.binary),
                "status": "connected" if connected else "disconnected", "verified_at": self.verified_at if connected else None,
                "last_verified_at": self.verified_at, "capabilities": list(self.capabilities) if self.ready else [],
                "description": self.description, "detail": detail, "cli_version": self.version,
                "authenticated": self.authenticated, "ready": self.ready, "busy": bool(self.active),
                "binary_path": self.binary,
                "version_gate": {"required": TESTED_CLI_VERSION, "installed": self.version,
                                 "matches": version_matches},
                "required_flags_present": flags_present,
                "bridge_mode": "structured-files", "shell_enabled": False, "last_error": self.last_error}

    def command(self, context: ExecutionContext):
        runtime = self.root / "work" / "codex-runtime"
        if not runtime.resolve().is_relative_to(self.root):
            raise BridgeError("permission", "Codex 运行目录超出项目范围")
        runtime.mkdir(parents=True, exist_ok=True)
        schema = runtime / "files.schema.json"
        if not schema.resolve().is_relative_to(self.root):
            raise BridgeError("permission", "结果 Schema 路径超出项目范围")
        schema.write_text(json.dumps(SCHEMA), encoding="utf-8")
        settings = {
            "default_permissions": "nexus-read",
            "permissions.nexus-read.filesystem": {":minimal": "read", str(Path(context.worktree).resolve()).replace("\\", "/"): "read"},
            "permissions.nexus-read.network.enabled": False,
            "features.shell_tool": False, "features.shell_snapshot": False,
            "features.apps": False, "features.plugins": False, "features.hooks": False,
            "features.browser_use": False, "features.computer_use": False,
            "features.skip_host_skill_discovery": True, "features.skill_mcp_dependency_install": False,
            "features.unbounded_connection_retries": False,
            "features.code_mode": False, "features.multi_agent": False,
            "web_search": "disabled", "project_doc_max_bytes": 0, "analytics.enabled": False,
            "history.persistence": "none", "model_reasoning_effort": "low",
            "log_dir": str(runtime / "logs"), "sqlite_home": str(runtime / "state"),
        }
        args = codex_argv(self.binary, [
            "--no-daemon", "--ask-for-approval", "never", "exec", "--ignore-user-config",
            "--ephemeral", "--json", "--color", "never", "--cd", context.worktree,
            "--output-schema", str(schema)])
        for key, value in settings.items():
            args.extend(["-c", key + "=" + toml(value)])
        return args + ["-"]

    async def execute(self, context: ExecutionContext):
        if not self.ready:
            raise BridgeError("not_connected", "Codex Adapter 尚未连接或环境检测未通过")
        if self.active:
            raise BridgeError("busy", "Codex 已有运行任务；当前并发限制为 1")
        process, tree, pumps = None, None, []
        completed, final, usage, failure, thread_id = False, "", {}, "", None
        self.active[context.task_id] = (None, None)
        try:
            async with asyncio.timeout(context.timeout_seconds):
                spawning = asyncio.create_task(asyncio.create_subprocess_exec(
                    *self.command(context), cwd=context.worktree, env=self.child_env(),
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    limit=2_000_000, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    **({"start_new_session": True} if os.name != "nt" else {})))
                try:
                    process = await asyncio.shield(spawning)
                except asyncio.CancelledError:
                    process = await spawning
                    process.kill()
                    await process.wait()
                    raise
                # Assign ownership before delivering any task prompt to the child.
                tree = ProcessTree(process.pid)
                self.active[context.task_id] = (process, tree)
                yield AgentEvent("started", "官方 Codex CLI 子进程已启动；Shell 与外部工具关闭", {"pid": process.pid, "cli_version": self.version})
                payload = {"objective": context.objective, "input_files": list(context.input_files)}
                instruction = (
                    "You are the real Codex execution engine for Agent Nexus. Do not invoke tools, commands, browsers, subagents or skills. "
                    "Return ONLY the JSON object required by the output schema. Generate full UTF-8 text for relative files in files. "
                    "Never access or request credentials, authentication data, cookies, tokens, private client data or paths outside this workspace. "
                    "Only edit existing files supplied in input_files; new files are allowed. File paths must use relative forward slashes. "
                    "Do not claim that you wrote files or ran tests: Nexus will independently validate and apply these contents. "
                    "Put any requested system/global install, dangerous command, executable script, deletion, remote push or external messaging "
                    "in approval_requests and do not carry it out. If the objective cannot be satisfied within those rules, explain it in summary. "
                    "A connection test requires files=[] and summary exactly the requested nonce.\n" + json.dumps(payload, ensure_ascii=False)
                )
                process.stdin.write(instruction.encode("utf-8"))
                await process.stdin.drain()
                process.stdin.close()
                queue = asyncio.Queue(maxsize=100)

                async def pump(reader, source):
                    try:
                        while line := await reader.readline():
                            await queue.put((source, line.decode("utf-8", "replace").strip()))
                    except Exception as exc:
                        await queue.put(("reader_error", str(exc)))
                    finally:
                        await queue.put((source, None))

                pumps = [asyncio.create_task(pump(process.stdout, "stdout")), asyncio.create_task(pump(process.stderr, "stderr"))]
                ended = 0
                while ended < 2:
                    source, line = await queue.get()
                    if line is None:
                        ended += 1
                        continue
                    if not line:
                        continue
                    if source == "reader_error":
                        raise BridgeError("protocol", "Codex 输出流超过限制或读取失败")
                    if source == "stderr":
                        failure = redact(line)[-4000:]
                        yield AgentEvent("log", failure, {"level": "warning", "stream": source})
                        continue
                    try:
                        event = json.loads(line)
                    except ValueError:
                        yield AgentEvent("log", redact(line)[:4000], {"level": "warning", "stream": source})
                        continue
                    kind = event.get("type")
                    if kind == "thread.started":
                        thread_id = event.get("thread_id")
                        yield AgentEvent("thread", "真实 Codex 会话已建立", {"thread_id": thread_id})
                    elif kind == "turn.completed":
                        completed, usage = True, event.get("usage", {})
                        yield AgentEvent("log", "Codex turn.completed；等待退出码与结构化结果校验", {"level": "info"})
                    elif kind in ("error", "turn.failed"):
                        failure = redact(str(event.get("message") or event.get("error") or "Codex turn failed"))[:4000]
                        yield AgentEvent("log", failure, {"level": "error"})
                    elif kind in ("item.completed", "item.started"):
                        item = event.get("item", {})
                        if item.get("type") in ("command_execution", "mcp_tool_call", "web_search", "file_change"):
                            raise BridgeError("policy_violation", "文件桥接不允许 CLI 工具执行；请求已终止且未应用生成结果")
                        if item.get("type") == "agent_message" and kind == "item.completed":
                            final = item.get("text", "")
                            yield AgentEvent("log", f"收到 Codex 真实结果（{len(final.encode('utf-8'))} bytes），尚未写入文件", {"level": "info"})
                        elif item.get("type") == "error":
                            yield AgentEvent("log", redact(item.get("message", "CLI warning"))[:4000], {"level": "warning"})
                        else:
                            yield AgentEvent("log", f"{kind} · {item.get('type', 'unknown')}", {"level": "debug"})
                    else:
                        yield AgentEvent("log", str(kind or "unknown"), {"level": "debug"})
                code = await process.wait()
                if code != 0 or not completed:
                    raise BridgeError(error_kind(failure), failure or f"CLI exit={code}；没有成功的 turn.completed", code)
                plan = validate_plan(final)
                self.verified_at, self.last_error = now(), None
        except TimeoutError as exc:
            self.verified_at, self.last_error = None, "Codex 请求超时，进程树已终止"
            raise BridgeError("timeout", self.last_error) from exc
        except BridgeError as exc:
            self.verified_at, self.last_error = None, redact(str(exc))
            raise
        except OSError as exc:
            self.verified_at, self.last_error = None, redact(str(exc))
            raise BridgeError("environment", self.last_error) from exc
        finally:
            if tree:
                tree.close()
            elif process and process.returncode is None:
                process.kill()
            if process:
                await process.wait()
            for job in pumps:
                job.cancel()
            if pumps:
                await asyncio.gather(*pumps, return_exceptions=True)
            self.active.pop(context.task_id, None)
        yield AgentEvent("completion", plan["summary"], {"plan": plan, "usage": usage, "thread_id": thread_id, "exit_code": code})

    async def cancel(self, task_id):
        process, tree = self.active.get(task_id, (None, None))
        if tree:
            tree.close()
        elif process and process.returncode is None:
            process.kill()
        if process:
            await process.wait()
