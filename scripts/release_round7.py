"""Build and verify a small source candidate without copying a user's data.

This ZIP is not an executable installer or a signed release.  Hashes detect a
changed member relative to its manifest; they do not authenticate a publisher.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tomllib
import zipfile


MAX_MEMBER_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_MEMBERS = 2048
FIXED_TIME = (2000, 1, 1, 0, 0, 0)
CANDIDATE = "round7-source-candidate"
_GENERATED = frozenset({"start_suwan.py", "start_gui.bat", "start_repl.bat"})
_FIXED = frozenset({"pyproject.toml", "README.md", "seven-day-log.json",
                    "scripts/observe_round7.py", "scripts/_offline.py", "scripts/release_round7.py"})
_REQUIRED = _FIXED | _GENERATED | frozenset({"src/suwan/__init__.py", "src/suwan/app.py",
    "src/suwan/config.py", "src/suwan/frontends/tkapp.py", "src/suwan/frontends/repl.py"})
_IGNORED = frozenset({".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"})
_RESERVED = frozenset({"CON", "PRN", "AUX", "NUL", *(f"COM{n}" for n in range(1, 10)),
                       *(f"LPT{n}" for n in range(1, 10))})
_MANIFEST_KEYS = frozenset({"format_version", "candidate", "source_version", "dependencies_bundled",
                           "installation_claim", "files"})

# Keep the launcher ASCII so Windows can run it independently of a console's
# legacy code page.  Both paths pass --home explicitly, including the GUI.
_LAUNCHER = '''"""Start the source candidate with an explicitly chosen data home."""
from pathlib import Path
import argparse
import asyncio
import importlib.util
import os
import sys

def main(argv=None):
    parser = argparse.ArgumentParser(description="Suwan source candidate (dependencies required)")
    parser.add_argument("--home", required=True, help="Explicit writable data directory")
    parser.add_argument("--frontend", choices=("gui", "repl"), default="gui")
    parser.add_argument("--smoke", help="One local REPL question, then exit")
    parser.add_argument("--offline", action="store_true", help="Block network and clear inherited provider settings")
    args = parser.parse_args(argv)
    if not args.home.strip():
        parser.error("--home must not be empty")
    if args.smoke is not None and args.frontend != "repl":
        parser.error("--smoke requires --frontend repl")
    root = Path(__file__).resolve().parent
    home = Path(args.home).expanduser().absolute()
    sys.path.insert(0, str(root))
    os.environ["SUWAN_SKIP_DOTENV"] = "1"
    if args.offline:
        # Load our guard by its bundled file path: an unrelated regular
        # package named scripts can otherwise mask this namespace directory.
        spec = importlib.util.spec_from_file_location("_suwan_candidate_offline", root / "scripts" / "_offline.py")
        if spec is None or spec.loader is None:
            raise RuntimeError("bundled offline guard is unavailable")
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        guard.install_offline_guard()
    os.environ["SUWAN_HOME"] = str(home)
    sys.path.insert(0, str(root / "src"))
    if args.offline:
        from suwan.app import SuwanRuntime
        class LocalRuntime(SuwanRuntime):
            def __init__(self, *values, **options):
                options.update(api_enabled=False, network_enabled=False)
                super().__init__(*values, **options)
    if args.frontend == "gui":
        from suwan.frontends import tkapp
        if args.offline:
            tkapp.SuwanRuntime = LocalRuntime
        return tkapp.main(["--home", str(home)])
    from suwan.frontends import repl
    if args.offline:
        repl.SuwanRuntime = LocalRuntime
    options = ["--home", str(home)]
    if args.smoke is not None:
        options += ["--smoke", args.smoke]
    return asyncio.run(repl.amain(options))

if __name__ == "__main__":
    raise SystemExit(main())
'''


class ReleaseError(ValueError):
    """A candidate failed a packaging or integrity requirement."""


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _is_link(path: Path) -> bool:
    return path.is_symlink() or path.is_junction()


def _reject_links(path: Path, *, stop: Path | None = None) -> None:
    """Check lexical ancestors before resolve/open can follow a link."""
    current = path
    while True:
        if _is_link(current):
            raise ReleaseError("symbolic links/junctions are not allowed")
        if current == stop or current.parent == current:
            break
        current = current.parent


def _safe_name(name: str) -> str:
    if type(name) is not str or not name or "\\" in name or name.startswith("/"):
        raise ReleaseError("unsafe archive path")
    parts = name.split("/")
    for part in parts:
        if (part in {"", ".", ".."} or not re.fullmatch(r"[A-Za-z0-9_.-]+", part)
                or part.endswith(".") or part.split(".", 1)[0].upper() in _RESERVED
                or part.lower().startswith(".env")):
            raise ReleaseError("unsafe archive path")
    if str(PurePosixPath(name)) != name:
        raise ReleaseError("noncanonical archive path")
    return name


def _allowed(name: str) -> bool:
    if name in _FIXED or name in _GENERATED:
        return True
    parts = name.split("/")
    return (len(parts) >= 3 and parts[:2] == ["src", "suwan"] and name.endswith(".py")
            and not any(part.lower() in _IGNORED for part in parts))


def _reject_file_parent_conflicts(folded_names: set[str]) -> None:
    # Every accepted member is a file.  Its ancestors must therefore be
    # directories, even when their spelling differs only by Windows case.
    # Check the complete set so ZIP member order cannot hide a conflict.
    for name in folded_names:
        parts = name.split("/")
        for length in range(1, len(parts)):
            if "/".join(parts[:length]) in folded_names:
                raise ReleaseError("archive file conflicts with a parent directory")


def _read_source(root: Path, name: str) -> bytes:
    _safe_name(name)
    path = root.joinpath(*name.split("/"))
    _reject_links(path, stop=root)
    try:
        if not stat.S_ISREG(path.stat(follow_symlinks=False).st_mode):
            raise ReleaseError(f"required source is not a regular file: {name}")
        with path.open("rb") as stream:
            payload = stream.read(MAX_MEMBER_BYTES + 1)
    except FileNotFoundError as exc:
        raise ReleaseError(f"required source is missing: {name}") from exc
    _reject_links(path, stop=root)
    if len(payload) > MAX_MEMBER_BYTES:
        raise ReleaseError(f"source member exceeds size limit: {name}")
    return payload


def _python_members(root: Path) -> dict[str, bytes]:
    package = root / "src" / "suwan"
    _reject_links(package, stop=root)
    if not package.is_dir():
        raise ReleaseError("required src/suwan directory is missing")
    result = {}
    def walk(directory):
        for path in sorted(directory.iterdir(), key=lambda value: value.name):
            lower = path.name.lower()
            if lower in _IGNORED or lower.startswith(".env"):
                continue
            if _is_link(path):
                raise ReleaseError("symbolic links/junctions are not allowed in src/suwan")
            if path.is_dir():
                walk(path)
            elif path.suffix == ".py":
                name = path.relative_to(root).as_posix()
                result[_safe_name(name)] = _read_source(root, name)
    walk(package)
    return result


def _bat(frontend: str) -> bytes:
    return ("@echo off\r\nsetlocal\r\n"
            "if not defined LOCALAPPDATA (\r\n"
            "  echo LOCALAPPDATA is unavailable. Run start_suwan.py with --home.\r\n"
            "  exit /b 1\r\n)\r\n"
            f'python "%~dp0start_suwan.py" --home "%LOCALAPPDATA%\\Suwan\\home" --frontend {frontend}\r\n'
            "if errorlevel 1 pause\r\n").encode("ascii")


def _version(payload: bytes) -> str:
    try:
        value = tomllib.loads(payload.decode("utf-8"))["project"]["version"]
    except (UnicodeError, ValueError, KeyError, TypeError) as exc:
        raise ReleaseError("pyproject.toml must declare a project version") from exc
    if type(value) is not str or not re.fullmatch(r"[A-Za-z0-9_.+-]{1,64}", value):
        raise ReleaseError("invalid source project version")
    return value


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, FIXED_TIME)
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | 0o644) << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def build_release(source_root: str | Path, output_zip: str | Path) -> dict:
    """Package only named source inputs; fail if any required input is missing."""
    root, output = Path(source_root).absolute(), Path(output_zip).absolute()
    _reject_links(root)
    _reject_links(output)
    if not root.is_dir():
        raise ReleaseError("source root must be a directory")
    if output.exists():
        raise FileExistsError("candidate archive already exists")
    if not output.parent.is_dir():
        raise ReleaseError("output parent directory must already exist")
    members = _python_members(root)
    mapping = {"pyproject.toml": "pyproject.toml", "README.md": "docs/release-user-guide.md",
               "seven-day-log.json": "docs/seven-day-log-template.json",
               "scripts/observe_round7.py": "scripts/observe_round7.py",
               "scripts/_offline.py": "scripts/_offline.py", "scripts/release_round7.py": "scripts/release_round7.py"}
    for destination, source in mapping.items():
        members[destination] = _read_source(root, source)
    members.update({"start_suwan.py": _LAUNCHER.encode("ascii"),
                    "start_gui.bat": _bat("gui"), "start_repl.bat": _bat("repl")})
    if not _REQUIRED <= members.keys():
        raise ReleaseError("required source entry point is missing")
    folded = set()
    for name in members:
        _safe_name(name)
        if not _allowed(name) or name.casefold() in folded:
            raise ReleaseError("duplicate, case-conflicting or disallowed source member")
        folded.add(name.casefold())
    _reject_file_parent_conflicts(folded)
    if len(members) + 1 > MAX_MEMBERS or sum(map(len, members.values())) > MAX_TOTAL_BYTES:
        raise ReleaseError("candidate exceeds archive size limits")
    manifest = {"format_version": 1, "candidate": CANDIDATE,
                "source_version": _version(members["pyproject.toml"]), "dependencies_bundled": False,
                "installation_claim": "current-machine-only",
                "files": [{"path": name, "bytes": len(payload), "sha256": _digest(payload)}
                          for name, payload in sorted(members.items())]}
    payload = json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
    if len(payload) > MAX_MANIFEST_BYTES:
        raise ReleaseError("manifest exceeds size limit")
    # Exclusive creation protects an existing candidate even if it appears
    # between the earlier existence check and opening the destination.
    with output.open("xb") as stream:
        try:
            with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
                for name, content in sorted({**members, "manifest.json": payload}.items()):
                    archive.writestr(_zip_info(name), content, compresslevel=9)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            # Leave the failed candidate visibly incomplete; never delete or
            # overwrite a path after another process could have replaced it.
            raise
    validate_release(output)
    return {"archive": str(output), "sha256": _digest(output.read_bytes()), "manifest": manifest}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReleaseError("duplicate manifest key")
        result[key] = value
    return result


def _read_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo, limit: int) -> bytes:
    with archive.open(info) as stream:
        payload = stream.read(limit + 1)
    if len(payload) > limit or len(payload) != info.file_size:
        raise ReleaseError("archive member exceeds or contradicts its declared size")
    return payload


def _validate_open(archive: zipfile.ZipFile) -> dict:
    infos = archive.infolist()
    if not infos or len(infos) > MAX_MEMBERS or archive.comment:
        raise ReleaseError("invalid archive member count or comment")
    names, folded, total = {}, set(), 0
    for info in infos:
        name = _safe_name(info.filename)
        if name.casefold() in folded:
            raise ReleaseError("duplicate or case-conflicting archive member")
        folded.add(name.casefold())
        if name != "manifest.json" and not _allowed(name):
            raise ReleaseError("disallowed archive member")
        mode = info.external_attr >> 16
        if stat.S_IFMT(mode) not in {0, stat.S_IFREG} or info.is_dir() or info.flag_bits & 1:
            raise ReleaseError("archive links, directories and encrypted members are not allowed")
        if info.extra or info.comment or info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
            raise ReleaseError("unsupported archive metadata")
        maximum = MAX_MANIFEST_BYTES if name == "manifest.json" else MAX_MEMBER_BYTES
        if info.file_size < 0 or info.file_size > maximum:
            raise ReleaseError("archive member exceeds size limit")
        total += info.file_size
        if total > MAX_TOTAL_BYTES + MAX_MANIFEST_BYTES:
            raise ReleaseError("archive exceeds total size limit")
        names[name] = info
    _reject_file_parent_conflicts(folded)
    if "manifest.json" not in names or not _REQUIRED <= names.keys():
        raise ReleaseError("required archive member is missing")
    try:
        manifest = json.loads(_read_member(archive, names["manifest.json"], MAX_MANIFEST_BYTES),
                              object_pairs_hook=_unique_object)
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ReleaseError("invalid manifest JSON") from exc
    if (type(manifest) is not dict or set(manifest) != _MANIFEST_KEYS
            or type(manifest["format_version"]) is not int or manifest["format_version"] != 1
            or manifest["candidate"] != CANDIDATE or manifest["dependencies_bundled"] is not False
            or manifest["installation_claim"] != "current-machine-only" or type(manifest["files"]) is not list):
        raise ReleaseError("unsupported source candidate manifest")
    records = {}
    for record in manifest["files"]:
        if (type(record) is not dict or set(record) != {"path", "bytes", "sha256"}
                or type(record["bytes"]) is not int or not 0 <= record["bytes"] <= MAX_MEMBER_BYTES
                or type(record["sha256"]) is not str or not re.fullmatch(r"[0-9a-f]{64}", record["sha256"])):
            raise ReleaseError("invalid manifest file record")
        name = _safe_name(record["path"])
        if name in records or name == "manifest.json" or not _allowed(name):
            raise ReleaseError("duplicate or disallowed manifest file record")
        records[name] = record
    if set(records) != set(names) - {"manifest.json"} or list(records) != sorted(records):
        raise ReleaseError("manifest and archive members do not match")
    for name, record in records.items():
        payload = _read_member(archive, names[name], MAX_MEMBER_BYTES)
        if len(payload) != record["bytes"] or _digest(payload) != record["sha256"]:
            raise ReleaseError(f"member size/SHA256 mismatch: {name}")
        if name == "pyproject.toml" and _version(payload) != manifest["source_version"]:
            raise ReleaseError("source version does not match pyproject.toml")
    return manifest


def validate_release(archive: str | Path) -> dict:
    """Verify bounded regular members and every hash before accepting a ZIP."""
    path = Path(archive).absolute()
    _reject_links(path)
    if path.stat().st_size > MAX_TOTAL_BYTES + 2 * MAX_MANIFEST_BYTES:
        raise ReleaseError("archive exceeds size limit")
    try:
        with zipfile.ZipFile(path) as opened:
            return _validate_open(opened)
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
        raise ReleaseError("invalid or unsupported ZIP archive") from exc


def extract_release(archive: str | Path, destination: str | Path) -> dict:
    """Verify the entire archive, then exclusively write to an empty directory."""
    path, target = Path(archive).absolute(), Path(destination).absolute()
    _reject_links(path)
    _reject_links(target)
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ReleaseError("extraction destination must be absent or an empty directory")
    if path.stat().st_size > MAX_TOTAL_BYTES + 2 * MAX_MANIFEST_BYTES:
        raise ReleaseError("archive exceeds size limit")
    try:
        with zipfile.ZipFile(path) as opened:
            manifest = _validate_open(opened)
            _reject_links(target)
            target.mkdir(parents=True, exist_ok=True)
            if any(target.iterdir()):
                raise ReleaseError("extraction destination changed or is not empty")
            for name in [*(record["path"] for record in manifest["files"]), "manifest.json"]:
                output = target.joinpath(*name.split("/"))
                _reject_links(output, stop=target)
                output.parent.mkdir(parents=True, exist_ok=True)
                _reject_links(output, stop=target)
                # Recheck the content copied from this still-open ZIP, so a
                # second archive at the same pathname cannot replace it.
                limit = MAX_MANIFEST_BYTES if name == "manifest.json" else MAX_MEMBER_BYTES
                payload = _read_member(opened, opened.getinfo(name), limit)
                if name != "manifest.json":
                    record = next(record for record in manifest["files"] if record["path"] == name)
                    if len(payload) != record["bytes"] or _digest(payload) != record["sha256"]:
                        raise ReleaseError("archive changed during extraction")
                with output.open("xb") as stream:
                    stream.write(payload)
            return manifest
    except (zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
        raise ReleaseError("invalid or unsupported ZIP archive") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build/check/extract a Suwan source candidate; dependencies are not bundled")
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--output", type=Path, help="Build a new candidate ZIP (never overwrite)")
    actions.add_argument("--verify", type=Path, help="Verify an existing source candidate")
    actions.add_argument("--extract", type=Path, help="Verify and extract a source candidate")
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args(argv)
    if args.extract is not None and args.destination is None:
        parser.error("--extract requires --destination")
    if args.extract is None and args.destination is not None:
        parser.error("--destination requires --extract")
    try:
        if args.output is not None:
            result = build_release(args.source, args.output)
        elif args.verify is not None:
            result = {"manifest": validate_release(args.verify)}
        else:
            result = {"manifest": extract_release(args.extract, args.destination)}
    except (OSError, ValueError) as exc:
        print(f"Source candidate failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
