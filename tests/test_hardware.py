import unittest

from hardware import HardwareController
from machine_config import MachineConfig


class FakeTof:
    def __init__(self, distance):
        self.range = distance


class FakeServo:
    def __init__(self):
        self.values = []

    def ChangeDutyCycle(self, value):
        self.values.append(value)

    def stop(self):
        pass


class FakeTankHardware(HardwareController):
    def __init__(self, config=None, tank_present=False):
        super().__init__(config or MachineConfig(), initialize=False)
        self._tank_present_override = tank_present
        self._tank_servo_pwm = FakeServo()
        self._gpio_ready = True
        self._input_states = {"system_switch": False, "tank_present": tank_present}

    def get_system_enabled(self):
        return True

    def _tank_present(self):
        return self._tank_present_override

    def set_present(self, present):
        self._tank_present_override = bool(present)
        self._input_states["tank_present"] = bool(present)


class HardwareControllerTest(unittest.TestCase):
    def test_tof_payload_marks_cup_presence(self):
        config = MachineConfig(cup_present_threshold_mm=50, tof_offset_z_mm=2)
        hardware = HardwareController(config, initialize=False)
        hardware._tof = FakeTof(40)

        payload = hardware.read_tof_payload()

        self.assertEqual(payload["raw_distance_mm"], 40)
        self.assertEqual(payload["z_mm"], 42)
        self.assertTrue(payload["cup_present"])
        self.assertFalse(payload["simulated"])

    def test_simulated_payload_when_tof_unconfigured(self):
        config = MachineConfig(default_tof_distance_mm=75, cup_present_threshold_mm=50)
        hardware = HardwareController(config, initialize=False)

        payload = hardware.read_tof_payload()

        self.assertEqual(payload["raw_distance_mm"], 75)
        self.assertFalse(payload["cup_present"])
        self.assertTrue(payload["simulated"])

    def test_config_error_status_has_priority(self):
        hardware = HardwareController(MachineConfig(), initialize=False)
        hardware.config_error = "missing led"

        hardware.set_led_status("printing")

        self.assertEqual(hardware.get_led_status(), "config_error")

    def test_system_switch_controls_case_led_output(self):
        hardware = HardwareController(MachineConfig(case_led_enabled=True), initialize=False)
        hardware._input_states = {"system_switch": False}

        hardware.set_case_led_enabled(True)
        self.assertTrue(hardware.get_hardware_state()["case_led_output_active"])

        hardware.set_simulated_input_state("system_switch", True)
        state = hardware.get_hardware_state()
        self.assertFalse(state["system_enabled"])
        self.assertFalse(state["case_led_output_active"])

        hardware.set_simulated_input_state("system_switch", False)
        state = hardware.get_hardware_state()
        self.assertTrue(state["system_enabled"])
        self.assertTrue(state["case_led_output_active"])

    def test_vacuum_relay_uses_pi_output_state(self):
        hardware = HardwareController(MachineConfig(), initialize=False)

        hardware.set_output_pin("vacuum_relay", True)

        state = hardware.get_hardware_state()
        self.assertTrue(state["output_states"]["vacuum_relay"])
        self.assertTrue(state["outputs"]["vacuum_relay"]["enabled"])
        self.assertEqual(state["outputs"]["vacuum_relay"]["board_pin"], 23)

    def test_hardware_state_exposes_gpio_metadata(self):
        hardware = HardwareController(MachineConfig(), initialize=False)
        hardware._gpio_ready = False
        hardware._gpio_error = "RPi.GPIO missing"

        state = hardware.get_hardware_state()

        self.assertIn("inputs", state)
        self.assertIn("outputs", state)
        self.assertFalse(state["gpio"]["ready"])
        self.assertEqual(state["gpio"]["error"], "RPi.GPIO missing")
        self.assertEqual(state["inputs"]["system_switch"]["board_pin"], 7)
        self.assertEqual(state["outputs"]["vacuum_relay"]["board_pin"], 23)

    def test_tank_missing_reports_fault_free_error_state(self):
        hardware = FakeTankHardware(MachineConfig(), tank_present=False)

        payload = hardware._sync_tank_state(force=True, emit_warning=True)

        self.assertFalse(payload["present"])
        self.assertFalse(payload["fault"])
        self.assertEqual(payload["status"], "missing")
        self.assertFalse(payload["servo_connected"])

    def test_tank_present_moves_servo_to_connected_position(self):
        config = MachineConfig(tank_servo_inserted_angle=10, tank_servo_removed_angle=60, tank_servo_settle_ms=0)
        hardware = FakeTankHardware(config, tank_present=True)

        payload = hardware._sync_tank_state(force=True)

        self.assertTrue(payload["present"])
        self.assertTrue(payload["servo_connected"])
        self.assertEqual(payload["status"], "inserted")
        self.assertEqual(hardware._tank_servo_angle, 10)

    def test_manual_servo_override_updates_tank_state(self):
        config = MachineConfig(tank_servo_inserted_angle=0, tank_servo_removed_angle=45, tank_servo_settle_ms=0)
        hardware = FakeTankHardware(config, tank_present=True)

        hardware.set_servo_angle(45)
        payload = hardware.get_tank_state()

        self.assertEqual(payload["status"], "connecting")
        self.assertFalse(payload["servo_connected"])


if __name__ == "__main__":
    unittest.main()
