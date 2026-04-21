import time
import unittest

from machine_config import ControlPin, MachineConfig
from print_jobs import PrintJobManager


class FakeMarlin:
    def __init__(self, delays=None):
        self.commands = []
        self.cleared = False
        self.delays = delays or {}

    def send_command(self, line, timeout=None):
        self.commands.append(line)
        time.sleep(self.delays.get(line, 0))
        return ["ok"]

    def clear_queue(self):
        self.cleared = True


class FakeHardware:
    def __init__(self, distance_mm=12.5, system_enabled=True):
        self.distance_mm = distance_mm
        self.system_enabled = system_enabled
        self.outputs = {"vacuum_relay": True}
        self.output_events = []

    def read_tof_mm(self):
        return self.distance_mm

    def get_system_enabled(self):
        return self.system_enabled

    def set_output_pin(self, pin_name, enabled):
        self.outputs[pin_name] = bool(enabled)
        self.output_events.append((pin_name, bool(enabled)))


class MachineConfigTest(unittest.TestCase):
    def test_pin_polarity_aliases_and_offsets_are_configurable(self):
        config = MachineConfig(
            bed_width_mm=120,
            bed_height_mm=80,
            tof_offset_x_mm=5,
            tof_offset_y_mm=-3,
            tof_offset_z_mm=1.5,
            pins={
                "heater": ControlPin("heater", 30, True),
                "pump": ControlPin("pump", 29, True),
                "flow_stop": ControlPin("flow_stop", 28, False),
                "case_led": ControlPin("case_led", 27, True),
            },
        )

        self.assertEqual(config.pin_command("pump", True), "M42 P29 S255")
        self.assertEqual(config.pin_command("heater", False), "M42 P30 S0")
        self.assertEqual(config.pin_command("flow_stop", False), "M42 P28 S255")
        self.assertEqual(config.pin_command("stop", True), "M42 P28 S0")
        self.assertEqual(config.pin_command("case_led", True), "M42 P27 S255")
        self.assertEqual(config.pin_command("led_filament", False), "M42 P27 S0")
        self.assertEqual(config.tof_probe_position, (55, 43))
        self.assertEqual(config.surface_z(12.5), 14.0)


