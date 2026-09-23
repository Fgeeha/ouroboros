"""git's binary verdict for an untracked inventory comes from ONE process (#1241).

One heavy delegated snapshot spawned ``git diff --no-index --numstat`` once per
untracked file — 66,327 processes, 36 of the 40 minutes it held the machine-wide
worktree lock. ``untracked_binary_verdicts`` stages every regular file as the
empty blob into a scratch index and asks one index-versus-worktree ``git diff``,
so the answer stays git's own (attributes, diff drivers, clean filters and
working-tree encodings included) and parity with the per-file verdict is exact.
"""

from __future__ import annotations

import os
import pathlib
import subprocess

from ouroboros import workspace_patch_capture as capture


def _git(cwd, *args, **kw):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, check=True, **kw)


def _fixture_repo(root: pathlib.Path) -> tuple[pathlib.Path, list[str]]:
    """Every class the verdict can be asked about, plus the config git consults."""
    repo = root / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    _git(repo, "config", "diff.drvbin.binary", "true")
    _git(repo, "config", "diff.drvtext.binary", "false")
    _git(repo, "config", "filter.stripnul.clean", "tr -d '\\000'")
    (repo / ".gitattributes").write_text(
        "*.dat -diff\n*.bin binary\n*.txt diff\n*.drv diff=drvbin\n*.drt diff=drvtext\n"
        "*.lfs filter=stripnul\n*.u16 working-tree-encoding=UTF-16\n", encoding="utf-8")
    _git(repo, "add", ".gitattributes")
    _git(repo, "commit", "-qm", "attrs")
    files = {
        "plain.dat": b"hello\n",             # -diff attribute: binary whatever the bytes
        "plain.bin": b"hello\n",             # binary macro
        "nul.txt": b"x\0y",                  # diff set: text despite the NUL
        "nul.md": b"x\0y",                   # unspecified: NUL in the first 8000 bytes
        "late.md": b"a" * 9000 + b"\0",      # NUL past git's probe: text
        "edge7999.md": b"a" * 7999 + b"\0",  # NUL at offset 7999: binary
        "edge8000.md": b"a" * 8000 + b"\0",  # NUL at offset 8000: text
        "nonul.drv": b"hello\n",             # driver with binary=true and no NUL: binary
        "nul.drt": b"x\0y",                  # driver with binary=false and a NUL: text
        "nul.lfs": b"x\0y",                  # clean filter strips the NUL: text
        "text.u16": "hi\n".encode("utf-16"),  # working-tree encoding: text
        "empty.md": b"",
        "target.bin2": b"x\0y",
        "new\nline.md": b"nl\n",             # a newline in the name survives -z
        "vanish.md": b"gone\n",
    }
    for name, data in files.items():
        (repo / name).write_bytes(data)
    os.symlink("target.bin2", repo / "link_to_bin")
    os.symlink("nowhere", repo / "dangling")
    rels = sorted(files) + ["link_to_bin", "dangling"]
    return repo, rels


def _oracle(repo: pathlib.Path, rel: str) -> bool:
    """Today's per-file verdict: ``-\\t-`` from ``git diff --no-index --numstat``."""
    proc = subprocess.run(
        ["git", "diff", "--no-index", "--numstat", "--no-ext-diff", "--no-color", "--", os.devnull, rel],
        cwd=str(repo), capture_output=True)
    first = proc.stdout.decode("utf-8", errors="replace").strip().splitlines()
    return bool(first) and first[0].startswith("-\t-")


def test_batch_verdict_matches_git_for_every_path_class(tmp_path):
    repo, rels = _fixture_repo(tmp_path)
    oracle = {rel: _oracle(repo, rel) for rel in rels}
    (repo / "vanish.md").unlink()  # vanished between the listing and the verdict: text, like git says
    oracle["vanish.md"] = False
    warnings: list = []

    verdicts = capture.untracked_binary_verdicts(repo, rels, warnings=warnings)

    assert verdicts is not None and warnings == []
    assert {rel: verdicts.get(rel, False) for rel in rels} == oracle
    # The classes that a NUL sniff or an attribute lookup alone would get wrong.
    assert verdicts["nonul.drv"] and not verdicts["nul.drt"] and not verdicts["nul.lfs"] and not verdicts["text.u16"]
    assert verdicts["edge7999.md"] and not verdicts["edge8000.md"]
    assert "link_to_bin" not in verdicts and "dangling" not in verdicts  # symlinks: text, never followed
    # Only the empty blob entered the target's object database: no content was hashed.
    objects = _git(repo, "count-objects").stdout.decode()
    assert objects.startswith("4 objects"), objects


def test_capture_asks_one_process_and_never_one_per_file(tmp_path, monkeypatch):
    repo, rels = _fixture_repo(tmp_path)
    calls: list = []
    real_run = subprocess.run

    def spy(cmd, *args, **kwargs):
        calls.append(list(cmd))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(capture.subprocess, "run", spy)
    artifacts, manifest = capture.write_workspace_patch_artifacts(repo, tmp_path / "artifacts", task={})

    assert manifest["status"] == "ready_with_changes", manifest["errors"]
    numstat_calls = [c for c in calls if "--no-index" in c and "--numstat" in c]
    assert numstat_calls == [], "the per-file --no-index --numstat spawn is back"
    assert sum(1 for c in calls if c[:2] == ["git", "diff"] and "--numstat" in c) == 1
    excluded = {row["path"]: row["reason"] for row in manifest["untracked_excluded"]}
    expected_binary = {rel for rel in rels if _oracle(repo, rel)}
    assert {rel for rel, reason in excluded.items() if reason == "binary file"} == expected_binary


def test_a_failed_batch_falls_back_to_the_per_file_verdict_with_a_warning(tmp_path, monkeypatch):
    repo, rels = _fixture_repo(tmp_path)
    real_run = subprocess.run

    def broken_read_tree(cmd, *args, **kwargs):
        if list(cmd[:2]) == ["git", "read-tree"]:
            raise OSError("scratch index unavailable")
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(capture.subprocess, "run", broken_read_tree)
    warnings: list = []
    assert capture.untracked_binary_verdicts(repo, rels, warnings=warnings) is None
    assert warnings and warnings[0]["reason"] == "binary_verdict_batch_unavailable"
    # ``None`` keeps git's per-file verdict: the same answer, one process per file.
    for rel in ("plain.dat", "nul.drt", "late.md"):
        reason = capture.untracked_capture_veto_reason(repo, rel, binary_verdicts=None)
        assert (reason == "binary file") == _oracle(repo, rel), rel
    assert capture.untracked_binary_verdicts(repo, [], warnings=warnings) == {}
