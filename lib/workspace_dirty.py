"""Report uncommitted files in the workspace at startup. stdlib only; never raises."""
import subprocess
from pathlib import Path
from typing import List

MAX_NAMED = 20

def dirty_files(root) -> List[str]:
    root = Path(root)
    if not (root / ".git").exists():
        return []
    try:
        out = subprocess.run(["git", "-C", str(root), "status", "--porcelain"],
                             capture_output=True, text=True, timeout=20)
    except Exception:
        return []
    if out.returncode != 0:
        return []
    return [line[3:] for line in out.stdout.splitlines() if line.strip()]

def describe(files: List[str], crashed: bool) -> str:
    if not files:
        return ""
    shown = ", ".join(files[:MAX_NAMED])
    more = f" (+{len(files) - MAX_NAMED} more)" if len(files) > MAX_NAMED else ""
    lead = ("The previous run crashed and left uncommitted files"
            if crashed else "The workspace has uncommitted files")
    return (f"{lead}: {shown}{more}. A half-written file can make the next error look "
            "unrelated. Read the first error before restarting again.")
