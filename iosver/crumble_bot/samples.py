"""Load captured StageClearReport / ClientBattleReport binaries as templates."""
from __future__ import annotations

import random
from pathlib import Path
from typing import List, Optional, Tuple


class SampleBank:
    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        self.clear_reports: List[Path] = sorted(self.directory.glob("StageClearReport_*.bin"))
        self.client_reports: List[Path] = sorted(self.directory.glob("ClientBattleReport_*.bin"))

    def pick_clear(self) -> Optional[bytes]:
        if not self.clear_reports:
            return None
        return random.choice(self.clear_reports).read_bytes()

    def pick_client(self) -> Optional[bytes]:
        if not self.client_reports:
            return None
        # prefer larger (more realistic) reports
        paths = sorted(self.client_reports, key=lambda p: p.stat().st_size, reverse=True)
        return paths[0].read_bytes()

    def pick_pair(self) -> Tuple[Optional[bytes], Optional[bytes]]:
        return self.pick_clear(), self.pick_client()