class PrintJobManagerTest(unittest.TestCase):
    def test_print_job_homes_sets_tof_offset_and_wraps_pump_with_stop(self):
        marlin = FakeMarlin()
        events = []
        config = MachineConfig(
            bed_width_mm=120,
            bed_height_mm=80,
            tof_offset_x_mm=5,
            tof_offset_y_mm=-3,
            tof_offset_z_mm=1.5,
            command_timeout_s=0.1,
        )
        manager = PrintJobManager(
            marlin,
            FakeHardware(12.5),
            config=config,
            emit_event=lambda event, payload: events.append((event, payload)),
        )

        manager.start_print(["M42 P29 S255", "G1 X1 Y2", "M42 P29 S0"])
        self.assertTrue(manager.wait_for_idle(1))

        self.assertIn("G28", marlin.commands)
        self.assertLess(marlin.commands.index("G28"), marlin.commands.index("G92 Z14.000"))
        self.assertLess(marlin.commands.index("G92 Z14.000"), marlin.commands.index("G1 X1 Y2"))
        self.assertIn("G0 X55.000 Y43.000", marlin.commands)

        pump_on_index = marlin.commands.index("M42 P29 S255", 1)
        pump_off_index = marlin.commands.index("M42 P29 S0", pump_on_index + 1)
        self.assertEqual(marlin.commands[pump_on_index - 1], "M42 P28 S0")
        self.assertEqual(marlin.commands[pump_off_index + 1], "M42 P28 S255")

        progress = [payload for event, payload in events if event == "print_progress"]
        self.assertEqual(progress[0]["percent"], 0)
        self.assertEqual(progress[-1]["percent"], 100)
        tof = [payload for event, payload in events if event == "tof_reading"][0]
        self.assertEqual(tof["probe_x_mm"], 55)
        self.assertEqual(tof["probe_y_mm"], 43)
        self.assertEqual(tof["z_mm"], 14.0)

    def test_recipe_before_while_and_after_phases_run_in_order(self):
        marlin = FakeMarlin(delays={"G1 X1 Y2": 0.03})
        hardware = FakeHardware()
        events = []
        recipe = {
            "version": 3,
            "name": "Cycle",
            "phases": {
                "before_printing": [
                    {"type": "set", "outputs": {"heater": True}},
                    {"type": "wait", "duration_ms": 1},
                    {"type": "set", "outputs": {"heater": False}},
                ],
                "while_printjob": [
                    {"type": "set", "outputs": {"vacuum": True}},
                    {"type": "wait", "duration_ms": 20},
                    {"type": "set", "outputs": {"vacuum": False}},
                ],
                "while_printing": [
                    {"type": "set", "outputs": {"vacuum": True}},
                    {"type": "wait", "duration_ms": 10},
                    {"type": "set", "outputs": {"vacuum": False}},
                ],
                "after_printjob": [
                    {"type": "set", "outputs": {"pump": True}},
                    {"type": "wait", "duration_ms": 1},
                    {"type": "set", "outputs": {"pump": False}},
                ],
            },
        }
        manager = PrintJobManager(
            marlin,
            hardware,
            emit_event=lambda event, payload: events.append((event, payload)),
            sleep_interval_s=0.001,
        )

        manager.start_print(["M42 P29 S255", "G1 X1 Y2", "M42 P29 S0"], recipe=recipe, recipe_name="cycle")
        self.assertTrue(manager.wait_for_idle(2))

        heater_on_index = marlin.commands.index("M42 P30 S255")
        home_index = marlin.commands.index("G28")
        gcode_index = marlin.commands.index("G1 X1 Y2")
        after_pump_on_index = marlin.commands.index("M42 P29 S255", gcode_index + 1)

        self.assertLess(heater_on_index, home_index)
        self.assertLess(home_index, gcode_index)
        self.assertGreater(after_pump_on_index, gcode_index)
        self.assertIn(("vacuum_relay", True), hardware.output_events)
        self.assertIn(("vacuum_relay", False), hardware.output_events)
        self.assertIn(("recipe_phase", {"phase": "while_printjob", "recipe": "cycle", "status": "running"}), events)
        self.assertIn(("recipe_phase", {"phase": "while_printing", "recipe": "cycle", "status": "running"}), events)

    def test_after_phase_is_skipped_when_print_errors(self):
        class FailingMarlin(FakeMarlin):
            def send_command(self, line, timeout=None):
                if line == "G1 X1 Y2":
                    raise RuntimeError("line failed")
                return super().send_command(line, timeout)

        marlin = FailingMarlin()
        recipe = {
            "version": 3,
            "name": "Cycle",
            "phases": {
                "after_printjob": [
                    {"type": "set", "outputs": {"heater": True}},
                ],
            },
        }
        manager = PrintJobManager(marlin, FakeHardware(), emit_event=lambda event, payload: None)

        manager.start_print(["G1 X1 Y2"], recipe=recipe)
        self.assertTrue(manager.wait_for_idle(1))

        self.assertNotIn("M42 P30 S255", marlin.commands)

    def test_abort_clears_queue_and_applies_safe_state(self):
        marlin = FakeMarlin()
        events = []
        hardware = FakeHardware()
        manager = PrintJobManager(
            marlin,
            hardware,
            emit_event=lambda event, payload: events.append((event, payload)),
        )

        manager.abort()

        self.assertTrue(marlin.cleared)
        self.assertIn("M410", marlin.commands)
        self.assertEqual(
            marlin.commands[-3:],
            [
                "M42 P29 S0",
                "M42 P30 S0",
                "M42 P28 S255",
            ],
        )
        self.assertNotIn("M42 P26 S0", marlin.commands)
        self.assertNotIn("M42 P27 S0", marlin.commands)
        self.assertFalse(hardware.outputs["vacuum_relay"])
        self.assertIn(("warning", {"msg": "Print aborted", "level": "warning"}), events)

    def test_start_print_rejects_when_system_switch_is_off(self):
        manager = PrintJobManager(FakeMarlin(), FakeHardware(system_enabled=False))

        with self.assertRaises(ValueError):
            manager.start_print(["G28"])


if __name__ == "__main__":
    unittest.main()
