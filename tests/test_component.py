"""Unit tests for KNXComponent — mocks context and xknx, tests sync paths."""
import json
from unittest.mock import MagicMock, patch

import pytest

from lucid_component_knx.component import KNXComponent


def make_context(**config):
    ctx = MagicMock()
    ctx.config = config if config else {}
    ctx.logger.return_value = MagicMock()
    ctx.topic.side_effect = lambda suffix: f"lucid/agents/test/components/knx/{suffix}"
    ctx.mqtt = MagicMock()
    return ctx


def make_component(**config):
    ctx = make_context(**config)
    return KNXComponent(ctx)


class TestInit:
    def test_component_id(self):
        c = make_component()
        assert c.component_id == "knx"

    def test_capabilities(self):
        c = make_component()
        caps = c.capabilities()
        assert "ping" in caps
        assert "reset" in caps
        assert "cfg/set" in caps
        assert "light/on" in caps
        assert "light/off" in caps
        assert "light/brightness/set" in caps

    def test_defaults(self):
        c = make_component()
        assert c._cfg["gateway_host"] == ""
        assert c._cfg["gateway_port"] == 3671
        assert c._cfg["lights"] == []

    def test_config_override(self):
        c = make_component(gateway_host="10.0.0.1", gateway_port=3672)
        assert c._cfg["gateway_host"] == "10.0.0.1"
        assert c._cfg["gateway_port"] == 3672

    def test_unknown_config_keys_ignored(self):
        c = make_component(unknown_key="value")
        assert "unknown_key" not in c._cfg


class TestStateAndCfg:
    def test_get_state_payload_initial(self):
        c = make_component()
        state = c.get_state_payload()
        assert state["connected"] is False
        assert state["lights"] == []

    def test_get_state_payload_with_lights(self):
        lights = [{"name": "Lab Light", "address": "1/0/1", "state_address": "1/0/2"}]
        c = make_component(lights=lights)
        c._light_state = {"Lab Light": {"name": "Lab Light", "on": None, "brightness": None}}
        state = c.get_state_payload()
        assert len(state["lights"]) == 1
        assert state["lights"][0]["name"] == "Lab Light"
        assert state["lights"][0]["on"] is None

    def test_get_cfg_payload(self):
        c = make_component(gateway_host="10.0.0.1")
        cfg = c.get_cfg_payload()
        assert cfg["gateway_host"] == "10.0.0.1"
        assert cfg["gateway_port"] == 3671
        assert "lights" in cfg


class TestPing:
    def test_ping_returns_ok(self):
        c = make_component()
        c.on_cmd_ping(json.dumps({"request_id": "r1"}))
        for call in c.context.mqtt.publish.call_args_list:
            topic = call[0][0]
            if "evt/ping/result" in topic:
                payload = json.loads(call[0][1])
                assert payload["ok"] is True
                assert payload["request_id"] == "r1"
                return
        pytest.fail("No result published to evt/ping/result")


class TestCfgSet:
    def test_cfg_set_valid_keys(self):
        c = make_component()
        c.on_cmd_cfg_set(json.dumps({
            "request_id": "r1",
            "set": {"gateway_host": "10.0.0.5", "gateway_port": 3672},
        }))
        assert c._cfg["gateway_host"] == "10.0.0.5"
        assert c._cfg["gateway_port"] == 3672
        for call in c.context.mqtt.publish.call_args_list:
            topic = call[0][0]
            if "evt/cfg/set/result" in topic:
                payload = json.loads(call[0][1])
                assert payload["ok"] is True
                return
        pytest.fail("No result published to evt/cfg/set/result")

    def test_cfg_set_unknown_key_rejected(self):
        c = make_component()
        c.on_cmd_cfg_set(json.dumps({
            "request_id": "r2",
            "set": {"nonexistent": "value"},
        }))
        for call in c.context.mqtt.publish.call_args_list:
            topic = call[0][0]
            if "evt/cfg/set/result" in topic:
                payload = json.loads(call[0][1])
                assert payload["ok"] is False
                assert "nonexistent" in payload["error"]
                return
        pytest.fail("No result published to evt/cfg/set/result")

    def test_cfg_set_lights_rejected(self):
        c = make_component()
        c.on_cmd_cfg_set(json.dumps({
            "request_id": "r3",
            "set": {"lights": []},
        }))
        for call in c.context.mqtt.publish.call_args_list:
            topic = call[0][0]
            if "evt/cfg/set/result" in topic:
                payload = json.loads(call[0][1])
                assert payload["ok"] is False
                assert "restart" in payload["error"]
                return
        pytest.fail("No result published to evt/cfg/set/result")

    def test_cfg_set_missing_set_field(self):
        c = make_component()
        c.on_cmd_cfg_set(json.dumps({"request_id": "r4"}))
        for call in c.context.mqtt.publish.call_args_list:
            topic = call[0][0]
            if "evt/cfg/set/result" in topic:
                payload = json.loads(call[0][1])
                assert payload["ok"] is False
                return
        pytest.fail("No result published to evt/cfg/set/result")


def make_component_with_light(name="Lab Light", has_brightness=True):
    """Return a component with a fake light device already in _devices."""
    c = make_component()
    mock_light = MagicMock()
    mock_light.name = name
    mock_light.supports_brightness = has_brightness
    c._devices[name] = mock_light
    return c, mock_light


