"""The one place that decides where the install lives.

Order: ``KARAKOS_HOME``, then ``WORKSPACE_ROOT`` (what the container sets),
then the checkout this file sits in. Nothing else may hardcode a root.
"""
from __future__ import annotations

import os
from pathlib import Path

_CHECKOUT = Path(__file__).resolve().parent.parent


def workspace_root(default: str | os.PathLike | None = None) -> str:
    """``default`` replaces the checkout fallback for callers that patch their own base."""
    return (os.environ.get("KARAKOS_HOME") or os.environ.get("WORKSPACE_ROOT")
            or str(default if default is not None else _CHECKOUT))


def workspace_path(default: str | os.PathLike | None = None) -> Path:
    return Path(workspace_root(default))
