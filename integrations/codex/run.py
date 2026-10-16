"""Stable entrypoint called by the installed, isolated Python launcher."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from noname_harness.codex_bridge import main

raise SystemExit(main())
