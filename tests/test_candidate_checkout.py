"""The browser candidate preserves dirty bytes and never writes the source Git state."""
from concurrent.futures import ThreadPoolExecutor
import os
import subprocess

import pytest

from tests import candidate_checkout as candidate

pytestmark = pytest.mark.serial


def test_ready_flag_does_not_hide_failed_supervisor():
    candidate.require_running_supervisor({"supervisor_ready": True, "workers_total": 1})
    for state in ({"supervisor_ready": True, "workers_total": 0},
                  {"supervisor_ready": True, "workers_total": 1, "supervisor_error": "init failed"}):
        with pytest.raises(candidate.CandidateError, match="CANDIDATE_SERVER_UNAVAILABLE"):
            candidate.require_running_supervisor(state)


def test_installed_project_cannot_supply_code_missing_from_candidate(monkeypatch):
    from importlib import metadata
    from types import SimpleNamespace

    monkeypatch.setattr(metadata, "distributions", lambda **kwargs: [
        SimpleNamespace(metadata={"Name": "ouroboros"})])
    with pytest.raises(candidate.CandidateError, match="--no-install-project"):
        candidate.require_candidate_interpreter()

    monkeypatch.setattr(metadata, "distributions", lambda **kwargs: [])
    candidate.require_candidate_interpreter()


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True).stdout


@pytest.fixture
def source(tmp_path):
    repo = tmp_path / "source"
    repo.mkdir()
    git(repo, "init", "-b", "candidate-test")
    git(repo, "config", "user.name", "Fixture")
    git(repo, "config", "user.email", "fixture@example.invalid")
    (repo / "edited").write_bytes(b"HEAD\n")
    (repo / "deleted").write_bytes(b"delete me\n")
    (repo / "recreated").write_bytes(b"old\n")
    (repo / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "synthetic test baseline")
    return repo


def test_staged_unstaged_new_binary_deleted_and_executable_bytes_survive(source, tmp_path):
    (source / "edited").write_bytes(b"staged\n")
    git(source, "add", "edited")
    (source / "edited").write_bytes(b"unstaged\r\n\x80\xff\x00")
    (source / "deleted").unlink()
    git(source, "rm", "recreated")
    (source / "recreated").write_bytes(b"new untracked after staged deletion\n")
    name = "new file\nwith tabs\t" if os.name != "nt" else "new file"
    (source / name).write_bytes(b"new\x00binary\xfe")
    (source / "executable").write_bytes(b"#!/bin/sh\nexit 0\n")
    (source / "executable").chmod(0o755)
    (source / "ignored").mkdir()
    (source / "ignored" / "artifact").write_bytes(b"not a Git candidate input")
    before = candidate.observe_candidate(source)
    target = tmp_path / "checkout"
    with candidate.candidate_checkout(source, target) as captured:
        assert captured.state == before and captured.overlay == {}
        assert git(target, "show", ":edited") == b"staged\n"
        assert (target / "edited").read_bytes() == b"unstaged\r\n\x80\xff\x00"
        assert not (target / "deleted").exists()
        assert not (target / "ignored").exists()
        assert (target / "recreated").read_bytes() == (source / "recreated").read_bytes()
        assert (target / "executable").stat().st_mode == (source / "executable").stat().st_mode
        assert candidate.observe_candidate(target).content_identity == before.content_identity
    assert candidate.observe_candidate(source) == before
    assert not target.exists()


def test_source_change_during_copy_is_explicit_failure(source, tmp_path, monkeypatch):
    copy = candidate._copy_candidate

    def racing_copy(*args):
        copy(*args)
        (source / "late-new").write_bytes(b"arrived during capture")

    monkeypatch.setattr(candidate, "_copy_candidate", racing_copy)
    with pytest.raises(candidate.CandidateError, match="CANDIDATE_CHANGED"):
        with candidate.candidate_checkout(source, tmp_path / "checkout"):
            pytest.fail("mixed candidate was admitted")
    assert (source / "late-new").read_bytes() == b"arrived during capture"


def test_empty_index_after_staged_deletions_is_preserved(source, tmp_path):
    git(source, "rm", "-r", ".")
    with candidate.candidate_checkout(source, tmp_path / "checkout") as captured:
        assert not captured.state.entries
        assert all(value is None for value in captured.state.files.values())


def test_split_index_is_refused(source, tmp_path):
    git(source, "update-index", "--split-index")
    with pytest.raises(candidate.CandidateError, match="split index"):
        with candidate.candidate_checkout(source, tmp_path / "checkout"):
            pytest.fail("unsupported index was accepted")


