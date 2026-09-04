"""Regression guard for the Windows aiodns/aiohttp DNS bug (see
src/utils/http.py's docstring and the commit that fixed it:
"Fix aiodns resolver failure on Windows; unblock all REST calls").

Guards against a future call site constructing its own aiohttp.ClientSession()
and silently bypassing src/utils/http.py's make_session().
"""
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SESSION_CONSTRUCTOR = re.compile(r"aiohttp\.ClientSession\(")


def test_only_utils_http_py_constructs_a_client_session():
    offenders = []
    for path in PROJECT_ROOT.rglob("*.py"):
        if ".venv" in path.parts or "tests" in path.parts:
            continue
        if path == PROJECT_ROOT / "src" / "utils" / "http.py":
            continue
        text = path.read_text(encoding="utf-8")
        if SESSION_CONSTRUCTOR.search(text):
            offenders.append(str(path.relative_to(PROJECT_ROOT)))

    assert offenders == [], (
        "aiohttp.ClientSession() constructed outside src/utils/http.py - this "
        "bypasses the ThreadedResolver DNS fix. Use src.utils.http.make_session() "
        f"instead. Offending file(s): {offenders}"
    )
