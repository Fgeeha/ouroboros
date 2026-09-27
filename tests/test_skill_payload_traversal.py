"""Excluded dependency trees must not enlarge review-payload discovery."""

import hashlib
import os
from pathlib import Path

import pytest

from ouroboros.skill_loader import (
    _SKILL_DIR_CACHE_NAMES,
    _iter_payload_files,
    compute_content_hash,
)


def _write_files(root, files):
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)


def _expected_hash(files):
    digest = hashlib.sha256()
    for name, content in sorted(files.items()):
        digest.update(name.encode("utf-8") + b"\0" + hashlib.sha256(content).digest())
    return digest.hexdigest()


@pytest.mark.parametrize("cache_name", sorted(_SKILL_DIR_CACHE_NAMES))
def test_cache_directories_are_not_visited_but_declared_entries_are_hashed(
    tmp_path, monkeypatch, cache_name
):
    root = tmp_path / ".hidden-parent" / "skill"
    payload = {
        "SKILL.md": b"# Test payload\n",
        "plugin.py": b"def register(api): pass\n",
        ".helpers/ordinary.py": b"VALUE = 1\n",
        ".env.example": b"EXAMPLE = 'placeholder'\n",
    }
    declared = {
        f"{cache_name}/entry.py": b"def register(api): pass\n",
        f"nested/{cache_name}/run.py": b"print('declared')\n",
    }
    _write_files(root, {**payload, **declared, f"{cache_name}/package/deep/ignored.py": b"ignored"})
    _write_files(root, {"metadata/.DS_Store": b"metadata"})
    visited = []
    real_scandir = os.scandir

    def checked_scandir(path):
        relative = Path(path).relative_to(root)
        assert not any(part in _SKILL_DIR_CACHE_NAMES for part in relative.parts)
        visited.append(relative.as_posix())
        return real_scandir(path)

    with monkeypatch.context() as patch:
        patch.setattr(os, "scandir", checked_scandir)
        # Python 3.10/3.11 pathlib caches scandir in its accessor.
        if accessor := getattr(root, "_accessor", None):
            patch.setattr(accessor, "scandir", checked_scandir)
        assert compute_content_hash(root) == _expected_hash(payload)
        visits = list(visited)
        visited.clear()
        arguments = {
            "manifest_entry": f"{cache_name}/entry.py",
            "manifest_scripts": [{"name": f"nested/{cache_name}/run.py"}],
        }
        assert compute_content_hash(root, **arguments) == _expected_hash({**payload, **declared})
        assert sorted(visited) == sorted(visits)
        assert [p.relative_to(root).as_posix() for p in _iter_payload_files(root, **arguments)] == sorted({**payload, **declared})
        (root / f"{cache_name}/entry.py").write_bytes(b"changed declared entry")
        assert compute_content_hash(root, **arguments) != _expected_hash({**payload, **declared})
        assert compute_content_hash(root) == _expected_hash(payload)


def test_payload_walk_preserves_file_symlinks_without_following_directory_symlinks(tmp_path):
    root = tmp_path / "skill"
    payload = {"SKILL.md": b"# Skill\n", "nested/helper.py": b"VALUE = 1\n"}
    _write_files(root, payload)
    outside = tmp_path / "outside.py"
    outside.write_bytes(b"outside")
    try:
        (root / "alias.py").symlink_to(root / "nested/helper.py")
        (root / "directory-alias").symlink_to(root / "nested", target_is_directory=True)
        (root / "escape.py").symlink_to(outside)
        root_alias = tmp_path / "root-alias"
        root_alias.symlink_to(root, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    expected = _expected_hash({**payload, "alias.py": payload["nested/helper.py"]})
    assert compute_content_hash(root) == expected
    assert compute_content_hash(root_alias) == expected
