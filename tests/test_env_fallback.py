"""Synthetic, offline checks for dotenv loading without an installed package."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from scripts._offline import isolated_environment
from suwan import _dotenv, config


class BuiltinDotenvTest(unittest.TestCase):
    def test_bom_export_quotes_comments_empty_and_bare_keys(self):
        text = "\ufeff# comment\nexport FIRST = plain # note\nSECOND='two # three'\nTHIRD=\"four # five\" # note\nHASH=keep#hash\nEMPTY=\nBARE\n"
        self.assertEqual(_dotenv.parse_environment(text, {}, override=True), {
            "FIRST": "plain", "SECOND": "two # three", "THIRD": "four # five",
            "HASH": "keep#hash", "EMPTY": "",
        })

    def test_escapes_preserve_windows_paths_and_do_not_execute_shell_syntax(self):
        text = r'''PATH='C:\Users\fixture'
ESCAPES="line\nnext\t\"quote\""
COMMAND=$(never_execute_this)
BACKTICKS=`never_execute_this`
'''
        values = _dotenv.parse_environment(text, {}, override=True)
        self.assertEqual(values["PATH"], r"C:\Users\fixture")
        self.assertEqual(values["ESCAPES"], 'line\nnext\t"quote"')
        self.assertEqual(values["COMMAND"], "$(never_execute_this)")
        self.assertEqual(values["BACKTICKS"], "`never_execute_this`")

    def test_expansion_obeys_file_override_and_is_one_pass(self):
        text = "BASE=file\nVALUE=${BASE}/${OUTER}/${MISSING:-fallback}\nLITERAL='${BASE}'\n"
        env = {"BASE": "inherited", "OUTER": "${BASE}"}
        values = _dotenv.parse_environment(text, env, override=True)
        self.assertEqual(values["VALUE"], "file/${BASE}/fallback")
        self.assertEqual(values["LITERAL"], "${BASE}")
        values = _dotenv.parse_environment(text, env, override=False)
        self.assertEqual(values["VALUE"], "inherited/${BASE}/fallback")

    def test_duplicate_assignments_and_unknown_variables(self):
        values = _dotenv.parse_environment("A=one\nB=${A}\nA=two\nC=${A}-${UNKNOWN}\n", {}, override=True)
        self.assertEqual(values, {"A": "two", "B": "one", "C": "two-"})

    def test_invalid_syntax_does_not_commit_earlier_values_or_reveal_contents(self):
        canary = "synthetic-secret-never-print"
        for malformed in (f'BROKEN="{canary}', f'BAD KEY={canary}', f'BROKEN="{canary}" suffix', f'BROKEN={canary}\0'):
            with self.subTest(malformed=malformed), tempfile.TemporaryDirectory() as temporary:
                fixture = Path(temporary) / "controlled.fixture"
                fixture.write_text("FIRST=changed\n" + malformed, encoding="utf-8")
                with patch.dict(os.environ, {"FIRST": "original"}, clear=True):
                    with self.assertRaises(ValueError) as error:
                        _dotenv.load_dotenv(fixture, override=True)
                    self.assertEqual(os.environ["FIRST"], "original")
                    self.assertNotIn(canary, str(error.exception))
                    self.assertIn("2", str(error.exception))

    def test_file_and_expansion_limits_reject_without_secret_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary) / "controlled.fixture"
            fixture.write_bytes(b"A=" + b"x" * _dotenv.MAX_FILE_BYTES)
            with self.assertRaisesRegex(ValueError, "256 KiB"):
                _dotenv.load_dotenv(fixture)
        with self.assertRaises(ValueError):
            _dotenv.parse_environment("A=${LARGE}${LARGE}", {"LARGE": "x" * _dotenv.MAX_VALUE_CHARS}, override=True)

    def test_invalid_utf8_error_is_generic(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary) / "controlled.fixture"
            fixture.write_bytes(b"TOKEN=canary\xff")
            with self.assertRaisesRegex(ValueError, "UTF-8") as error:
                _dotenv.load_dotenv(fixture)
            self.assertNotIn("canary", str(error.exception))

    def test_override_false_preserves_process_values(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary) / "controlled.fixture"
            fixture.write_text("EXISTING=file\nNEW=${EXISTING}\n", encoding="utf-8")
            with patch.dict(os.environ, {"EXISTING": "process"}, clear=True):
                self.assertTrue(_dotenv.load_dotenv(fixture, override=False))
                self.assertEqual(os.environ["EXISTING"], "process")
                self.assertEqual(os.environ["NEW"], "process")


class EnvironmentAuthorityTest(unittest.TestCase):
    def test_selected_synthetic_file_is_loaded_without_sdk_and_overrides_parent(self):
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary) / "controlled.fixture"
            fixture.write_text("DEEPSEEK_API_KEY=synthetic-file-value\nMODEL_DEEPSEEK=fixture-model\n", encoding="utf-8-sig")
            with patch.dict(os.environ, {"SUWAN_SKIP_DOTENV": "0", "DEEPSEEK_API_KEY": "parent-value"}, clear=True):
                with patch.object(config, "load_dotenv", _dotenv.load_dotenv):
                    settings = config.Settings.from_env(env_file=fixture, home_dir=Path(temporary))
                self.assertEqual(settings.api_keys["deepseek"], "synthetic-file-value")
                self.assertEqual(settings.model_names["deepseek"], "fixture-model")

    def test_existing_candidate_precedence_and_root_authority_do_not_depend_on_cwd(self):
        project_env = Path(config.__file__).resolve().parents[2] / ".env"
        explicit = Path("explicit.fixture")
        override = Path("override.fixture")
        for selected, existing, expected in (
            (explicit, {explicit, override, project_env}, explicit),
            (explicit, {override, project_env}, override),
            (None, {project_env}, project_env),
        ):
            loader = Mock(return_value=True)
            with self.subTest(expected=str(expected)), patch.dict(os.environ, {"SUWAN_SKIP_DOTENV": "0", "SUWAN_ENV_FILE": str(override)}, clear=True):
                with patch.object(config, "load_dotenv", loader), patch.object(Path, "exists", lambda path: path in existing):
                    self.assertTrue(config.load_environment(selected))
            loader.assert_called_once_with(expected, override=True)

    def test_offline_skip_prevents_both_file_checks_and_reader(self):
        with patch.dict(os.environ, {"SUWAN_SKIP_DOTENV": "1"}, clear=True):
            with patch.object(Path, "exists", side_effect=AssertionError("must not inspect file")):
                with patch.object(config, "load_dotenv", side_effect=AssertionError("must not read")):
                    self.assertFalse(config.load_environment("unused.fixture"))
        with patch.object(config, "load_environment", side_effect=AssertionError("must not load")):
            settings = config.Settings.from_env(env={}, load_env=False)
            self.assertFalse(any(settings.api_keys.values()))

    def test_missing_dotenv_import_selects_builtin_loader_without_reading_configuration(self):
        root = Path(__file__).resolve().parents[1]
        code = """
import sys
sys.path[:0] = [sys.argv[1], sys.argv[1] + '/src']
import tests
sys.modules['dotenv'] = None
from suwan import config
from suwan._dotenv import load_dotenv
assert config.load_dotenv is load_dotenv
assert config.load_environment('unused.fixture') is False
print('builtin loader selected; offline skip retained')
"""
        proc = subprocess.run([sys.executable, "-B", "-X", "utf8", "-c", code, str(root)], cwd=root,
                              env=isolated_environment(root / "unused-test-home"), capture_output=True,
                              text=True, encoding="utf-8", timeout=15)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("offline skip retained", proc.stdout)
