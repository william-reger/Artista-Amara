import time
import unittest

from relay_sequencer import (
    RecipeValidationError,
    RelaySequencer,
    RelaySequencerBusyError,
    compile_recipe,
    compile_recipe_phase,
    validate_recipe,
)


class FakeMarlin:
    def __init__(self):
        self.commands = []

    def send_command(self, line, timeout=None):
        self.commands.append(line)
        return ["ok"]


class FakeHardware:
    def __init__(self):
        self.outputs = []

    def set_output_pin(self, pin_name, enabled):
        self.outputs.append((pin_name, bool(enabled)))
        return True


class RelaySequencerTest(unittest.TestCase):
    def test_version_2_recipe_migrates_to_before_printing_actions(self):
        recipe = {
            "version": 2,
            "name": "Test",
            "steps": [
                {"time_ms": 0, "set": {"pump": True}},
                {"time_ms": 10, "set": {"heater": True}},
            ],
            "loops": [
                {
                    "start_ms": 20,
                    "repeat": 2,
                    "period_ms": 10,
                    "steps": [
                        {"offset_ms": 0, "set": {"flow_stop": True}},
                        {"offset_ms": 5, "set": {"flow_stop": False, "air": True}},
                    ],
                }
            ],
        }

        normalized, timeline = compile_recipe(recipe)

        self.assertEqual(normalized["version"], 3)
        self.assertEqual(normalized["phases"]["while_printjob"], [])
        vacuum_sets = [
            action["outputs"]["vacuum"]
            for action in normalized["phases"]["before_printing"]
            if action["type"] == "set" and "vacuum" in action["outputs"]
        ]
        self.assertIn(True, vacuum_sets)
        self.assertEqual(
            timeline,
            [
                {"time_ms": 0, "set": {"pump": True}},
                {"time_ms": 10, "set": {"heater": True}},
                {"time_ms": 20, "set": {"flow_stop": True}},
                {"time_ms": 25, "set": {"flow_stop": False, "vacuum": True}},
                {"time_ms": 30, "set": {"flow_stop": True}},
                {"time_ms": 35, "set": {"flow_stop": False, "vacuum": True}},
            ],
        )

    def test_version_3_allows_empty_phases_and_expands_repeats(self):
        recipe = {
            "version": 3,
            "name": "Cycle",
            "phases": {
                "before_printing": [
                    {"type": "set", "outputs": {"pump": True}},
                    {"type": "wait", "duration_ms": 10},
                    {
                        "type": "repeat",
                        "count": 2,
                        "actions": [
                            {"type": "set", "outputs": {"vacuum": True}},
                            {"type": "wait", "duration_ms": 5},
                            {"type": "set", "outputs": {"vacuum": False}},
                        ],
                    },
                ],
                "while_printjob": [],
                "while_printing": [],
                "after_printjob": [],
            },
        }

        normalized, timeline, duration_ms = compile_recipe_phase(recipe, "before_printing")

        self.assertEqual(normalized["version"], 3)
        self.assertEqual(duration_ms, 20)
        self.assertEqual(
            timeline,
            [
                {"time_ms": 0, "set": {"pump": True}},
                {"time_ms": 10, "set": {"vacuum": True}},
                {"time_ms": 15, "set": {"vacuum": True}},
                {"time_ms": 20, "set": {"vacuum": False}},
            ],
        )

    def test_validation_rejects_unknown_outputs(self):
        with self.assertRaises(RecipeValidationError):
            validate_recipe(
                {
                    "version": 3,
                    "name": "Broken",
                    "phases": {"before_printing": [{"type": "set", "outputs": {"steam": True}}]},
                }
            )

    def test_validation_rejects_non_vacuum_in_while_phases(self):
        with self.assertRaises(RecipeValidationError):
            validate_recipe(
                {
                    "version": 3,
                    "name": "Broken",
                    "phases": {"while_printing": [{"type": "set", "outputs": {"pump": True}}]},
                }
            )

    def test_sequencer_runs_before_phase_outputs_and_stops_safe(self):
        marlin = FakeMarlin()
        hardware = FakeHardware()
        events = []
        sequencer = RelaySequencer(
            marlin,
            hardware=hardware,
            emit_event=lambda event, payload: events.append((event, payload)),
            sleep_interval_s=0.001,
        )
        recipe = {
            "version": 3,
            "name": "Latte",
            "phases": {
                "before_printing": [
                    {"type": "set", "outputs": {"pump": True, "heater": True}},
                    {"type": "wait", "duration_ms": 15},
                    {"type": "set", "outputs": {"vacuum": True, "flow_stop": True}},
                    {"type": "wait", "duration_ms": 15},
                    {"type": "set", "outputs": {"pump": False}},
                ],
                "while_printjob": [],
                "while_printing": [],
                "after_printjob": [],
            },
        }

        sequencer.start(recipe)
        self.assertTrue(sequencer.wait_for_idle(1))

        self.assertIn("M42 P29 S255", marlin.commands)
        self.assertIn("M42 P30 S255", marlin.commands)
        self.assertIn("M42 P28 S255", marlin.commands)
        self.assertGreaterEqual(marlin.commands.count("M42 P29 S0"), 1)
        self.assertGreaterEqual(marlin.commands.count("M42 P30 S0"), 1)
        self.assertGreaterEqual(marlin.commands.count("M42 P28 S0"), 1)
        self.assertNotIn("M42 P26 S255", marlin.commands)
        self.assertIn(("vacuum_relay", True), hardware.outputs)
        self.assertIn(("vacuum_relay", False), hardware.outputs)

        relay_updates = [payload for event, payload in events if event == "relay_update"]
        self.assertTrue(any(update["pump"] and update["heater"] for update in relay_updates))
        self.assertEqual(sequencer.get_state()["status"], "completed")

    def test_busy_start_raises(self):
        marlin = FakeMarlin()
        sequencer = RelaySequencer(marlin, sleep_interval_s=0.001)
        recipe = {
            "version": 3,
            "name": "Busy",
            "phases": {
                "before_printing": [
                    {"type": "set", "outputs": {"pump": True}},
                    {"type": "wait", "duration_ms": 100},
                    {"type": "set", "outputs": {"pump": False}},
                ]
            },
        }

        sequencer.start(recipe)
        time.sleep(0.01)
        with self.assertRaises(RelaySequencerBusyError):
            sequencer.start(recipe)
        sequencer.stop()
        self.assertTrue(sequencer.wait_for_idle(1))

    def test_manual_set_relays_updates_state(self):
        marlin = FakeMarlin()
        hardware = FakeHardware()
        sequencer = RelaySequencer(marlin, hardware=hardware)

        state = sequencer.set_relays({"pump": True, "air": True})

        self.assertTrue(state["relay_state"]["pump"])
        self.assertTrue(state["relay_state"]["vacuum"])
        self.assertIn("M42 P29 S255", marlin.commands)
        self.assertNotIn("M42 P28 S255", marlin.commands)
        self.assertIn(("vacuum_relay", True), hardware.outputs)

    def test_validation_rejects_removed_valve_output(self):
        with self.assertRaises(RecipeValidationError):
            validate_recipe(
                {
                    "version": 3,
                    "name": "No Valve",
                    "phases": {"before_printing": [{"type": "set", "outputs": {"valve": True}}]},
                }
            )


if __name__ == "__main__":
    unittest.main()
