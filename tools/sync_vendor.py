"""Copy the pyiotopen library into custom_components/iotopen/pyiotopen/.

The integration imports the library relatively (``from .pyiotopen import ...``) so it runs on
any Home Assistant without installing anything. pyiotopen/src/pyiotopen is the canonical source;
run this after changing it:  python tools/sync_vendor.py
"""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "pyiotopen" / "src" / "pyiotopen"
DST = ROOT / "custom_components" / "iotopen" / "pyiotopen"

DST.mkdir(exist_ok=True)
for path in SRC.iterdir():
    if path.suffix in (".py", ".typed"):
        shutil.copy2(path, DST / path.name)
        print("copied", path.name)
