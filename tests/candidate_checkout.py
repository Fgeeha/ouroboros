"""Byte-faithful browser candidate checkout, with read-only source observation.

The input is Git's tracked paths plus non-ignored untracked files. Ignored local
artifacts are not candidate inputs. Symlinks, gitlinks, sparse/assume-unchanged,
split or unmerged indexes and special files fail explicitly. We copy raw bytes,
not a filtered Git diff. Two full observations plus per-read stat checks detect
concurrent changes; this is not a filesystem lock or an adversarial snapshot.

A fixture that must PROVE what a server ran asks for `origin_proof=True`: the
copy then carries two bytes nothing else has — a per-checkout static sentinel and
a per-checkout VERSION suffix — so a served response identifies THIS checkout
rather than merely agreeing with HEAD or with the state we wrote it from.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import urllib.request


# Served from the candidate's own web/ directory, and read back through the
# server's static mount. The name is inert for every other consumer.
SENTINEL_PATH = "web/candidate-sentinel.txt"
SENTINEL_ROUTE = "/static/candidate-sentinel.txt"
VERSION_PATH = "VERSION"


class CandidateError(RuntimeError):
    pass


def require_candidate_interpreter():
    """A dependency-only venv cannot supply deleted candidate code via site hooks."""
    from importlib.metadata import distributions
    import site

    # Ignore build metadata in the current source directory; inspect installed
    # distributions in the interpreter's site directories, including editables.
    if not any(dist.metadata["Name"] == "ouroboros"
               for dist in distributions(path=site.getsitepackages())):
        return
    raise CandidateError(
        "CANDIDATE_UNSUPPORTED installed ouroboros: use a dependency-only venv "
        "(uv sync --no-install-project) to prevent imports from another checkout"
    )


def _fetch(url, timeout=5):
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 - local test server
        return response.read()


def require_running_supervisor(state):
    if state.get("supervisor_error") or not state.get("supervisor_ready") or not state.get("workers_total"):
        raise CandidateError(f"CANDIDATE_SERVER_UNAVAILABLE: {state.get('supervisor_error') or 'no worker pool'}")


def assert_served_candidate(url, checkout, data_dir, pid, candidate):
    """Bind a healthy server to its owned PID, this checkout's bytes and its Python.

    Three independent claims, because none of them implies the next:

    * the answering process is the PID this fixture spawned (service binding);
    * the STATIC tree it serves is this checkout — proven by a sentinel asset
      that exists in no commit, in no other checkout and not in the source
      worktree, so agreement with HEAD or with our own copy cannot fake it;
    * the PYTHON it imported is this checkout's — `/api/health` reports
      `get_version()`, which reads VERSION through `ouroboros/version.py`'s OWN
      module path, so the per-checkout suffix can only come from a process whose
      `ouroboros` package was loaded from this directory.
    """
    from ouroboros.server_process import read_service_bindings

    binding = read_service_bindings(data_dir)["main"]
    require_running_supervisor(json.loads(_fetch(url + "/api/state")))
    assert binding["pid"] == pid, "health answered by a different server process"
    assert url == f"http://127.0.0.1:{binding['port']}"
    for relative, route in (("web/index.html", "/"),
                            ("web/modules/api_client.js", "/static/modules/api_client.js")):
        assert _fetch(url + route) == candidate.state.files[relative][0], relative
    assert _fetch(url + SENTINEL_ROUTE) == candidate.sentinel_bytes, (
        "the static tree served is not this candidate checkout")
    health = json.loads(_fetch(url + "/api/health"))
    assert health["runtime_version"] == candidate.version_text, (
        f"server Python came from outside the candidate checkout: {health['runtime_version']!r}")
    verify_checkout(checkout, candidate)


def _git(repo: Path, *args: str, data=None) -> bytes:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    result = subprocess.run(
        ["git", "-c", "core.fsmonitor=false", *args], cwd=repo, env=env,
        input=data, capture_output=True, timeout=120,
    )
    if result.returncode:
        raise CandidateError(f"CANDIDATE_GIT_FAILED {args[0]}: {os.fsdecode(result.stderr)}")
    return result.stdout


def _names(raw: bytes) -> set[str]:
    return {os.fsdecode(value) for value in raw.split(b"\0") if value}


def _filesystem_names(repo: Path) -> set[str]:
    # Git's untracked listing silently omits FIFOs/sockets. Walk the unignored
    # surface too, pruning ignored artifact trees before entering them.
    ignored = _names(_git(repo, "ls-files", "--others", "--ignored", "--exclude-standard",
                          "--directory", "-z"))
    names = set()
    for directory, dirs, files in os.walk(repo, followlinks=False, onerror=_walk_error):
        for name in list(dirs) + files:
            path = Path(directory) / name
            rel = path.relative_to(repo).as_posix()
            if rel == ".git" or rel in ignored or rel + "/" in ignored:
                if name in dirs:
                    dirs.remove(name)
                continue
            if name in dirs and not path.is_symlink():
                continue
            names.add(rel)
    return names


def _walk_error(error):
    raise CandidateError(f"CANDIDATE_UNREADABLE: {error}") from error


def _read(root: Path, name: str):
    path = root / name
    if Path(name).is_absolute() or ".." in Path(name).parts or ".git" in Path(name).parts:
        raise CandidateError(f"CANDIDATE_UNSUPPORTED path: {name!r}")
    for parent in path.parents:
        if parent == root:
            break
        if parent.is_symlink():
            raise CandidateError(f"CANDIDATE_UNSUPPORTED symlink directory: {name!r}")
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(before.st_mode):
        raise CandidateError(f"CANDIDATE_UNSUPPORTED non-regular file: {name!r}")
    with path.open("rb") as stream:
        content = stream.read()
        after = os.fstat(stream.fileno())
    signature = lambda s: (s.st_dev, s.st_ino, s.st_mode, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    if signature(before) != signature(after) or signature(after) != signature(path.lstat()):
        raise CandidateError(f"CANDIDATE_CHANGED while reading: {name!r}")
    return content, stat.S_IMODE(before.st_mode), signature(before)


def _content_digest(head: bytes, index: bytes, files: dict, prefix: bytes = b"") -> str:
    digest = hashlib.sha256(prefix + head + index)
    for name, value in sorted(files.items()):
        digest.update(os.fsencode(name) + b"\0")
        if value is None:
            digest.update(b"deleted\0")
        else:
            content, mode = value[0], value[1]
            digest.update(str(mode).encode() + b"\0" + hashlib.sha256(content).digest())
    return digest.hexdigest()


@dataclass
class CandidateState:
    head: bytes
    branch: bytes
    index: bytes
    status: bytes
    entries: bytes
    files: dict

    @property
    def identity(self) -> str:
        """Source identity: the branch a worktree is on is part of what we read."""
        return _content_digest(self.head, self.index, self.files, prefix=self.branch)

    @property
    def content_identity(self) -> str:
        """Branch-free: a clone names its own branch, so a copy can never match it."""
        return _content_digest(self.head, self.index, self.files)


@dataclass
class CandidateCheckout:
    """The selected source bytes plus the per-checkout proof bytes written over them."""

    state: CandidateState
    overlay: dict = field(default_factory=dict)

    @property
    def identity(self) -> str:
        return self.state.identity

    @property
    def checkout_identity(self) -> str:
        return _content_digest(self.state.head, self.state.index,
                               {**self.state.files, **self.overlay})

    @property
    def sentinel_bytes(self) -> bytes:
        return self.overlay[SENTINEL_PATH][0]

    @property
    def version_text(self) -> str:
        return self.overlay[VERSION_PATH][0].decode("utf-8").strip()


def _origin_proof_overlay(state: CandidateState) -> dict:
    """Per-checkout bytes for the static and Python origin proofs."""
    # Named `marker`, not `token`: this value is printed in diffs and evidence
    # projections, and a secret-shaped name gets it redacted out of review.
    marker = secrets.token_hex(16)
    version = state.files.get(VERSION_PATH)
    if version is None or state.files.get("web/index.html") is None:
        raise CandidateError(
            "CANDIDATE_UNSUPPORTED: the origin proof needs a candidate with VERSION and web/")
    # Build metadata, so every consumer that parses a version still parses this one.
    text = version[0].decode("utf-8").strip() + f"+candidate.{marker[:12]}\n"
    return {
        SENTINEL_PATH: (f"ouroboros candidate checkout {marker}\n".encode("utf-8"), 0o644),
        VERSION_PATH: (text.encode("utf-8"), version[1]),
    }


def observe_candidate(repo: Path) -> CandidateState:
    """Read HEAD, branch, index, status and every candidate byte without refreshing Git."""
    repo = Path(repo).resolve()
    if Path(os.fsdecode(_git(repo, "rev-parse", "--show-toplevel").rstrip(b"\n"))).resolve() != repo:
        raise CandidateError("CANDIDATE_UNSUPPORTED: source must be a repository root")
    head = _git(repo, "rev-parse", "--verify", "HEAD")
    branch = _git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if _git(repo, "rev-parse", "--shared-index-path").strip():
        raise CandidateError("CANDIDATE_UNSUPPORTED split index")
    entries = _git(repo, "ls-files", "--stage", "-z")
    for entry in entries.split(b"\0"):
        if entry:
            mode, _oid, stage = entry.split(b"\t", 1)[0].split()
            if mode != b"100644" and mode != b"100755" or stage != b"0":
                raise CandidateError("CANDIDATE_UNSUPPORTED symlink, gitlink or unmerged index")
    for entry in _git(repo, "ls-files", "-v", "-z").split(b"\0"):
        if entry and (entry[:1].islower() or entry[:1] == b"S"):
            raise CandidateError("CANDIDATE_UNSUPPORTED sparse/assume-unchanged index")
    index_path = Path(os.fsdecode(_git(repo, "rev-parse", "--git-path", "index").rstrip(b"\n")))
    if not index_path.is_absolute():
        index_path = repo / index_path
    index = index_path.read_bytes()
    status = _git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    names = _names(_git(repo, "ls-tree", "-r", "--name-only", "-z", "HEAD"))
    names |= _names(_git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard"))
    names |= _filesystem_names(repo)
    files = {name: _read(repo, name) for name in sorted(names)}
    return CandidateState(head, branch, index, status, entries, files)


def _copy_candidate(source: Path, target: Path, state: CandidateState) -> None:
    # No checkout: filters, hooks, EOL settings and HEAD's old file bytes never
    # participate. --no-local copies objects independently (no source hardlinks).
    _git(source, "clone", "--no-local", "--no-checkout", "--depth=1", "--",
         str(source), str(target))
    if _git(target, "rev-parse", "HEAD") != state.head:
        raise CandidateError("CANDIDATE_CHANGED: clone HEAD differs from captured HEAD")
    # Staged objects need not be reachable from HEAD. Transfer just those missing
    # blobs before installing the exact index; never write-tree on the source.
    oids = {entry.split()[1] for entry in state.entries.split(b"\0") if entry}
    checks = _git(target, "cat-file", "--batch-check", data=b"\n".join(sorted(oids)) + b"\n") if oids else b""
    for row in checks.splitlines():
        if row.endswith(b" missing"):
            oid = row.split()[0].decode("ascii")
            content = _git(source, "cat-file", "blob", oid)
            if _git(target, "hash-object", "-w", "--stdin", data=content).strip().decode() != oid:
                raise CandidateError("CANDIDATE_CHANGED staged object")
    (target / ".git" / "index").write_bytes(state.index)
    for name, value in state.files.items():
        if value is not None:
            content, mode, _signature = value
            path = target / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
            path.chmod(mode)


def verify_checkout(target: Path, checkout: CandidateCheckout) -> None:
    """Check executable/file identity before each boot and after final teardown."""
    if observe_candidate(target).content_identity != checkout.checkout_identity:
        raise CandidateError("CANDIDATE_CHANGED: fixture checkout differs from selected bytes/index/HEAD")


def _verify_source(source: Path, before: CandidateState, phase: str):
    after = observe_candidate(source)
    if after == before:
        return
    fields = [key for key in ("head", "branch", "index", "status", "entries")
              if getattr(before, key) != getattr(after, key)]
    paths = sorted(name for name in before.files.keys() | after.files.keys()
                   if name not in before.files or name not in after.files
                   or before.files[name] != after.files[name])
    raise CandidateError(f"CANDIDATE_CHANGED {phase}: metadata={fields!r}, paths={paths[:10]!r}; "
                         "retry with a stable source")


@contextmanager
def candidate_checkout(source: Path, target: Path, *, origin_proof: bool = False):
    """Own an independent dirty checkout and refuse source drift even after a green test."""
    source, target = Path(source).resolve(), Path(target).resolve()
    if target.exists():
        raise CandidateError("CANDIDATE_UNSUPPORTED: destination must not exist")
    target.parent.mkdir(parents=True, exist_ok=True)
    if source in target.parents:
        ignored = subprocess.run(["git", "check-ignore", "-q", str(target)], cwd=source, timeout=10)
        if ignored.returncode:
            raise CandidateError("CANDIDATE_UNSUPPORTED: destination inside source must be ignored")
    before = observe_candidate(source)
    checkout = CandidateCheckout(before, _origin_proof_overlay(before) if origin_proof else {})
    assembled = False
    try:
        _copy_candidate(source, target, before)
        # Written only into the copy: the source keeps the bytes we observed.
        for name, (content, mode) in checkout.overlay.items():
            (target / name).write_bytes(content)
            (target / name).chmod(mode)
        _verify_source(source, before, "during capture")
        verify_checkout(target, checkout)
        assembled = True
        yield checkout
    finally:
        try:
            _verify_source(source, before, "during browser verification")
            if assembled:
                verify_checkout(target, checkout)
        finally:
            if target.exists():
                from ouroboros.subagent_worktrees import _force_rmtree

                _force_rmtree(target)  # Also handles Windows read-only Git objects.
                if target.exists():
                    raise CandidateError(f"CANDIDATE_CLEANUP_FAILED: {target}")
