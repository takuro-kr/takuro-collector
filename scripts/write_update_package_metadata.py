from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from takuro_collector import __version__


def main() -> int:
    target = Path(sys.argv[1])
    target.write_text(
        json.dumps({"schema_version": 1, "version": __version__}, separators=(",", ":")),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
