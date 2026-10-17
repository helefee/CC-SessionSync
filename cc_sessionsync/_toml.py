"""读 TOML：Python 3.11+ 用标准库 tomllib，3.10 用 tomlkit（本来就是依赖）。只提供 loads。"""
from __future__ import annotations

try:
    from tomllib import loads  # noqa: F401
except ImportError:  # Python 3.10
    def loads(s: str) -> dict:
        import tomlkit
        return tomlkit.parse(s).unwrap()
