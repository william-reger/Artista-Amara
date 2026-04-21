import unittest

from gcode_gen import hatch_segments, paths_to_gcode


SQUARE_PATH = [
    {
        "closed": True,
        "points": [
            {"x": 0, "y": 0},
            {"x": 100, "y": 0},
            {"x": 100, "y": 100},
            {"x": 0, "y": 100},
        ],
    }
]


class GcodeGeneratorTest(unittest.TestCase):
    def test_square_polygon_creates_expected_hatch_lines(self):
        segments = hatch_segments([[(0, 0), (8, 0), (8, 8), (0, 8)]], 4)

        self.assertEqual(
            segments,
            [
                ((0.0, 0), (8.0, 0)),
                ((8.0, 4), (0.0, 4)),
            ],
        )

    def test_nozzle_defaults_to_hatch_spacing_and_can_be_overridden(self):
        default_gcode = paths_to_gcode(
            SQUARE_PATH,
            coordinate_size=100,
            cup_radius=50,
            nozzle_mm=4,
        )
        custom_gcode = paths_to_gcode(
            SQUARE_PATH,
            coordinate_size=100,
            cup_radius=50,
            nozzle_mm=4,
            hatch_spacing_mm=10,
        )

        default_draws = [line for line in default_gcode if line.startswith("G1 X")]
        custom_draws = [line for line in custom_gcode if line.startswith("G1 X")]
        self.assertEqual(len(default_draws), 20)
        self.assertEqual(len(custom_draws), 8)

    def test_gcode_uses_pump_and_stop_pins_in_draw_order(self):
        gcode = paths_to_gcode(
            SQUARE_PATH,
            coordinate_size=100,
            cup_radius=50,
            hatch_spacing_mm=80,
        )
        first_travel = next(index for index, line in enumerate(gcode) if line.startswith("G0 X"))
        first_draw = next(index for index, line in enumerate(gcode) if line.startswith("G1 X"))

        self.assertIn("M42 P30 S0", gcode)
        self.assertLess(gcode.index("M42 P28 S0", first_travel), gcode.index("M42 P29 S255", first_travel))
        self.assertLess(gcode.index("M42 P29 S255", first_travel), first_draw)
        self.assertLess(first_draw, gcode.index("M42 P29 S0", first_draw))
        self.assertLess(gcode.index("M42 P29 S0", first_draw), gcode.index("M42 P28 S255", first_draw))
        self.assertFalse(any("P27" in line for line in gcode), "case LED filament must stay UI-owned")
        self.assertFalse(any("P26" in line for line in gcode), "valve relay must stay sequencer-owned")

    def test_custom_pattern_cannot_reclaim_foam_relays(self):
        gcode = paths_to_gcode(
            SQUARE_PATH,
            coordinate_size=100,
            cup_radius=50,
            hatch_spacing_mm=80,
            pattern={
                "pins": {"heater": 30, "pump": 29, "flow_stop": 28, "vacuum": 23, "case_led": 27},
                "idle": {"heater": False, "pump": False, "flow_stop": True, "vacuum": False, "case_led": True},
                "draw": {"flow_stop": False, "pump": True, "vacuum": True, "case_led": True},
                "travel": {"pump": False, "flow_stop": True, "vacuum": False, "case_led": False},
            },
        )

        self.assertTrue(any("P28" in line for line in gcode), "flow-stop valve must stay print-owned")
        self.assertFalse(any("P23" in line for line in gcode), "vacuum relay must stay hardware-owned")
        self.assertFalse(any("P27" in line for line in gcode), "case LED filament must stay UI-owned")


if __name__ == "__main__":
    unittest.main()
