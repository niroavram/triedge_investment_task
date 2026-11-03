"""Thin, read-mostly git helpers. All writes go through temporary worktrees."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path


class GitError(RuntimeError):
    pass


def git(repo: str | Path, *args: str, check: bool = True) -> str:
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if check and p.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {p.stderr.strip()}")
    return p.stdout


def rev_parse(repo: str | Path, ref: str) -> str:
    return git(repo, "rev-parse", ref).strip()


def show_file(repo: str | Path, sha: str, path: str) -> str | None:
    """File content at a commit, or None if absent. Used to read config from the BASE sha."""
    p = subprocess.run(
        ["git", "-C", str(repo), "show", f"{sha}:{path}"], capture_output=True, text=True
    )
    return p.stdout if p.returncode == 0 else None


def materialize(repo: str | Path, sha: str, dest: Path | None = None) -> Path:
    """Write a commit's files into a fresh directory (no .git). Returns the directory.

    Deliberately not `git archive`: that honours the tree's own `.gitattributes`, so a PR
    could `export-ignore` a file and hide it from every scanner. ls-tree + cat-file reads
    the exact blobs. Symlinks and submodules are skipped (never followed out of the tree).
    """
    dest = dest or Path(tempfile.mkdtemp(prefix="pr-agent-"))
    listing = subprocess.run(
        ["git", "-C", str(repo), "ls-tree", "-r", "-z", "--full-tree", sha],
        capture_output=True,
        check=True,
    ).stdout
    entries = []
    for record in listing.split(b"\0"):
        if not record:
            continue
        meta, _, path = record.partition(b"\t")
        mode, kind, blob = meta.split()
        if kind == b"blob" and mode != b"120000":  # regular files only
            entries.append((blob.decode(), path.decode(), mode == b"100755"))
    if not entries:
        return dest
    batch = subprocess.run(
        ["git", "-C", str(repo), "cat-file", "--batch"],
        input="".join(f"{blob}\n" for blob, _, _ in entries).encode(),
        capture_output=True,
        check=True,
    ).stdout
    pos = 0
    for _, rel, executable in entries:
        header_end = batch.index(b"\n", pos)
        size = int(batch[pos:header_end].split()[2])
        content = batch[header_end + 1 : header_end + 1 + size]
        pos = header_end + 1 + size + 1  # trailing newline after each object
        target = (dest / rel).resolve()
        if not target.is_relative_to(dest.resolve()):
            continue  # defensive: never write outside dest
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        if executable:
            target.chmod(0o755)
    return dest


def apply_patch(workdir: Path, patch: str, check_only: bool = False) -> tuple[bool, str]:
    """`git apply` a unified diff inside a (non-git) directory."""
    args = ["git", "apply", "--whitespace=nowarn"] + (["--check"] if check_only else []) + ["-"]
    p = subprocess.run(args, cwd=workdir, input=patch, capture_output=True, text=True)
    return p.returncode == 0, p.stderr
