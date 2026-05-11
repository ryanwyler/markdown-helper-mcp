"""markdown_guide: print the agent guide as markdown."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import _common as c


def register_parser(sub) -> None:
    p = sub.add_parser("guide", help="print the agent guide as markdown")
    c.add_pretty(p)
    p.set_defaults(func=cmd_guide)


def cmd_guide(args: argparse.Namespace) -> int:
    install_root = Path(__file__).resolve().parents[3]
    guide_path = install_root / "docs" / "GUIDE.md"
    if not guide_path.exists():
        c.emit_error({"error": f"guide not found at {guide_path}"})
        return 1
    print(guide_path.read_text(encoding="utf-8"))
    return 0
