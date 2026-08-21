"""Repository discovery and catalog presentation."""

from __future__ import annotations

from pathlib import Path


def is_initialized(path: Path) -> bool:
    try:
        return (path / ".coordination" / "README.md").is_file()
    except OSError:
        return False


def is_git_repository(path: Path) -> bool:
    """Report whether `path` has a direct `.git` marker (file or directory).

    This intentionally never invokes Git or inspects the marker's contents;
    it is a cheap, local structural check only.
    """
    try:
        marker = path / ".git"
        return marker.is_file() or marker.is_dir()
    except OSError:
        return False


def discover_repositories(root: Path, active_repo: Path) -> list[dict[str, object]]:
    """Discover Git direct children of `root`, plus the active repo.

    A direct child is included only when it is itself a Git repository (has
    a direct `.git` file-or-directory marker); coordination initialization is
    not a discovery filter. The active repository is included even when it is
    not a direct child of `root`, but only when it is either a Git repository
    or already coordination-initialized, so the initial configuration stays
    usable without exposing arbitrary directories. Paths are resolved and
    deduplicated, and every entry carries a dynamically derived `initialized`
    flag. Entries are sorted by case-insensitive display name, then by path.
    """
    candidates: list[Path] = []
    if root.is_dir():
        try:
            children = sorted(root.iterdir())
        except OSError:
            children = []
        for child in children:
            try:
                if child.is_dir() and is_git_repository(child):
                    candidates.append(child)
            except OSError:
                continue

    seen: set[Path] = set()
    entries: list[dict[str, object]] = []
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        entries.append(
            {"name": resolved.name, "path": str(resolved), "initialized": is_initialized(resolved)}
        )

    try:
        active_resolved = active_repo.resolve()
    except OSError:
        active_resolved = None
    if active_resolved is not None and active_resolved not in seen:
        if is_git_repository(active_resolved) or is_initialized(active_resolved):
            entries.append(
                {
                    "name": active_resolved.name,
                    "path": str(active_resolved),
                    "initialized": is_initialized(active_resolved),
                }
            )

    entries.sort(key=lambda entry: (str(entry["name"]).lower(), str(entry["path"])))
    return entries


def catalog_payload(
    entries: list[dict[str, object]], active_repo: Path, root: Path
) -> dict[str, object]:
    active_str = str(active_repo)
    return {
        "root": str(root),
        "active": active_str,
        "entries": [
            {
                "name": entry["name"],
                "path": entry["path"],
                "active": entry["path"] == active_str,
                "initialized": entry["initialized"],
            }
            for entry in entries
        ],
    }
