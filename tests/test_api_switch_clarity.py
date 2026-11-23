"""The GUI distinguishes permission to call a model from probe results."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests import test_round6_surface as helpers


class ApiSwitchClarityTest(unittest.TestCase):
    setUp = helpers.DesktopTkBoundaryTest.setUp
    close_app = helpers.DesktopTkBoundaryTest.close_app
    restore_gc = helpers.DesktopTkBoundaryTest.restore_gc
    pump_until = helpers.DesktopTkBoundaryTest.pump_until
    ready = helpers.DesktopTkBoundaryTest.ready

    def test_testing_with_api_off_explains_the_switch_and_makes_no_request(self):
        self.ready()
        self.assertFalse(self.runtime.api_enabled)
        self.assertFalse(self.app.api_checkbox.instate(["selected"]))
        with patch.object(self.app.server, "api_check") as probe:
            self.app.api_test_button.invoke()
            self.app._pump()
        probe.assert_not_called()
        self.assertFalse(self.runtime.api_enabled)
        status = str(self.app.api_state_lbl.cget("text"))
        self.assertIn("API 未开启", status)
        self.assertIn(str(self.app.api_checkbox.cget("text")), status)
        self.assertNotIn("连接失败", status)

    def test_checkbox_saves_and_explicit_probe_works_with_web_search_off(self):
        self.ready()
        create = Mock(return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="OK"))]))

        async def install_model():
            self.runtime.llm._keyless = True
            self.runtime.llm._client = SimpleNamespace(
                chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

        self.app.server._submit(install_model()).result(timeout=3)
        self.app.api_checkbox.invoke()
        self.pump_until(lambda: self.runtime.api_enabled and not self.app._settings_pending)
        self.assertTrue(self.app.api_checkbox.instate(["selected"]))
        self.assertFalse(self.runtime.network_enabled)
        create.assert_not_called()
        self.app.api_test_button.invoke()
        self.pump_until(lambda: "连接成功" in str(self.app.api_state_lbl.cget("text")))
        create.assert_called_once()
        state = self.app.server.desktop_view().result(timeout=3)
        self.assertTrue(state["settings"]["api_enabled"])
        self.assertFalse(state["settings"]["network_enabled"])
