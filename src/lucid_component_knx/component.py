"""KNX IP lighting component — controls KNX lights via xknx IP tunneling."""
from __future__ import annotations

import asyncio
import copy
import json
import threading
import time
from datetime import datetime, timezone
from typing import Any, Optional

from lucid_component_base import Component, ComponentContext


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class KNXComponent(Component):
    """KNX lighting controller via xknx IP gateway tunneling."""

    _MONITOR_INTERVAL_S = 5.0

    _DEFAULTS: dict[str, Any] = {
        "gateway_host": "",
        "gateway_port": 3671,
        "lights": [],
    }

    def __init__(self, context: ComponentContext) -> None:
        super().__init__(context)
        self._log = context.logger()
        self._cfg: dict[str, Any] = dict(self._DEFAULTS)
        if context.config:
            for k, v in context.config.items():
                if k in self._cfg:
                    self._cfg[k] = v
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._devices: dict[str, Any] = {}
        self._light_state: dict[str, dict[str, Any]] = {}
        self._connected = False

    @property
    def component_id(self) -> str:
        return "knx"

    def capabilities(self) -> list[str]:
        return [
            "ping", "reset", "cfg/set",
            "light/on", "light/off", "light/brightness/set",
        ]

    def metadata(self) -> dict[str, Any]:
        out = super().metadata()
        out["capabilities"] = self.capabilities()
        return out

    def get_state_payload(self) -> dict[str, Any]:
        return {
            "connected": self._connected,
            "lights": list(self._light_state.values()),
        }

    def get_cfg_payload(self) -> dict[str, Any]:
        return dict(self._cfg)

    def schema(self) -> dict[str, Any]:
        s = copy.deepcopy(super().schema())
        s["publishes"]["state"]["fields"].update({
            "connected": {"type": "boolean"},
            "lights": {
                "type": "array",
                "items": {
                    "type": "object",
                    "fields": {
                        "name": {"type": "string"},
                        "on": {"type": "boolean"},
                        "brightness": {"type": "integer", "min": 0, "max": 255},
                    },
                },
            },
        })
        s["publishes"]["cfg"]["fields"].update({
            "gateway_host": {"type": "string"},
            "gateway_port": {"type": "integer"},
            "lights": {"type": "array", "description": "KNX light device definitions"},
        })
        s["subscribes"].update({
            "cmd/light/on": {
                "fields": {
                    "light": {"type": "string", "description": "Light name from config"},
                },
            },
            "cmd/light/off": {
                "fields": {
                    "light": {"type": "string", "description": "Light name from config"},
                },
            },
            "cmd/light/brightness/set": {
                "fields": {
                    "light": {"type": "string"},
                    "brightness": {"type": "integer", "min": 0, "max": 255},
                },
            },
        })
        return s

    # ── lifecycle ──────────────────────────────────────────────

    def _start(self) -> None:
        self._stop_event.clear()
        self._connected = False
        self._devices = {}
        self._light_state = {
            c["name"]: {"name": c["name"], "on": None, "brightness": None}
            for c in self._cfg.get("lights", [])
            if "name" in c
        }
        self._publish_all_retained()
        self._thread = threading.Thread(
            target=self._xknx_thread, name="KNXThread", daemon=True
        )
        self._thread.start()
        self._log.info(
            "KNX component started (gateway: %s:%s)",
            self._cfg["gateway_host"], self._cfg["gateway_port"],
        )

    def _stop(self) -> None:
        self._stop_event.set()
        # Wake the loop so it detects the stop_event on its next 0.5s sleep tick
        # rather than calling loop.stop() which abandons xknx tasks mid-flight.
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(lambda: None)
        if self._thread:
            self._thread.join(timeout=15.0)
            self._thread = None
        self._connected = False
        self._log.info("KNX component stopped")

    def _publish_all_retained(self) -> None:
        self.publish_metadata()
        self.publish_schema()
        self.publish_status()
        self.publish_state()
        self.set_telemetry_config({
            "connected": {"enabled": False, "interval_s": 0.1, "change_threshold_percent": 0},
        })
        self.publish_cfg()

    # ── xknx thread ──────────────────────────────────────────

    def _xknx_thread(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._xknx_main())
        except Exception:
            self._log.exception("KNX thread exited with error")
        finally:
            self._loop.close()
            self._loop = None

    async def _xknx_main(self) -> None:
        from xknx import XKNX
        from xknx.devices import Light
        from xknx.io import ConnectionConfig, ConnectionType

        host = self._cfg["gateway_host"]
        port = int(self._cfg["gateway_port"])

        if not host:
            self._log.error("gateway_host not configured — KNX thread idle")
            return

        while not self._stop_event.is_set():
            try:
                conn_cfg = ConnectionConfig(
                    connection_type=ConnectionType.TUNNELING,
                    gateway_ip=host,
                    gateway_port=port,
                )
                async with XKNX(connection_config=conn_cfg) as xknx:
                    self._devices = {}
                    for light_cfg in self._cfg.get("lights", []):
                        name = light_cfg["name"]
                        light = Light(
                            xknx,
                            name=name,
                            group_address_switch=light_cfg.get("address"),
                            group_address_switch_state=light_cfg.get("state_address"),
                            group_address_brightness=light_cfg.get("brightness_address"),
                            group_address_brightness_state=light_cfg.get(
                                "brightness_state_address"
                            ),
                        )
                        xknx.devices.async_add(light)
                        light.register_device_updated_cb(self._device_updated_cb)
                        self._devices[name] = light

                    self._connected = True
                    self.publish_state()
                    self._log.info("Connected to KNX gateway %s:%d", host, port)

                    last_monitor = 0.0
                    while not self._stop_event.is_set():
                        await asyncio.sleep(0.5)
                        now = time.time()
                        if now - last_monitor >= self._MONITOR_INTERVAL_S:
                            last_monitor = now
                            self.publish_state()
                            if self.should_publish_telemetry("connected", True):
                                self.publish_telemetry("connected", True)

            except Exception:
                if not self._stop_event.is_set():
                    self._log.exception("KNX gateway error")

            self._connected = False
            self._devices = {}

            if self._stop_event.is_set():
                break

            try:
                self.publish_state()
                if self.should_publish_telemetry("connected", False):
                    self.publish_telemetry("connected", False)
            except Exception:
                self._log.debug("Could not publish state during disconnect")

            self._log.info("Retrying KNX connection in 30s")
            for _ in range(60):
                if self._stop_event.is_set():
                    break
                await asyncio.sleep(0.5)

    def _device_updated_cb(self, device: Any) -> None:
        name = device.name
        if name not in self._light_state:
            return
        self._light_state[name] = {
            "name": name,
            "on": device.state,
            "brightness": device.current_brightness,
        }
        self.publish_state()

    # ── async dispatch helper ─────────────────────────────────

    def _run_async(self, coro: Any) -> Any:
        """Schedule a coroutine on the xknx event loop and block until done (5s timeout)."""
        if self._loop is None or not self._loop.is_running():
            raise RuntimeError("KNX event loop not running")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=5.0)

    # ── command helpers ───────────────────────────────────────

    def _resolve_lights(self, light_name: str) -> list[Any]:
        """Return list of devices for the given name.

        If light_name is empty or "all", returns all connected devices.
        Returns an empty list if the named light is unknown.
        """
        if not light_name or light_name == "all":
            return list(self._devices.values())
        device = self._devices.get(light_name)
        return [device] if device is not None else []

    # ── command handlers ──────────────────────────────────────

    def on_cmd_ping(self, payload_str: str) -> None:
        try:
            payload = json.loads(payload_str) if payload_str else {}
            request_id = payload.get("request_id", "")
        except json.JSONDecodeError:
            request_id = ""
        self.publish_result("ping", request_id, ok=True)

    def on_cmd_reset(self, payload_str: str) -> None:
        try:
            payload = json.loads(payload_str) if payload_str else {}
            request_id = payload.get("request_id", "")
        except json.JSONDecodeError:
            request_id = ""
        self._stop_event.set()
        if self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread:
            self._thread.join(timeout=10.0)
        self._stop_event.clear()
        self._connected = False
        self._devices = {}
        self._thread = threading.Thread(
            target=self._xknx_thread, name="KNXThread", daemon=True
        )
        self._thread.start()
        self.publish_result("reset", request_id, ok=True)

    def on_cmd_cfg_set(self, payload_str: str) -> None:
        request_id, set_dict, parse_error = self._parse_cfg_set_payload(payload_str)
        if parse_error:
            self.publish_cfg_set_result(
                request_id=request_id, ok=False, applied=None,
                error=parse_error, ts=_utc_iso(), action="cfg/set",
            )
            return

        hot_reload_keys = {"gateway_host", "gateway_port"}
        applied: dict[str, Any] = {}
        unknown: list[str] = []

        for key, val in set_dict.items():
            if key == "lights":
                self.publish_cfg_set_result(
                    request_id=request_id, ok=False, applied=None,
                    error="'lights' cannot be changed via cfg/set; restart agent to reload",
                    ts=_utc_iso(), action="cfg/set",
                )
                return
            elif key in hot_reload_keys:
                self._cfg[key] = val
                applied[key] = val
            else:
                unknown.append(key)

        if unknown:
            self.publish_cfg_set_result(
                request_id=request_id, ok=False, applied=applied or None,
                error=f"unknown cfg key(s): {', '.join(sorted(unknown))}",
                ts=_utc_iso(), action="cfg/set",
            )
            return

        self.publish_state()
        self.publish_cfg()
        self.publish_cfg_set_result(
            request_id=request_id, ok=True, applied=applied or None,
            error=None, ts=_utc_iso(), action="cfg/set",
        )

    def on_cmd_light_on(self, payload_str: str) -> None:
        try:
            payload = json.loads(payload_str) if payload_str else {}
            request_id = payload.get("request_id", "")
            light_name = payload.get("light", "")
        except json.JSONDecodeError:
            request_id, light_name = "", ""

        devices = self._resolve_lights(light_name)
        if not devices:
            self.publish_result("light/on", request_id, ok=False,
                                error=f"unknown light: {light_name!r}")
            return
        try:
            for device in devices:
                self._run_async(device.set_on())
        except Exception as exc:
            self.publish_result("light/on", request_id, ok=False, error=str(exc))
            return
        self.publish_result("light/on", request_id, ok=True)

    def on_cmd_light_off(self, payload_str: str) -> None:
        try:
            payload = json.loads(payload_str) if payload_str else {}
            request_id = payload.get("request_id", "")
            light_name = payload.get("light", "")
        except json.JSONDecodeError:
            request_id, light_name = "", ""

        devices = self._resolve_lights(light_name)
        if not devices:
            self.publish_result("light/off", request_id, ok=False,
                                error=f"unknown light: {light_name!r}")
            return
        try:
            for device in devices:
                self._run_async(device.set_off())
        except Exception as exc:
            self.publish_result("light/off", request_id, ok=False, error=str(exc))
            return
        self.publish_result("light/off", request_id, ok=True)

    def on_cmd_light_brightness_set(self, payload_str: str) -> None:
        try:
            payload = json.loads(payload_str) if payload_str else {}
            request_id = payload.get("request_id", "")
            light_name = payload.get("light", "")
            brightness = payload.get("brightness")
        except json.JSONDecodeError:
            request_id, light_name, brightness = "", "", None

        devices = self._resolve_lights(light_name)
        if not devices:
            self.publish_result("light/brightness/set", request_id, ok=False,
                                error=f"unknown light: {light_name!r}")
            return
        no_brightness = [d.name for d in devices if not d.supports_brightness]
        if no_brightness:
            self.publish_result("light/brightness/set", request_id, ok=False,
                                error=f"light(s) have no brightness address: {', '.join(no_brightness)}")
            return
        if brightness is None:
            self.publish_result("light/brightness/set", request_id, ok=False,
                                error="missing 'brightness' in payload")
            return
        brightness = max(0, min(255, int(brightness)))
        try:
            for device in devices:
                self._run_async(device.set_brightness(brightness))
        except Exception as exc:
            self.publish_result("light/brightness/set", request_id, ok=False, error=str(exc))
            return
        self.publish_result("light/brightness/set", request_id, ok=True)
