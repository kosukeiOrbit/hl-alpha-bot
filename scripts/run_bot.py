"""hl-alpha-bot 起動スクリプト。

Usage:
    python scripts/run_bot.py
    python scripts/run_bot.py --profile config/profile_phase0.yaml
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.main import main

if __name__ == "__main__":
    main()
