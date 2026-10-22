"""Launcher scripts must stay parseable by Windows PowerShell 5.1.

The desktop shortcut runs ``powershell.exe -File scripts/console.ps1``. That
host reads a BOM-less script with the local ANSI code page, so a UTF-8 file with
Chinese text is decoded into different characters and the parser fails — the
window flashes and nothing starts. Editors that drop the BOM therefore break the
only entry point users double-click, which is why this is asserted here.
"""
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
BOM = b"\xef\xbb\xbf"


def powershell_scripts() -> list[Path]:
    return sorted(SCRIPTS.glob("*.ps1"))


def test_launcher_scripts_exist():
    names = {path.name for path in powershell_scripts()}
    assert {"console.ps1", "stop.ps1", "launch.ps1"} <= names


def test_scripts_with_non_ascii_keep_utf8_bom():
    broken = []
    for path in powershell_scripts():
        raw = path.read_bytes()
        if raw.startswith(BOM) or raw.isascii():
            continue
        broken.append(path.name)
    assert not broken, f"缺少 UTF-8 BOM（Windows PowerShell 5.1 会解析失败）：{broken}"


def test_console_and_stop_have_bom():
    for name in ("console.ps1", "stop.ps1"):
        assert (SCRIPTS / name).read_bytes().startswith(BOM), name


def test_script_text_decodes_as_utf8():
    for path in powershell_scripts():
        path.read_text(encoding="utf-8")  # raises UnicodeDecodeError on bad bytes