class TestLightOn:
    def test_unknown_light_returns_error(self):
        c = make_component()
        c.on_cmd_light_on(json.dumps({"request_id": "r1", "light": "Ghost Light"}))
        for call in c.context.mqtt.publish.call_args_list:
            topic = call[0][0]
            if "evt/light/on/result" in topic:
                payload = json.loads(call[0][1])
                assert payload["ok"] is False
                assert "Ghost Light" in payload["error"]
                return
        pytest.fail("No result published")

    def test_loop_not_running_returns_error(self):
        c, _ = make_component_with_light()
        # _loop is None by default (component not started)
        c.on_cmd_light_on(json.dumps({"request_id": "r1", "light": "Lab Light"}))
        for call in c.context.mqtt.publish.call_args_list:
            topic = call[0][0]
            if "evt/light/on/result" in topic:
                payload = json.loads(call[0][1])
                assert payload["ok"] is False
                assert "not running" in payload["error"]
                return
        pytest.fail("No result published")

    def test_success_calls_set_on(self):
        c, mock_light = make_component_with_light()
        with patch.object(c, "_run_async", return_value=None) as mock_run:
            c.on_cmd_light_on(json.dumps({"request_id": "r2", "light": "Lab Light"}))
            mock_run.assert_called_once()
        for call in c.context.mqtt.publish.call_args_list:
            topic = call[0][0]
            if "evt/light/on/result" in topic:
                payload = json.loads(call[0][1])
                assert payload["ok"] is True
                assert payload["request_id"] == "r2"
                return
        pytest.fail("No result published")


class TestLightOff:
    def test_unknown_light_returns_error(self):
        c = make_component()
        c.on_cmd_light_off(json.dumps({"request_id": "r1", "light": "Ghost"}))
        for call in c.context.mqtt.publish.call_args_list:
            topic = call[0][0]
            if "evt/light/off/result" in topic:
                payload = json.loads(call[0][1])
                assert payload["ok"] is False
                return
        pytest.fail("No result published")

    def test_success(self):
        c, mock_light = make_component_with_light()
        with patch.object(c, "_run_async", return_value=None):
            c.on_cmd_light_off(json.dumps({"request_id": "r2", "light": "Lab Light"}))
        for call in c.context.mqtt.publish.call_args_list:
            topic = call[0][0]
            if "evt/light/off/result" in topic:
                payload = json.loads(call[0][1])
                assert payload["ok"] is True
                return
        pytest.fail("No result published")


class TestLightBrightnessSet:
    def test_unknown_light_returns_error(self):
        c = make_component()
        c.on_cmd_light_brightness_set(json.dumps(
            {"request_id": "r1", "light": "Ghost", "brightness": 200}
        ))
        for call in c.context.mqtt.publish.call_args_list:
            topic = call[0][0]
            if "evt/light/brightness/set/result" in topic:
                payload = json.loads(call[0][1])
                assert payload["ok"] is False
                return
        pytest.fail("No result published")

    def test_no_brightness_address_returns_error(self):
        c, _ = make_component_with_light(has_brightness=False)
        c.on_cmd_light_brightness_set(json.dumps(
            {"request_id": "r2", "light": "Lab Light", "brightness": 200}
        ))
        for call in c.context.mqtt.publish.call_args_list:
            topic = call[0][0]
            if "evt/light/brightness/set/result" in topic:
                payload = json.loads(call[0][1])
                assert payload["ok"] is False
                assert "brightness address" in payload["error"]
                return
        pytest.fail("No result published")

    def test_missing_brightness_field_returns_error(self):
        c, _ = make_component_with_light()
        c.on_cmd_light_brightness_set(json.dumps(
            {"request_id": "r3", "light": "Lab Light"}
        ))
        for call in c.context.mqtt.publish.call_args_list:
            topic = call[0][0]
            if "evt/light/brightness/set/result" in topic:
                payload = json.loads(call[0][1])
                assert payload["ok"] is False
                assert "brightness" in payload["error"]
                return
        pytest.fail("No result published")

    def test_success(self):
        c, mock_light = make_component_with_light()
        with patch.object(c, "_run_async", return_value=None):
            c.on_cmd_light_brightness_set(json.dumps(
                {"request_id": "r4", "light": "Lab Light", "brightness": 200}
            ))
        for call in c.context.mqtt.publish.call_args_list:
            topic = call[0][0]
            if "evt/light/brightness/set/result" in topic:
                payload = json.loads(call[0][1])
                assert payload["ok"] is True
                return
        pytest.fail("No result published")

    def test_brightness_clamped_high(self):
        c, mock_light = make_component_with_light()
        with patch.object(c, "_run_async", return_value=None):
            c.on_cmd_light_brightness_set(json.dumps(
                {"request_id": "r5", "light": "Lab Light", "brightness": 999}
            ))
        mock_light.set_brightness.assert_called_once_with(255)

    def test_brightness_clamped_low(self):
        c, mock_light = make_component_with_light()
        with patch.object(c, "_run_async", return_value=None):
            c.on_cmd_light_brightness_set(json.dumps(
                {"request_id": "r6", "light": "Lab Light", "brightness": -50}
            ))
        mock_light.set_brightness.assert_called_once_with(0)
