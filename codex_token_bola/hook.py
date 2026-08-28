from __future__ import annotations

try:
    from ._runtime.hook import main
except ModuleNotFoundError:
    from scripts.hook import main

__all__ = ["main"]


if __name__ == "__main__":
    raise SystemExit(main())
