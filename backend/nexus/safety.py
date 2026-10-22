"""Metadata-only skill review. No third-party content is ever executed."""
import hashlib
import re
from urllib.parse import urlencode, urlparse

SAFE_PERMISSIONS = {"workspace.read", "workspace.write"}
RISK_PATTERNS = {
    "credential_access": r"(?i)(cookies?|tokens?|credentials?|\.ssh|\.aws|\.env|api[_ -]?key)",
    "system_mutation": r"(?i)(HKEY_|reg(?:istry|\.exe)|set-executionpolicy|netsh|shutdown|format\s+[a-z]:)",
    "remote_execution": r"(?i)(invoke-expression|\biex\b|curl.+\||wget.+\||powershell.+-enc|eval\s*\(|exec\s*\()",
    "destructive_operation": r"(?i)(rm\s+-rf|remove-item.+-recurse|rmdir\s+/s|git\s+push|danger-full-access)",
    "prompt_injection": r"(?i)(ignore.{0,30}(instructions|rules)|bypass.{0,30}(approval|sandbox)|忽略.{0,12}(规则|指令)|绕过.{0,12}(审批|沙箱))",
}


def digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def review(content: str, permissions: list[str]) -> dict:
    findings = [key for key, pattern in RISK_PATTERNS.items() if re.search(pattern, content)]
    unknown = sorted(set(permissions) - SAFE_PERMISSIONS)
    if unknown:
        findings.append("unsupported_permissions")
    if not content.strip():
        findings.append("empty_content")
    return {"passed": not findings, "findings": findings, "unsupported_permissions": unknown,
            "method": "static-heuristic-v1", "sha256": digest(content),
            "limitation": "静态规则不能证明安全；试运行仅验证清单，不执行 Skill 代码"}


def search_links(query: str) -> list[dict]:
    return [
        {"title": "GitHub · 查找 SKILL.md", "url": "https://github.com/search?" + urlencode({"q": query + " path:SKILL.md", "type": "code"})},
        {"title": "在线检索 · 官方来源优先", "url": "https://www.google.com/search?" + urlencode({"q": query + " agent skill official"})},
    ]


def valid_source(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username and not parsed.password