@pytest.mark.parametrize("mutation", ["source", "checkout"])
def test_mutation_after_start_invalidates_success(source, tmp_path, mutation):
    checkout = tmp_path / "checkout"
    with pytest.raises(candidate.CandidateError, match="CANDIDATE_CHANGED"):
        with candidate.candidate_checkout(source, checkout):
            selected = source if mutation == "source" else checkout
            (selected / "edited").write_bytes(b"unexpected modification")
    assert not checkout.exists()


@pytest.mark.parametrize("flag", ["--assume-unchanged", "--skip-worktree"])
def test_unsupported_index_flags_fail_instead_of_omitting_inputs(source, tmp_path, flag):
    git(source, "update-index", flag, "edited")
    with pytest.raises(candidate.CandidateError, match="CANDIDATE_UNSUPPORTED"):
        with candidate.candidate_checkout(source, tmp_path / "checkout"):
            pytest.fail("unsupported input was accepted")


def test_symlink_is_refused_without_dereferencing_foreign_bytes(source, tmp_path):
    foreign = tmp_path / "sentinel"
    foreign.write_bytes(b"private")
    try:
        (source / "link").symlink_to(foreign)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(candidate.CandidateError, match="CANDIDATE_UNSUPPORTED"):
        with candidate.candidate_checkout(source, tmp_path / "checkout"):
            pytest.fail("symlink was silently copied or omitted")
    assert foreign.read_bytes() == b"private"


@pytest.mark.skipif(os.name == "nt", reason="POSIX FIFO")
def test_special_file_is_refused_before_a_blocking_read(source, tmp_path):
    os.mkfifo(source / "fifo")
    with pytest.raises(candidate.CandidateError, match="CANDIDATE_UNSUPPORTED"):
        candidate.observe_candidate(source)


def test_two_independent_copies_preserve_the_same_source(source, tmp_path):
    before = candidate.observe_candidate(source)

    def run(number):
        target = tmp_path / str(number) / "checkout"
        with candidate.candidate_checkout(source, target) as snapshot:
            assert (target / ".git").is_dir()
            assert (target / "edited").read_bytes() == b"HEAD\n"
            return snapshot.identity

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(run, range(2))) == [before.identity] * 2
    assert candidate.observe_candidate(source) == before


def test_origin_proof_bytes_are_unique_per_checkout_and_absent_from_the_source(source, tmp_path):
    (source / "VERSION").write_text("9.9.9\n", encoding="utf-8")
    (source / "web").mkdir()
    (source / "web" / "index.html").write_bytes(b"<!doctype html>\n")
    before = candidate.observe_candidate(source)
    identities, sentinels, versions = set(), set(), set()
    for number in range(2):
        target = tmp_path / str(number) / "checkout"
        with candidate.candidate_checkout(source, target, origin_proof=True) as checkout:
            # Proof bytes live in the COPY only; the source keeps what we observed.
            assert (target / candidate.SENTINEL_PATH).read_bytes() == checkout.sentinel_bytes
            assert not (source / candidate.SENTINEL_PATH).exists()
            assert (source / "VERSION").read_text(encoding="utf-8") == "9.9.9\n"
            # A parseable build-metadata suffix, not an invented version.
            assert checkout.version_text.startswith("9.9.9+candidate.")
            assert (target / "VERSION").read_text(encoding="utf-8").strip() == checkout.version_text
            identities.add(checkout.identity)
            sentinels.add(checkout.sentinel_bytes)
            versions.add(checkout.version_text)
            candidate.verify_checkout(target, checkout)
    assert len(identities) == 1, "the selected source bytes are one identity"
    assert len(sentinels) == 2 and len(versions) == 2, "proof bytes must not repeat across checkouts"
    assert candidate.observe_candidate(source) == before


def test_origin_proof_refuses_a_candidate_without_the_bytes_it_must_prove(source, tmp_path):
    with pytest.raises(candidate.CandidateError, match="CANDIDATE_UNSUPPORTED"):
        with candidate.candidate_checkout(source, tmp_path / "checkout", origin_proof=True):
            pytest.fail("a candidate without VERSION/web cannot carry the origin proof")
    assert not (tmp_path / "checkout").exists()


def test_proof_bytes_removed_from_the_checkout_invalidate_success(source, tmp_path):
    (source / "VERSION").write_text("9.9.9\n", encoding="utf-8")
    (source / "web").mkdir()
    (source / "web" / "index.html").write_bytes(b"<!doctype html>\n")
    target = tmp_path / "checkout"
    with pytest.raises(candidate.CandidateError, match="CANDIDATE_CHANGED"):
        with candidate.candidate_checkout(source, target, origin_proof=True):
            (target / candidate.SENTINEL_PATH).unlink()
    assert not target.exists()
