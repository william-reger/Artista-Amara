import tempfile
import unittest
from pathlib import Path

from machine_config import MachineConfig, load_machine_config, save_machine_config


class MachineSettingsPersistenceTest(unittest.TestCase):
    def test_load_missing_file_uses_defaults(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config = load_machine_config(Path(temp_dir) / "missing.json")

        self.assertEqual(config.tof_i2c_address, 0x29)
        self.assertEqual(config.led_pin_bcm, 9)
        self.assertEqual(config.cup_present_threshold_mm, 90.0)
        self.assertEqual(config.tank_detect_pin_board, 24)
        self.assertEqual(config.tank_led_pin_board, 19)
        self.assertEqual(config.tank_servo_pin_board, 40)

    def test_save_and_load_machine_offsets_and_tank_settings(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target = Path(temp_dir) / "machine_settings.json"
            config = MachineConfig(
                print_center_x_mm=88.8,
                print_center_y_mm=77.7,
                tof_offset_x_mm=1.2,
                tof_offset_y_mm=-3.4,
                tof_offset_z_mm=5.6,
                cup_present_threshold_mm=42.0,
                nozzle_mm=2.5,
                tof_i2c_address=0x29,
                led_pin_bcm=9,
                tank_servo_inserted_angle=12.0,
                tank_servo_removed_angle=66.0,
                tank_servo_settle_ms=450,
            )

            save_machine_config(config, target)
            loaded = load_machine_config(target)

        self.assertEqual(loaded.print_center, (88.8, 77.7))
        self.assertEqual(loaded.tof_offset_x_mm, 1.2)
        self.assertEqual(loaded.tof_offset_y_mm, -3.4)
        self.assertEqual(loaded.tof_offset_z_mm, 5.6)
        self.assertEqual(loaded.cup_present_threshold_mm, 42.0)
        self.assertEqual(loaded.nozzle_mm, 2.5)
        self.assertEqual(loaded.tank_servo_inserted_angle, 12.0)
        self.assertEqual(loaded.tank_servo_removed_angle, 66.0)
        self.assertEqual(loaded.tank_servo_settle_ms, 450)
        self.assertTrue(loaded.cup_present(41.9))
        self.assertFalse(loaded.cup_present(42.1))

    def test_update_rejects_invalid_threshold(self):
        config = MachineConfig()

        with self.assertRaises(ValueError):
            config.update_from_settings({"cup_present_threshold_mm": 0})

    def test_update_rejects_invalid_nozzle(self):
        config = MachineConfig()

        with self.assertRaises(ValueError):
            config.update_from_settings({"nozzle_mm": 0})

    def test_update_rejects_negative_tank_settle_ms(self):
        config = MachineConfig()

        with self.assertRaises(ValueError):
            config.update_from_settings({"tank_servo_settle_ms": -1})

    def test_default_pin_mapping_uses_new_relay_layout(self):
        config = MachineConfig()

        self.assertEqual(config.pin_command("heater", True), "M42 P30 S255")
        self.assertEqual(config.pin_command("pump", True), "M42 P29 S255")
        self.assertEqual(config.pin_command("flow_stop", True), "M42 P28 S255")
        self.assertEqual(config.pin_command("stop", False), "M42 P28 S0")
        self.assertEqual(config.pin_command("case_led", True), "M42 P27 S255")
        self.assertNotIn("valve", config.pins)
        self.assertNotIn("vacuum", config.pins)
        self.assertEqual(config.output_pins["vacuum_relay"].board_pin, 23)

    def test_legacy_settings_are_migrated_to_fixed_pin_layout(self):
        config = MachineConfig().update_from_settings(
            {
                "pins": {
                    "vacuum": {"name": "vacuum", "pin": 28, "active_high": True},
                    "valve": {"name": "valve", "pin": 26, "active_high": True},
                    "stop": {"name": "stop", "pin": 27, "active_high": True},
                },
                "output_pins": {
                    "case_led_relay": {"name": "case_led_relay", "board_pin": 23, "active_high": True}
                },
            }
        )

        self.assertEqual(config.pin_command("flow_stop", True), "M42 P28 S255")
        self.assertEqual(config.pin_command("case_led", True), "M42 P27 S255")
        self.assertNotIn("valve", config.pins)
        self.assertNotIn("vacuum", config.pins)
        self.assertIn("vacuum_relay", config.output_pins)
        self.assertNotIn("case_led_relay", config.output_pins)


if __name__ == "__main__":
    unittest.main()
