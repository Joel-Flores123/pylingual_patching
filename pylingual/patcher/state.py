from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path

from pylingual.patcher.analysis import PatchSession


class PatchState:
    """The decompiled source being patched and every version of it that has been checked"""

    def __init__(self, session: PatchSession, source_path: Path, output_path: Path):
        self.session = session
        self.source_path = source_path
        self.output_path = output_path
        self.initial_source = source_path.read_text(encoding="utf-8")
        self.patches: list[dict] = []
        self.lock = threading.Lock()
        self.check(self.initial_source)

    def check(self, source: str) -> dict:
        with self.lock:
            analysis = self.session.analyze(source)
            patch = {
                "id": len(self.patches),
                "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "summary": analysis["equivalence"]["summary"],
                "successful": analysis["equivalence"]["successful"],
                "source": source,
                "analysis": analysis,
            }
            self.patches.append(patch)
            return patch

    def patch_list(self) -> list[dict]:
        return [{k: v for k, v in patch.items() if k not in ("source", "analysis")} for patch in self.patches]

    def save(self, source: str) -> Path:
        with self.lock:
            self.output_path.write_text(source, encoding="utf-8")
        return self.output_path
