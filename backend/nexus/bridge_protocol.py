"""Provider-independent structured-file contract and safe error formatting."""
import json
import re

SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {
        "summary": {"type": "string"},
        "files": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                  "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                  "required": ["path", "content"]}},
        "approval_requests": {"type": "array", "items": {"type": "string"}},
    }, "required": ["summary", "files", "approval_requests"],
}


class BridgeError(RuntimeError):
    def __init__(self, kind: str, message: str, exit_code=None):
        self.kind, self.exit_code = kind, exit_code
        super().__init__(message)


def redact(text: str) -> str:
    text = re.sub(r"(?i)Bearer\s+[^\s\"']+|\bsk-[A-Za-z0-9_-]+", "[REDACTED]", text)
    return re.sub(r'(?i)((?:access_token|refresh_token|api_key|authorization)[\"\s:=]+)[^\s,\"}]+', r'\1[REDACTED]', text)


def error_kind(message: str) -> str:
    lower = message.lower()
    if any(s in lower for s in ("401", "not logged", "unauthorized", "authentication", "login")):
        return "authentication"
    if any(s in lower for s in ("429", "rate limit", "quota")):
        return "rate_limit"
    if any(s in lower for s in ("503", "502", "service unavailable")):
        return "provider_unavailable"
    if any(s in lower for s in ("sandbox", "permission", "access is denied")):
        return "permission"
    return "execution"


def toml(value) -> str:
    if isinstance(value, dict):
        return "{" + ",".join(json.dumps(k) + "=" + toml(v) for k, v in value.items()) + "}"
    return json.dumps(value, ensure_ascii=False)


def validate_plan(value: str) -> dict:
    if len(value.encode("utf-8")) > 1_200_000:
        raise BridgeError("protocol", "Adapter 结果超过大小限制")
    try:
        plan = json.loads(value)
        if not isinstance(plan, dict) or set(plan) != {"summary", "files", "approval_requests"}:
            raise ValueError()
        if not isinstance(plan["summary"], str) or len(plan["summary"]) > 16000:
            raise ValueError()
        if not isinstance(plan["files"], list) or len(plan["files"]) > 20:
            raise ValueError()
        for file in plan["files"]:
            if not isinstance(file, dict) or set(file) != {"path", "content"} or not all(isinstance(v, str) for v in file.values()):
                raise ValueError()
        if not isinstance(plan["approval_requests"], list) or len(plan["approval_requests"]) > 20 or not all(isinstance(s, str) and len(s) <= 2000 for s in plan["approval_requests"]):
            raise ValueError()
        return plan
    except (ValueError, TypeError) as exc:
        raise BridgeError("protocol", "Adapter 未返回有效的结构化文件结果；未落盘") from exc


