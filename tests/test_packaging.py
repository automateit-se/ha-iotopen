"""The integration must run on a plain Home Assistant without installing pyiotopen."""

from __future__ import annotations

import re
from pathlib import Path

INTEGRATION = Path(__file__).resolve().parent.parent / "custom_components" / "iotopen"


def test_only_relative_pyiotopen_imports():
    offenders = [
        f"{path.name}:{n}"
        for path in INTEGRATION.glob("*.py")
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if re.match(r"\s*(from pyiotopen\b|import pyiotopen\b)", line)
    ]
    assert not offenders, f"use 'from .pyiotopen import ...': {offenders}"


def test_vendored_library_matches_source():
    src = INTEGRATION.parent.parent / "pyiotopen" / "src" / "pyiotopen"
    for path in src.glob("*.py"):
        vendored = INTEGRATION / "pyiotopen" / path.name
        assert vendored.read_bytes() == path.read_bytes(), f"run tools/sync_vendor.py ({path.name})"
