"""REPL settings, snapshot export, and restart use the desktop runtime API."""
import tests

import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from suwan.app import SuwanRuntime
from suwan.config import Settings
from suwan.frontends.repl import amain, desktop_command


class DesktopReplTest(unittest.IsolatedAsyncioTestCase):
    async def test_public_commands_persist_and_export_without_credentials(self):
        with tempfile.TemporaryDirectory(prefix="suwan-v6-repl-") as folder:
            settings = Settings.from_env(env={}, load_env=False, home_dir=Path(folder))
            settings.api_keys["mimo"] = "DO_NOT_EXPORT_PROVIDER_CREDENTIAL"
            runtime = SuwanRuntime(settings, api_enabled=False, migrate_legacy_data=False)
            await runtime.start()
            try:
                self.assertIn("保存", await desktop_command(runtime, '/desk set quiet_start 21'))
                self.assertIn("保存", await desktop_command(runtime, '/desk set model "local-review-model"'))
                source = runtime.organs["memory"].add("桌面验收：周末去合成展馆。", source="manual", persist=True)
                found = await desktop_command(runtime, "/desk find 合成展馆")
                self.assertIn(source.id, found)
                self.assertIn("合成展馆", found)
                view = await runtime.desktop_view()
                self.assertEqual(view["settings"]["quiet_start"], 21)
                exported = await runtime.export_data()
                self.assertTrue(exported["ok"], exported)
                text = Path(exported["file"]).read_text(encoding="utf-8")
                self.assertIn("合成展馆", text)
                self.assertNotIn("DO_NOT_EXPORT_PROVIDER_CREDENTIAL", text)
                json.loads(text)
            finally:
                await runtime.stop()
            restored = SuwanRuntime(settings, migrate_legacy_data=False)
            await restored.start()
            try:
                view = await restored.desktop_view()
                self.assertEqual(view["settings"]["quiet_start"], 21)
                self.assertEqual(restored.llm.model, "local-review-model")
                self.assertFalse(restored.api_enabled)
            finally:
                await restored.stop()

    async def test_invalid_switch_commands_do_not_silently_toggle_settings(self):
        with tempfile.TemporaryDirectory(prefix="suwan-v6-repl-input-") as folder:
            settings = Settings.from_env(env={}, load_env=False, home_dir=Path(folder))
            output = io.StringIO()
            commands = ["/api maybe", "/net maybe", "/desk set quiet_start 21", "/desk settings", "/quit"]
            with patch("suwan.frontends.repl._build_settings", return_value=settings), \
                 patch("builtins.input", side_effect=commands), contextlib.redirect_stdout(output):
                self.assertEqual(await amain([]), 0)
            text = output.getvalue()
            self.assertIn("用法：/api on|off", text)
            self.assertIn("用法：/net on|off", text)
            restored = SuwanRuntime(settings, migrate_legacy_data=False)
            await restored.start()
            try:
                view = await restored.desktop_view()
                self.assertEqual(view["settings"]["quiet_start"], 21)
                self.assertFalse(restored.api_enabled)
                self.assertFalse(restored.network_enabled)
            finally:
                await restored.stop()

    async def test_oversize_review_query_reports_error_and_repl_continues(self):
        with tempfile.TemporaryDirectory(prefix="suwan-v6-repl-query-") as folder:
            settings = Settings.from_env(env={}, load_env=False, home_dir=Path(folder))
            output = io.StringIO()
            commands = ["/desk find " + "字" * 201, "/desk settings", "/quit"]
            with patch("suwan.frontends.repl._build_settings", return_value=settings), \
                 patch("builtins.input", side_effect=commands), contextlib.redirect_stdout(output):
                self.assertEqual(await amain([]), 0)
            self.assertIn("桌面操作未完成", output.getvalue())
            self.assertIn('"quiet_start"', output.getvalue())


if __name__ == "__main__":
    unittest.main()
