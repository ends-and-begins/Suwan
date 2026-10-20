"""Source-candidate privacy, integrity and explicit-home launcher boundaries."""
from __future__ import annotations

import tests

import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import zipfile

from scripts import release_round7 as release


class SourceCandidateTest(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory(prefix="suwan-round7-release-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "source"
        files = {
            "pyproject.toml": '[project]\nname="suwan-v2"\nversion="0.1.0"\n',
            "src/suwan/__init__.py": '__version__ = "0.1.0"\n',
            "src/suwan/config.py": "# offline fixture config\n",
            "src/suwan/app.py": 'class SuwanRuntime:\n    def __init__(self, *values, **options):\n        self.options = options\n',
            "src/suwan/frontends/__init__.py": "",
            "src/suwan/assets/nested/__init__.py": "# package member\n",
            "src/suwan/frontends/repl.py": (
                "from suwan.app import SuwanRuntime\nimport json, os\n"
                "async def amain(argv=None):\n    runtime = SuwanRuntime(object())\n"
                "    print(json.dumps({'args': argv, 'options': runtime.options, 'skip_dotenv': os.environ.get('SUWAN_SKIP_DOTENV'), 'module': __file__}))\n"
                "    return 0\n"),
            "src/suwan/frontends/tkapp.py": (
                "from suwan.app import SuwanRuntime\nimport json, os\n"
                "def main(argv=None):\n    runtime = SuwanRuntime(object())\n"
                "    print(json.dumps({'args': argv, 'options': runtime.options, 'skip_dotenv': os.environ.get('SUWAN_SKIP_DOTENV')}))\n"
                "    return 0\n"),
            "docs/release-user-guide.md": "# Source candidate\nDependencies are not bundled.\n",
            "docs/seven-day-log-template.json": '{"days": [], "status": "NOT_RUN"}',
            "scripts/observe_round7.py": "# observation fixture\n",
            "scripts/_offline.py": "def install_offline_guard():\n    return {}\n",
            "scripts/release_round7.py": "# validator fixture\n",
        }
        for name, content in files.items():
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        self.archive = self.root / "candidate.zip"

    def build(self):
        return release.build_release(self.source, self.archive)

    def repack(self, transform=None, extra=None):
        output = self.root / "modified.zip"
        with zipfile.ZipFile(self.archive) as source, zipfile.ZipFile(output, "w") as destination:
            for info in source.infolist():
                content = source.read(info)
                if transform is not None:
                    content = transform(info.filename, content)
                destination.writestr(info, content)
            if extra is not None:
                destination.writestr(*extra)
        return output

    def link_directory(self, target, link):
        try:
            os.symlink(target, link, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                raise
            result = subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(release._is_link(link))
        def unlink():
            if link.is_symlink():
                link.unlink()
            elif link.is_junction():
                link.rmdir()
        self.addCleanup(unlink)

    def test_manifest_is_complete_hashed_and_independent_of_source_absolute_path(self):
        result = self.build()
        self.assertEqual(result["archive"], str(self.archive.absolute()))
        self.assertEqual(result["sha256"], hashlib.sha256(self.archive.read_bytes()).hexdigest())
        manifest = release.validate_release(self.archive)
        self.assertEqual(manifest, result["manifest"])
        self.assertEqual(manifest["candidate"], "round7-source-candidate")
        self.assertEqual(manifest["source_version"], "0.1.0")
        self.assertIs(manifest["dependencies_bundled"], False)
        self.assertNotIn(str(self.source), json.dumps(manifest))
        with zipfile.ZipFile(self.archive) as archive:
            self.assertEqual(set(archive.namelist()), {"manifest.json", *(row["path"] for row in manifest["files"])})
            for row in manifest["files"]:
                payload = archive.read(row["path"])
                self.assertEqual(row["bytes"], len(payload))
                self.assertEqual(row["sha256"], hashlib.sha256(payload).hexdigest())
                self.assertEqual(archive.getinfo(row["path"]).date_time, release.FIXED_TIME)
            self.assertIn(b"Source candidate", archive.read("README.md"))
            for name in ("start_suwan.py", "start_gui.bat", "start_repl.bat"):
                archive.read(name).decode("ascii")

    def test_env_private_data_unknown_roots_and_cache_are_never_packaged(self):
        secret = "ROUND7_FAKE_PRIVATE_CANARY"
        # The test guard forbids opening dotenv files even for writing.  Rename
        # a harmless fixture into place, then ensure the builder never opens it.
        staging = self.source / "canary-to-rename"
        staging.write_text(secret, encoding="utf-8")
        staging.rename(self.source / ".env")
        paths = ("private-data/history.json", "data/memory.json", "backups/old.py", "research/probe.py",
                 "docs/internal-report.md", ".git/config", "src/suwan/__pycache__/canary.py",
                 "src/suwan/assets/private.txt")
        for name in paths:
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(secret, encoding="utf-8")
        self.build()
        with zipfile.ZipFile(self.archive) as archive:
            self.assertFalse(any(".env" in name or "__pycache__" in name for name in archive.namelist()))
            self.assertNotIn(secret, "\n".join(archive.read(name).decode("utf-8") for name in archive.namelist()))

    def test_build_is_deterministic_and_never_overwrites_an_existing_candidate(self):
        self.build()
        second = self.root / "second.zip"
        release.build_release(self.source, second)
        first_bytes = self.archive.read_bytes()
        self.assertEqual(first_bytes, second.read_bytes())
        with self.assertRaises(FileExistsError):
            release.build_release(self.source, self.archive)
        self.assertEqual(self.archive.read_bytes(), first_bytes)

    def test_missing_required_guide_or_observer_is_not_silently_ignored(self):
        for name in ("docs/release-user-guide.md", "scripts/observe_round7.py"):
            with self.subTest(name=name):
                path = self.source / name
                original = path.read_bytes()
                path.unlink()
                try:
                    with self.assertRaisesRegex(release.ReleaseError, "missing"):
                        self.build()
                    self.assertFalse(self.archive.exists())
                finally:
                    path.write_bytes(original)

    def test_allowed_source_file_link_is_rejected_before_open(self):
        real = Path.is_symlink
        victim = self.source / "src/suwan/__init__.py"
        def fake(path):
            return path == victim or real(path)
        with patch.object(Path, "is_symlink", fake):
            with self.assertRaisesRegex(release.ReleaseError, "links"):
                self.build()
        self.assertFalse(self.archive.exists())

    def test_actual_source_directory_symlink_or_junction_is_rejected(self):
        target = self.root / "outside-package"
        target.mkdir()
        (target / "private.py").write_text("# never included\n", encoding="utf-8")
        self.link_directory(target, self.source / "src/suwan/linked")
        with self.assertRaisesRegex(release.ReleaseError, "links"):
            self.build()
        self.assertFalse(self.archive.exists())

    def test_modified_source_member_is_rejected_by_sha_and_not_extracted(self):
        self.build()
        def tamper(name, payload):
            if name == "src/suwan/config.py":
                return bytes([payload[0] ^ 1]) + payload[1:]
            return payload
        bad = self.repack(tamper)
        with self.assertRaisesRegex(release.ReleaseError, "SHA256"):
            release.validate_release(bad)
        destination = self.root / "should-not-exist"
        with self.assertRaises(release.ReleaseError):
            release.extract_release(bad, destination)
        self.assertFalse(destination.exists())

    def test_unsafe_unknown_and_duplicate_archive_members_are_rejected(self):
        self.build()
        cases = ("../escape.txt", "/absolute.py", "C:/escape.py", "src\\suwan\\escape.py", ".env",
                 "private-data/history.json", "src/suwan/CON.py", "src/suwan/app.py", "src/suwan/APP.py")
        for name in cases:
            with self.subTest(name=name):
                with patch("warnings.warn"):
                    bad = self.repack(extra=(name, b"do not extract"))
                with self.assertRaises(release.ReleaseError):
                    release.validate_release(bad)
                with self.assertRaises(release.ReleaseError):
                    release.extract_release(bad, self.root / "unsafe-destination")
                self.assertFalse((self.root / "unsafe-destination").exists())
                self.assertFalse((self.root / "escape.txt").exists())

    def test_archive_symlink_mode_is_rejected(self):
        self.build()
        info = zipfile.ZipInfo("src/suwan/linked.py")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        bad = self.repack(extra=(info, b"../../private"))
        with self.assertRaisesRegex(release.ReleaseError, "links"):
            release.validate_release(bad)

    def test_real_candidate_rejects_manifest_valid_file_parent_conflicts_before_extraction(self):
        repository = Path(__file__).resolve().parents[1]
        release.build_release(repository, self.archive)
        with zipfile.ZipFile(self.archive) as source:
            original = {name: source.read(name) for name in source.namelist()}
        # Both members are otherwise allowlisted and have correct manifest
        # records.  Check both ZIP orders and Windows case-insensitive parents.
        for index, name in enumerate(("src/suwan/app.py/extra.py", "src/suwan/APP.py/extra.py")):
            with self.subTest(path=name):
                members = dict(original)
                payload = b"# synthetic conflict regression\n"
                members[name] = payload
                manifest = json.loads(members["manifest.json"])
                manifest["files"].append({"path": name, "bytes": len(payload),
                                          "sha256": hashlib.sha256(payload).hexdigest()})
                manifest["files"].sort(key=lambda record: record["path"])
                members["manifest.json"] = json.dumps(manifest).encode("utf-8")
                bad = self.root / f"prefix-conflict-{index}.zip"
                with zipfile.ZipFile(bad, "w") as destination:
                    for path, content in sorted(members.items(), reverse=bool(index)):
                        destination.writestr(release._zip_info(path), content)
                with self.assertRaisesRegex(release.ReleaseError, "parent directory"):
                    release.validate_release(bad)
                target = self.root / f"no-partial-extraction-{index}"
                with self.assertRaisesRegex(release.ReleaseError, "parent directory"):
                    release.extract_release(bad, target)
                self.assertFalse(target.exists())

    def test_manifest_missing_extra_record_and_duplicate_key_are_rejected(self):
        self.build()
        for change in ("missing", "extra", "duplicate-key"):
            with self.subTest(change=change):
                def tamper(name, payload):
                    if name != "manifest.json":
                        return payload
                    manifest = json.loads(payload)
                    if change == "missing":
                        manifest["files"].pop()
                    elif change == "extra":
                        manifest["files"].append({"path": "src/suwan/extra.py", "bytes": 0, "sha256": "0" * 64})
                    else:
                        return b'{"format_version":1,' + payload[1:]
                    return json.dumps(manifest).encode()
                bad = self.repack(tamper)
                with self.assertRaises(release.ReleaseError):
                    release.validate_release(bad)

    def test_member_size_limit_is_checked_before_reading_compressed_payload(self):
        self.build()
        with patch.object(release, "MAX_MEMBER_BYTES", 10):
            with self.assertRaisesRegex(release.ReleaseError, "size limit"):
                release.validate_release(self.archive)

    def test_safe_extraction_preserves_bytes_and_rejects_nonempty_destination(self):
        result = self.build()
        target = self.root / "extract"
        manifest = release.extract_release(self.archive, target)
        self.assertEqual(manifest, result["manifest"])
        for row in manifest["files"]:
            self.assertEqual(hashlib.sha256((target / row["path"]).read_bytes()).hexdigest(), row["sha256"])
        marker = target / "do-not-overwrite"
        marker.write_text("keep", encoding="utf-8")
        with self.assertRaisesRegex(release.ReleaseError, "empty"):
            release.extract_release(self.archive, target)
        self.assertEqual(marker.read_text(encoding="utf-8"), "keep")

    def test_extraction_rejects_destination_junction_without_writing_through_it(self):
        self.build()
        private = self.root / "private-empty"
        private.mkdir()
        link = self.root / "linked-destination"
        self.link_directory(private, link)
        with self.assertRaisesRegex(release.ReleaseError, "links"):
            release.extract_release(self.archive, link)
        self.assertFalse(any(private.iterdir()))

    def test_launcher_requires_home_prefers_its_src_and_skips_dotenv_in_normal_mode(self):
        self.build()
        extracted = self.root / "launch"
        release.extract_release(self.archive, extracted)
        missing = subprocess.run([sys.executable, str(extracted / "start_suwan.py"), "--frontend", "repl"],
                                 cwd=self.root, capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(missing.returncode, 2)
        self.assertIn("--home", missing.stderr)
        home = self.root / "explicit-home"
        process = subprocess.run([sys.executable, str(extracted / "start_suwan.py"), "--frontend", "repl",
                                  "--home", str(home), "--smoke", "synthetic"],
                                 cwd=self.root, capture_output=True, text=True, encoding="utf-8", timeout=15)
        self.assertEqual(process.returncode, 0, process.stderr)
        output = json.loads(process.stdout)
        self.assertEqual(output["args"], ["--home", str(home), "--smoke", "synthetic"])
        self.assertEqual(output["skip_dotenv"], "1")
        self.assertEqual(output["options"], {})
        self.assertTrue(Path(output["module"]).is_relative_to(extracted))

    def test_launcher_offline_constructor_overrides_both_selected_frontends_only(self):
        self.build()
        extracted = self.root / "launch-offline"
        release.extract_release(self.archive, extracted)
        for frontend in ("gui", "repl"):
            with self.subTest(frontend=frontend):
                process = subprocess.run([sys.executable, str(extracted / "start_suwan.py"), "--frontend", frontend,
                                          "--home", str(self.root / "home"), "--offline"],
                                         cwd=self.root, capture_output=True, text=True, encoding="utf-8", timeout=15)
                self.assertEqual(process.returncode, 0, process.stderr)
                self.assertEqual(json.loads(process.stdout)["options"], {"api_enabled": False, "network_enabled": False})

    def test_real_offline_launcher_overrides_saved_api_and_network_on_without_a_network_attempt(self):
        from suwan.app import SuwanRuntime
        from suwan.clock import ManualClock
        from suwan.config import Settings
        home = self.root / "saved-on-home"
        async def prepare():
            runtime = SuwanRuntime(Settings.from_env(env={}, load_env=False, home_dir=home),
                                   clock=ManualClock(), migrate_legacy_data=False)
            await runtime.start()
            try:
                result = await runtime.update_desktop_settings({"api_enabled": True, "network_enabled": True})
                self.assertTrue(result["ok"])
            finally:
                await runtime.stop()
        asyncio.run(prepare())
        repository = Path(__file__).resolve().parents[1]
        # Only source Python and the guard are needed here; the frozen workflow
        # suite separately launches the final archive built from the real tree.
        for source in (repository / "src/suwan").rglob("*.py"):
            if "__pycache__" in source.parts:
                continue
            destination = self.source / source.relative_to(repository)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read_bytes())
        shutil.copyfile(repository / "scripts/_offline.py", self.source / "scripts/_offline.py")
        self.build()
        extracted = self.root / "real-offline"
        release.extract_release(self.archive, extracted)
        wrapper = ("import atexit,json,runpy,sys; p=sys.argv.pop(1); sys.argv[0]=p; "
                   "atexit.register(lambda: print('OFFLINE_BLOCKS='+str(getattr(sys,'_suwan_offline_guard',{}).get('network_blocks','missing')))); "
                   "runpy.run_path(p,run_name='__main__')")
        process = subprocess.run([sys.executable, "-X", "utf8", "-c", wrapper, str(extracted / "start_suwan.py"),
                                  "--frontend", "repl", "--home", str(home), "--offline"],
                                 input="/cfg\n/quit\n", cwd=self.root, capture_output=True, text=True,
                                 encoding="utf-8", timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("API=False 联网=False", process.stdout)
        self.assertIn("OFFLINE_BLOCKS=0", process.stdout)

    def test_real_candidate_offline_launcher_ignores_unrelated_regular_scripts_package(self):
        repository = Path(__file__).resolve().parents[1]
        release.build_release(repository, self.archive)
        extracted = self.root / "real-collision-candidate"
        release.extract_release(self.archive, extracted)
        unrelated = self.root / "synthetic-site-packages" / "scripts"
        unrelated.mkdir(parents=True)
        (unrelated / "__init__.py").write_text("# unrelated regular package\n", encoding="utf-8")
        self.assertFalse((extracted / "scripts/__init__.py").exists())
        env = dict(os.environ)
        env.update(PYTHONPATH=str(unrelated.parent), PYTHONDONTWRITEBYTECODE="1")
        wrapper = (
            "import atexit,runpy,sys; p=sys.argv.pop(1); sys.argv[0]=p; "
            "atexit.register(lambda: print('SCRIPTS_ORIGIN='+str(getattr(sys.modules.get('scripts'),'__file__',None)))); "
            "atexit.register(lambda: print('OFFLINE_BLOCKS='+str(getattr(sys,'_suwan_offline_guard',{}).get('network_blocks','missing')))); "
            "runpy.run_path(p,run_name='__main__')")
        process = subprocess.run(
            [sys.executable, "-B", "-X", "utf8", "-c", wrapper, str(extracted / "start_suwan.py"),
             "--frontend", "repl", "--home", str(self.root / "collision-home"), "--offline"],
            input="/cfg\n/quit\n", env=env, cwd=self.root, capture_output=True,
            text=True, encoding="utf-8", timeout=30)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertIn("API=False 联网=False", process.stdout)
        self.assertIn("OFFLINE_BLOCKS=0", process.stdout)
        self.assertIn("SCRIPTS_ORIGIN=None", process.stdout)

    def test_cli_build_verify_and_extract_use_the_same_validation(self):
        with patch("sys.stdout", new_callable=io.StringIO) as output:
            self.assertEqual(release.main(["--source", str(self.source), "--output", str(self.archive)]), 0)
            summary = json.loads(output.getvalue())
        self.assertEqual(summary["archive"], str(self.archive))
        with patch("sys.stdout", new_callable=io.StringIO):
            self.assertEqual(release.main(["--verify", str(self.archive)]), 0)
            self.assertEqual(release.main(["--extract", str(self.archive), "--destination", str(self.root / "cli-extracted")]), 0)


if __name__ == "__main__":
    unittest.main()
