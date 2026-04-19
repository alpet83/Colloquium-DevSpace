"""Runtime bridge to current SandwichPack implementation.

Keeps MCP local runtime aligned with `agent/lib/sandwich_pack.py` without
manual copy/paste drift.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _load_agent_sandwich_pack() -> ModuleType:
    here = Path(__file__).resolve()
    repo_root = here.parents[2]
    lib_dir = repo_root / "agent" / "lib"
    init_py = lib_dir / "__init__.py"
    sp_py = lib_dir / "sandwich_pack.py"
    pkg_name = "_agent_runtime_lib"
    mod_name = f"{pkg_name}.sandwich_pack"

    if pkg_name not in sys.modules:
        pkg_spec = importlib.util.spec_from_file_location(
            pkg_name,
            init_py,
            submodule_search_locations=[str(lib_dir)],
        )
        if pkg_spec is None or pkg_spec.loader is None:
            raise RuntimeError(f"Cannot load package from {init_py}")
        pkg_mod = importlib.util.module_from_spec(pkg_spec)
        sys.modules[pkg_name] = pkg_mod
        pkg_spec.loader.exec_module(pkg_mod)

    if mod_name in sys.modules:
        return sys.modules[mod_name]

    mod_spec = importlib.util.spec_from_file_location(mod_name, sp_py)
    if mod_spec is None or mod_spec.loader is None:
        raise RuntimeError(f"Cannot load SandwichPack from {sp_py}")
    module = importlib.util.module_from_spec(mod_spec)
    sys.modules[mod_name] = module
    mod_spec.loader.exec_module(module)
    return module


_mod = _load_agent_sandwich_pack()

SandwichPack = _mod.SandwichPack
compute_md5 = _mod.compute_md5
estimate_tokens = _mod.estimate_tokens

__all__ = ["SandwichPack", "compute_md5", "estimate_tokens"]
