"""Internal module-level state shared by the tool implementations.

Kept separate from ``tools.__init__`` to avoid circular imports.
"""

from __future__ import annotations

from pathlib import Path

from core.commands import GatingMode
from policy import PolicyEngine

_workspace_root: Path = Path.cwd()
_policy: PolicyEngine = PolicyEngine(
    allow_roots=[_workspace_root],
    deny_globs=[],
    never_allow_regex=[],
)
_policy.set_workspace(_workspace_root)


_active_dynamic_workspace_root: Path | None = _workspace_root


def set_workspace(path: str | Path) -> None:
    """Change the working directory used to resolve relative tool paths."""
    global _workspace_root, _active_dynamic_workspace_root
    new_root = Path(path).resolve()
    _workspace_root = new_root
    _policy.set_workspace(_workspace_root)

    # Revoke previously added workspace root if it exists in _policy._allow_roots
    if _active_dynamic_workspace_root is not None and _active_dynamic_workspace_root != new_root:
        # Check against _path_key comparison or normalized form
        try:
            from policy.engine import _long_path
            old_long = _long_path(_active_dynamic_workspace_root)
            _policy._allow_roots = [
                r for r in _policy._allow_roots
                if _long_path(r) != old_long
            ]
        except Exception:
            if _active_dynamic_workspace_root in _policy._allow_roots:
                _policy._allow_roots.remove(_active_dynamic_workspace_root)

    from policy.engine import _long_path
    long_new_root = _long_path(new_root)
    if not any(_long_path(r) == long_new_root for r in _policy._allow_roots):
        _policy._allow_roots.append(new_root)

    _active_dynamic_workspace_root = new_root


def set_policy(engine: PolicyEngine) -> None:
    """Replace the active policy engine."""
    global _policy, _active_dynamic_workspace_root
    _policy = engine
    _policy.set_workspace(_workspace_root)
    _active_dynamic_workspace_root = _workspace_root


def set_gating_mode(mode: GatingMode) -> None:
    """Set the gating mode on the active policy engine."""
    _policy.set_gating_mode(mode)


# Aliases that match the naming convention used by the rest of the codebase.
set_policy_engine = set_policy
set_workspace_root = set_workspace
