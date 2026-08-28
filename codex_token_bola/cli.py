from __future__ import annotations

try:
    from ._runtime.bola import main
except ModuleNotFoundError:
    from scripts.bola import main

__all__ = ["main"]
