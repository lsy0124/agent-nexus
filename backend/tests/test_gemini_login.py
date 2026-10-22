"""Login-launcher safety tests; never open Google login or invoke a model."""
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("nexus_gemini_login_script", ROOT / "scripts" / "gemini_login.py")
login = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(login)


class FixtureAdapter:
    """Only local test data: no credential, account or model exists."""
    compatible = True

    def __init__(self, root):
        self.root = root

    async def detect(self):
        return {"installed": True, "cli_version": "0.64.0"}

    def configure_subscription(self, approved):
        assert approved is True

    def login_spec(self):
        return {"argv": ["LOCAL_FIXTURE_NODE", "LOCAL_FIXTURE_OFFICIAL_CLI"], "cwd": "LOCAL_FIXTURE_NEUTRAL_DIR",
                "environment": {"GEMINI_CLI_HOME": "LOCAL_FIXTURE_DEDICATED_HOME", "NO_BROWSER": "existing-value"}}


def arrange(monkeypatch, tty=True):
    monkeypatch.setattr(login, "GeminiCLIAdapter", FixtureAdapter)
    monkeypatch.setattr(login, "terminal_is_interactive", lambda: tty)


@pytest.mark.parametrize("mode,manual", [("manual", True), ("browser", False)])
def test_login_mode_inherits_terminal_and_never_captures_or_parses_oauth(monkeypatch, capsys, mode, manual):
    arrange(monkeypatch)
    calls = []

    def call(argv, **kwargs):
        calls.append((argv, kwargs))
        assert argv == ["LOCAL_FIXTURE_NODE", "LOCAL_FIXTURE_OFFICIAL_CLI"]
        assert kwargs["cwd"] == "LOCAL_FIXTURE_NEUTRAL_DIR"
        assert not any(key in kwargs for key in ("stdin", "stdout", "stderr", "capture_output", "input", "shell"))
        assert (kwargs["env"].get("NO_BROWSER") == "true") is manual
        assert "--prompt" not in argv and "-p" not in argv
        return 0

    monkeypatch.setattr(login.subprocess, "call", call)
    assert asyncio.run(login.main(mode=mode)) == 0
    assert len(calls) == 1
    assert "ONLY into the official CLI" in capsys.readouterr().out if manual else True


def test_missing_terminal_stops_before_constructing_adapter_or_starting_login(monkeypatch, capsys):
    arrange(monkeypatch, tty=False)

    def forbidden(*args, **kwargs):
        raise AssertionError("Non-TTY login must not construct, launch, or capture anything")

    monkeypatch.setattr(login, "GeminiCLIAdapter", forbidden)
    monkeypatch.setattr(login.subprocess, "call", forbidden)
    assert asyncio.run(login.main()) == 2
    messages = capsys.readouterr()
    assert "-NewWindow" in messages.err and "no authentication was started" in messages.err


def test_public_check_does_not_require_tty_or_activate_subscription(monkeypatch, capsys):
    arrange(monkeypatch, tty=False)

    class PublicOnly(FixtureAdapter):
        def configure_subscription(self, **kwargs):
            raise AssertionError("Metadata check must not authorize login")

        def login_spec(self):
            raise AssertionError("Metadata check must not prepare login")

    monkeypatch.setattr(login, "GeminiCLIAdapter", PublicOnly)
    assert asyncio.run(login.main(check=True)) == 0
    text = capsys.readouterr().out
    assert '"cli_version": "0.64.0"' in text
    assert '"model_request_started": false' in text and '"account_login_verified": false' in text


def test_incompatible_cli_does_not_start_login(monkeypatch, capsys):
    arrange(monkeypatch)

    class WrongVersion(FixtureAdapter):
        compatible = False

    monkeypatch.setattr(login, "GeminiCLIAdapter", WrongVersion)
    assert asyncio.run(login.main()) == 1
    assert "login was not started" in capsys.readouterr().err


def test_process_failure_is_sanitized_and_account_verification_is_not_invented(monkeypatch, capsys):
    arrange(monkeypatch)

    def fail(*args, **kwargs):
        raise OSError("LOCAL_FIXTURE_OAUTH_PRIVATE_VALUE_MUST_NOT_APPEAR")

    monkeypatch.setattr(login.subprocess, "call", fail)
    assert asyncio.run(login.main()) == 1
    messages = capsys.readouterr()
    assert "PRIVATE_VALUE" not in messages.out + messages.err
    assert "no model task was submitted" in messages.err


def test_official_nonzero_exit_is_returned_without_retry_or_fake_success(monkeypatch):
    arrange(monkeypatch)
    calls = []

    def call(*args, **kwargs):
        calls.append(True)
        return 17

    monkeypatch.setattr(login.subprocess, "call", call)
    assert asyncio.run(login.main()) == 17 and len(calls) == 1


@pytest.mark.parametrize("stdin_tty,stdout_tty,expected", [(True, True, True), (False, True, False), (True, False, False), (False, False, False)])
def test_tty_detection_only_checks_handles_and_never_reads_oauth_input(monkeypatch, stdin_tty, stdout_tty, expected):
    class Terminal:
        def __init__(self, tty):
            self.tty = tty

        def isatty(self):
            return self.tty

        def read(self, *args):
            raise AssertionError("The login launcher never reads an authorization code")

    monkeypatch.setattr(login, "sys", SimpleNamespace(stdin=Terminal(stdin_tty), stdout=Terminal(stdout_tty)))
    assert login.terminal_is_interactive() is expected


def test_unknown_login_mode_is_rejected(monkeypatch):
    arrange(monkeypatch)
    with pytest.raises(ValueError):
        asyncio.run(login.main(mode="unsupported"))
