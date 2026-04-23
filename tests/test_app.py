import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import CleaningSession, RECIPE_DIR, UPLOAD_DIR, app, handle_print_job_complete, socketio
from machine_config import MachineConfig
from relay_sequencer import RelaySequencer
from usage_stats import UsageStatsStore


class FakePrintManager:
    def __init__(self):
        self.started_gcode = None
        self.started_recipe = None
        self.started_recipe_name = None
        self.started_moving_only = False
        self.aborted = False
        self.active = False

    def start_print(self, gcode, recipe=None, recipe_name=None, moving_only=False):
        self.started_gcode = gcode
        self.started_recipe = recipe
        self.started_recipe_name = recipe_name
        self.started_moving_only = bool(moving_only)
        self.active = True
        return "job-1"

    def abort(self):
        self.aborted = True
        self.active = False
        return "job-1"

    def is_active(self):
        return self.active


class FakeMarlin:
    def __init__(self):
        self.commands = []
        self.timeout = None
        self.debug_state = {
            "connected": False,
            "port": "/dev/serial0",
            "baudrate": 115200,
            "timeout": 2,
            "last_command": None,
            "last_response_lines": [],
            "last_error": None,
            "last_error_type": None,
            "last_test": None,
        }

    def send_command(self, line, timeout=None):
        self.commands.append(line)
        self.debug_state["connected"] = True
        self.debug_state["last_command"] = line
        self.debug_state["last_response_lines"] = ["ok"]
        self.debug_state["last_error"] = None
        self.debug_state["last_error_type"] = None
        return ["ok"]

    def get_debug_state(self):
        return dict(self.debug_state)

    def mark_test_result(self, name, success, command=None, responses=None, error=None):
        self.debug_state["last_test"] = {
            "name": name,
            "success": success,
            "command": command,
            "responses": list(responses or []),
            "error": error,
        }

    def clear_queue(self):
        return None


class FakeRelaySequencer:
    def __init__(self):
        self.state = {
            "status": "idle",
            "recipe": None,
            "relay_state": {
                "pump": False,
                "heater": False,
                "vacuum": False,
                "flow_stop": False,
            },
            "elapsed_ms": 0,
            "error": None,
            "timeline_length": 0,
        }
        self.started = None
        self.stopped = False

    def start(self, recipe, recipe_name=None):
        self.started = (recipe, recipe_name)
        self.state["status"] = "running"
        self.state["recipe"] = recipe_name or recipe["name"]
        self.state["timeline_length"] = 1
        return self.state["recipe"]

    def stop(self):
        self.stopped = True
        self.state["status"] = "stopped"
        return self.get_state()

    def set_relays(self, values):
        self.state["relay_state"].update(values)
        return self.get_state()

    def get_state(self):
        return {
            "status": self.state["status"],
            "recipe": self.state["recipe"],
            "relay_state": dict(self.state["relay_state"]),
            "elapsed_ms": self.state["elapsed_ms"],
            "error": self.state["error"],
            "timeline_length": self.state["timeline_length"],
        }

    def is_active(self):
        return self.state["status"] == "running"


class FakeHardware:
    def __init__(self, tank_present=True, system_enabled=True):
        self.tank_present = tank_present
        self.system_enabled = system_enabled
        self.angle = None
        self.case_led_enabled = False
        self.output_states = {"vacuum_relay": False}

    def get_system_enabled(self):
        return self.system_enabled

    def get_hardware_state(self):
        return {
            "system_enabled": self.system_enabled,
            "system_switch_closed": not self.system_enabled,
            "quick_buttons": {},
            "case_led_enabled": self.case_led_enabled,
            "case_led_output_active": self.case_led_enabled and self.system_enabled,
            "output_states": dict(self.output_states),
            "inputs": {
                "system_switch": {
                    "name": "system_switch",
                    "board_pin": 7,
                    "bcm_pin": 4,
                    "pull_up": True,
                    "active_low": True,
                    "active": not self.system_enabled,
                    "raw_value": self.system_enabled,
                    "last_changed_monotonic": 0.0,
                }
            },
            "outputs": {
                "vacuum_relay": {
                    "name": "vacuum_relay",
                    "board_pin": 23,
                    "bcm_pin": 11,
                    "active_high": True,
                    "enabled": bool(self.output_states["vacuum_relay"]),
                    "gpio_ready": True,
                    "last_changed_monotonic": 0.0,
                }
            },
            "gpio": {"ready": True, "error": None},
            "config_error": None,
            "tank_present": self.tank_present,
        }

    def get_tank_state(self):
        return {
            "present": self.tank_present,
            "servo_connected": self.tank_present,
            "fault": False,
            "status": "inserted" if self.tank_present else "missing",
            "message": "Tank inserted" if self.tank_present else "Tank missing",
            "simulated": False,
            "angle": self.angle,
        }

    def read_tof_mm(self):
        return 12.0

    def read_tof_payload(self, allow_simulated=True):
        return {
            "distance_mm": 12.0,
            "raw_distance_mm": 12.0,
            "z_mm": 12.0,
            "cup_present": True,
            "simulated": False,
            "offsets": {"x_mm": 0.0, "y_mm": 0.0, "z_mm": 0.0},
        }

    def update_config(self, config):
        self.config = config

    def set_case_led_enabled(self, enabled):
        self.case_led_enabled = bool(enabled)
        return self.get_hardware_state()

    def set_output_pin(self, pin_name, enabled):
        self.output_states[pin_name] = bool(enabled)
        return True

    def set_servo_angle(self, angle):
        self.angle = float(angle)

    def set_system_state_callback(self, callback):
        self.callback = callback


class AppSocketIOTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.recipe_path = RECIPE_DIR / "test-api-recipe.json"
        self.hardware = FakeHardware(tank_present=True)
        app.config["HARDWARE"] = self.hardware
        app.config["PRINT_MANAGER"] = FakePrintManager()
        app.config["RELAY_SEQUENCER"] = FakeRelaySequencer()
        app.config["MARLIN"] = FakeMarlin()
        app.config["USAGE_STATS"] = UsageStatsStore(Path(self.temp_dir.name) / "usage_stats.json")
        app.config["CLEANING_SESSION"] = CleaningSession()

    def tearDown(self):
        self.temp_dir.cleanup()
        app.config.pop("PRINT_MANAGER", None)
        app.config.pop("RELAY_SEQUENCER", None)
        app.config.pop("HARDWARE", None)
        app.config.pop("MARLIN", None)
        app.config.pop("MACHINE_CONFIG", None)
        app.config.pop("USAGE_STATS", None)
        app.config.pop("CLEANING_SESSION", None)
        if self.recipe_path.exists():
            self.recipe_path.unlink()

    def test_health_reports_socketio(self):
        client = app.test_client()

        response = client.get("/api/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"status": "ok", "socketio": True})

    def test_machine_config_endpoint_includes_tank_state(self):
        client = app.test_client()

        response = client.get("/api/machine-config")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIn("tank_state", payload)
        self.assertTrue(payload["tank_state"]["present"])
        self.assertIn("tank_servo_inserted_angle", payload)
        self.assertIn("nozzle_mm", payload)
        self.assertIn("print_center_x_mm", payload)

    def test_recipes_endpoint_still_lists_recipes(self):
        client = app.test_client()

        response = client.get("/api/recipes")

        self.assertEqual(response.status_code, 200)
        self.assertIn("recipes", response.get_json())
        self.assertIn("Cleaning", response.get_json()["recipes"])

    def test_presets_endpoint_lists_svg_files(self):
        client = app.test_client()

        response = client.get("/api/presets")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIn("presets", payload)
        self.assertTrue(any(preset["file"].endswith(".svg") for preset in payload["presets"]))

    def test_usage_stats_endpoint_returns_defaults(self):
        client = app.test_client()

        response = client.get("/api/usage-stats")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["prints_since_cleaning"], 0)
        self.assertEqual(payload["total_prints"], 0)
        self.assertEqual(payload["total_cleanings"], 0)
        self.assertTrue(payload["can_defer_cleaning"])

    def test_start_print_endpoint_starts_manager(self):
        manager = FakePrintManager()
        app.config["PRINT_MANAGER"] = manager
        client = app.test_client()

        response = client.post("/api/start-print", json={"gcode": ["G21", "M400"]})

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json(), {"job_id": "job-1", "status": "started", "moving_only": False})
        self.assertEqual(manager.started_gcode, ["G21", "M400"])

    def test_start_print_endpoint_passes_recipe_name_to_manager(self):
        manager = FakePrintManager()
        app.config["PRINT_MANAGER"] = manager
        client = app.test_client()

        response = client.post("/api/start-print", json={"gcode": ["G21"], "recipe": "oatmilk"})

        self.assertEqual(response.status_code, 202)
        self.assertEqual(manager.started_recipe_name, "oatmilk")
        self.assertEqual(manager.started_recipe["version"], 3)

    def test_start_print_endpoint_passes_moving_only_to_manager(self):
        manager = FakePrintManager()
        app.config["PRINT_MANAGER"] = manager
        client = app.test_client()

        response = client.post("/api/start-print", json={"gcode": ["G21"], "moving_only": True})

        self.assertEqual(response.status_code, 202)
        self.assertTrue(manager.started_moving_only)
        self.assertTrue(response.get_json()["moving_only"])

    def test_start_print_rejects_missing_tank(self):
        app.config["HARDWARE"] = FakeHardware(tank_present=False)
        client = app.test_client()

        response = client.post("/api/start-print", json={"gcode": ["G21"]})

        self.assertEqual(response.status_code, 409)
        self.assertIn("Tank", response.get_json()["error"])

    def test_abort_endpoint_aborts_manager(self):
        manager = FakePrintManager()
        app.config["PRINT_MANAGER"] = manager
        client = app.test_client()

        response = client.post("/api/abort")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"job_id": "job-1", "status": "aborted"})
        self.assertTrue(manager.aborted)

    def test_gcode_endpoint_accepts_paths_and_saves_file(self):
        client = app.test_client()
        response = client.post(
            "/api/gcode",
            json={
                "paths": [
                    {
                        "closed": True,
                        "points": [
                            {"x": 0, "y": 0},
                            {"x": 100, "y": 0},
                            {"x": 100, "y": 100},
                            {"x": 0, "y": 100},
                        ],
                    }
                ],
                "coordinate_size": 100,
                "cup_radius": 50,
                "cup_size": "espresso",
                "cup_scale": 0.5,
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["filename"].endswith(".gcode"))
        self.assertEqual(payload["line_count"], len(payload["gcode"]))
        self.assertEqual(payload["print_diameter_mm"], 45.0)
        self.assertEqual(payload["effective_print_diameter_mm"], 22.5)
        self.assertIn("M42 P29 S255", payload["gcode"])
        target = UPLOAD_DIR / payload["filename"]
        self.assertTrue(target.exists())
        target.unlink()

    def test_gcode_endpoint_requires_known_cup_size(self):
        client = app.test_client()

        response = client.post(
            "/api/gcode",
            json={
                "paths": [
                    {
                        "closed": True,
                        "points": [
                            {"x": 0, "y": 0},
                            {"x": 100, "y": 0},
                            {"x": 100, "y": 100},
                            {"x": 0, "y": 100},
                        ],
                    }
                ],
                "coordinate_size": 100,
                "cup_radius": 50,
            },
        )

        self.assertEqual(response.status_code, 400)
        self.assertIn("cup_size", response.get_json()["error"])

    def test_gcode_endpoint_uses_machine_center_and_nozzle(self):
        app.config["MACHINE_CONFIG"] = MachineConfig(
            print_center_x_mm=10,
            print_center_y_mm=20,
            nozzle_mm=10,
        )
        client = app.test_client()

        response = client.post(
            "/api/gcode",
            json={
                "paths": [
                    {
                        "closed": True,
                        "points": [
                            {"x": 0, "y": 0},
                            {"x": 100, "y": 0},
                            {"x": 100, "y": 100},
                            {"x": 0, "y": 100},
                        ],
                    }
                ],
                "coordinate_size": 100,
                "cup_radius": 50,
                "cup_size": "espresso",
                "cup_scale": 0.5,
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["nozzle_mm"], 10)
        self.assertIn("G0 X-1.250 Y10.000 F3000", payload["gcode"])
        target = UPLOAD_DIR / payload["filename"]
        self.assertTrue(target.exists())
        target.unlink()

    def test_save_recipe_validates_new_format(self):
        client = app.test_client()
        response = client.put(
            "/api/recipes/test-api-recipe",
            json={
                "version": 3,
                "name": "API Recipe",
                "phases": {
                    "before_printing": [
                        {"type": "set", "outputs": {"pump": True, "flow_stop": True}},
                    ],
                    "while_printjob": [],
                    "while_printing": [],
                    "after_printjob": [],
                },
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["recipe"]["version"], 3)
        self.assertTrue(self.recipe_path.exists())

    def test_save_recipe_migrates_legacy_version_2_format(self):
        client = app.test_client()
        response = client.put(
            "/api/recipes/test-api-recipe",
            json={
                "version": 2,
                "name": "Legacy Recipe",
                "steps": [{"time_ms": 0, "set": {"pump": True}}],
                "loops": [],
            },
        )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["recipe"]["version"], 3)
        self.assertEqual(payload["recipe"]["phases"]["before_printing"][0]["outputs"]["pump"], True)

    def test_foam_endpoints_start_stop_and_state(self):
        sequencer = FakeRelaySequencer()
        app.config["RELAY_SEQUENCER"] = sequencer
        client = app.test_client()

        start = client.post("/api/foam/start", json={"recipe": "oatmilk"})
        state = client.get("/api/foam/state")
        stop = client.post("/api/foam/stop")

        self.assertEqual(start.status_code, 202)
        self.assertEqual(start.get_json()["status"], "started")
        self.assertEqual(state.status_code, 200)
        self.assertEqual(state.get_json()["status"], "running")
        self.assertEqual(stop.status_code, 200)
        self.assertTrue(sequencer.stopped)

    def test_cleaning_start_endpoint_starts_manager_in_moving_only_mode(self):
        manager = FakePrintManager()
        app.config["PRINT_MANAGER"] = manager
        client = app.test_client()

        response = client.post("/api/cleaning/start")

        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.get_json()["recipe"], "Cleaning")
        self.assertTrue(manager.started_moving_only)
        self.assertEqual(manager.started_recipe_name, "Cleaning")
        self.assertEqual(manager.started_gcode, ["G0 X100.000 Y100.000 F1800", "G0 Z0.000 F300"])

    def test_cleaning_output_requires_active_cleaning_mode(self):
        client = app.test_client()

        response = client.post("/api/cleaning/output", json={"name": "pump", "enabled": True})

        self.assertEqual(response.status_code, 409)
        self.assertIn("Cleaning mode is not active", response.get_json()["error"])

    def test_cleaning_output_updates_pump_when_active(self):
        app.config["CLEANING_SESSION"].activate()
        marlin = FakeMarlin()
        app.config["MARLIN"] = marlin
        client = app.test_client()

        response = client.post("/api/cleaning/output", json={"name": "pump", "enabled": True})

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["outputs"]["pump"])
        self.assertIn("M42 P29 S255", marlin.commands)

    def test_cleaning_stop_turns_outputs_off(self):
        session = app.config["CLEANING_SESSION"]
        session.activate()
        session.set_output("pump", True)
        marlin = FakeMarlin()
        app.config["MARLIN"] = marlin
        client = app.test_client()

        response = client.post("/api/cleaning/stop")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["cleaning"]["active"])
        self.assertIn("M42 P29 S0", marlin.commands)
        self.assertIn("M42 P28 S255", marlin.commands)

    def test_relays_endpoint_updates_partial_state(self):
        sequencer = FakeRelaySequencer()
        app.config["RELAY_SEQUENCER"] = sequencer
        client = app.test_client()

        response = client.post("/api/relays", json={"pump": True, "vacuum": True})

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["relay_state"]["pump"])
        self.assertTrue(payload["relay_state"]["vacuum"])

    def test_relays_endpoint_rejects_removed_valve(self):
        app.config["RELAY_SEQUENCER"] = RelaySequencer(FakeMarlin(), hardware=self.hardware)
        client = app.test_client()

        response = client.post("/api/relays", json={"valve": True})

        self.assertEqual(response.status_code, 400)

    def test_case_led_endpoint_uses_marlin_p27(self):
        marlin = FakeMarlin()
        app.config["MARLIN"] = marlin
        client = app.test_client()

        with patch("app.save_machine_config", lambda config, path: None):
            response = client.put("/api/case-led", json={"enabled": True})

        self.assertEqual(response.status_code, 200)
        self.assertIn("M42 P27 S255", marlin.commands)

    def test_tank_endpoint_returns_state(self):
        client = app.test_client()

        response = client.post("/api/tank", json={"angle": 15})

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["angle"], 15)
        self.assertIn("tank", payload)

    def test_motion_home_sends_g28(self):
        marlin = FakeMarlin()
        app.config["MARLIN"] = marlin
        client = app.test_client()

        response = client.post("/api/motion/home")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(marlin.commands, ["G28"])

    def test_motion_jog_sends_relative_move_and_restores_absolute(self):
        marlin = FakeMarlin()
        app.config["MARLIN"] = marlin
        client = app.test_client()

        response = client.post("/api/motion/jog", json={"axis": "x", "distance_mm": -5})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(marlin.commands, ["G91", "G0 X-5.000 F1800", "G90"])

    def test_debug_state_returns_snapshot(self):
        marlin = FakeMarlin()
        app.config["MARLIN"] = marlin
        client = app.test_client()

        response = client.get("/api/debug/state")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIn("hardware", payload)
        self.assertIn("relay_state", payload)
        self.assertIn("tank_state", payload)
        self.assertIn("marlin", payload)
        self.assertIn("guards", payload)
        self.assertIn("issues", payload)
        self.assertIn("inputs", payload["hardware"])
        self.assertIn("outputs", payload["hardware"])
        self.assertIn("cleaning_state", payload)
        self.assertIn("usage_stats", payload)

    def test_debug_uart_command_runs_manual_command(self):
        marlin = FakeMarlin()
        app.config["MARLIN"] = marlin
        client = app.test_client()

        response = client.post("/api/debug/uart/command", json={"command": "M114"})

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["command"], "M114")
        self.assertEqual(payload["responses"], ["ok"])
        self.assertIn("M114", marlin.commands)

    def test_debug_uart_command_requires_idle_and_system_enabled(self):
        manager = FakePrintManager()
        manager.active = True
        app.config["PRINT_MANAGER"] = manager
        client = app.test_client()

        response = client.post("/api/debug/uart/command", json={"command": "M114"})

        self.assertEqual(response.status_code, 409)
        self.assertIn("Print job is active", response.get_json()["error"])

    def test_debug_uart_test_runs_predefined_command(self):
        marlin = FakeMarlin()
        app.config["MARLIN"] = marlin
        client = app.test_client()

        response = client.post("/api/debug/uart/test", json={"name": "position"})

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["command"], "M114")
        self.assertEqual(marlin.debug_state["last_test"]["name"], "position")

    def test_debug_output_uses_gpio_for_vacuum(self):
        hardware = FakeHardware()
        app.config["HARDWARE"] = hardware
        client = app.test_client()

        response = client.post("/api/debug/output", json={"name": "vacuum", "enabled": True})

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["result"]["target"], "gpio")
        self.assertTrue(hardware.output_states["vacuum_relay"])

    def test_debug_output_uses_marlin_for_control_pin(self):
        marlin = FakeMarlin()
        app.config["MARLIN"] = marlin
        client = app.test_client()

        response = client.post("/api/debug/output", json={"name": "pump", "enabled": True})

        self.assertEqual(response.status_code, 200)
        self.assertIn("M42 P29 S255", marlin.commands)

    def test_handle_print_job_complete_increments_print_stats(self):
        handle_print_job_complete(job_id="job-1", recipe=None, recipe_name=None, moving_only=False)

        stats = app.config["USAGE_STATS"].load()
        self.assertEqual(stats["total_prints"], 1)
        self.assertEqual(stats["prints_since_cleaning"], 1)
        self.assertIsNotNone(stats["last_printed_at"])

    def test_handle_print_job_complete_marks_cleaning_success(self):
        handle_print_job_complete(job_id="job-2", recipe=None, recipe_name="Cleaning", moving_only=True)

        stats = app.config["USAGE_STATS"].load()
        self.assertEqual(stats["total_cleanings"], 1)
        self.assertEqual(stats["prints_since_cleaning"], 0)
        self.assertIsNotNone(stats["last_cleaned_at"])
        self.assertTrue(app.config["CLEANING_SESSION"].is_active())

    def test_socketio_connect_and_ping(self):
        client = socketio.test_client(app)
        self.assertTrue(client.is_connected())

        received = client.get_received()
        self.assertTrue(
            any(
                event["name"] == "server_status"
                and event["args"][0]["status"] == "connected"
                for event in received
            )
        )
        self.assertTrue(any(event["name"] == "relay_update" for event in received))
        self.assertTrue(any(event["name"] == "foam_status" for event in received))
        self.assertTrue(any(event["name"] == "tank_status" for event in received))
        self.assertTrue(any(event["name"] == "machine_config" for event in received))
        self.assertTrue(any(event["name"] == "usage_stats" for event in received))
        self.assertTrue(any(event["name"] == "cleaning_state" for event in received))
        self.assertTrue(any(event["name"] == "debug_snapshot" for event in received))

        client.emit("client_ping", {"timestamp": 123})
        received = client.get_received()
        self.assertTrue(
            any(
                event["name"] == "server_pong"
                and event["args"][0]["status"] == "ok"
                and event["args"][0]["timestamp"] == 123
                for event in received
            )
        )

        client.disconnect()


if __name__ == "__main__":
    unittest.main()
