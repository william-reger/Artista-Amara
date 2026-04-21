import math
import re
from dataclasses import dataclass


COMMAND_RE = re.compile(r"([ML])\s*(-?\d+(?:\.\d+)?)\s*,?\s*(-?\d+(?:\.\d+)?)", re.IGNORECASE)

DEFAULT_PRINT_DIAMETER_MM = 80.0
DEFAULT_NOZZLE_MM = 4.0
DEFAULT_DRAW_FEED = 1200
DEFAULT_TRAVEL_FEED = 3000
DEFAULT_DEVICE_PATTERN = {
    "pins": {
        "heater": 30,
        "pump": 29,
        "flow_stop": 28,
    },
    "idle": {
        "heater": False,
        "pump": False,
        "flow_stop": True,
    },
    "draw": {
        "flow_stop": False,
        "pump": True,
    },
    "travel": {
        "pump": False,
        "flow_stop": True,
    },
}


@dataclass(frozen=True)
class GeneratorSettings:
    coordinate_size: float = 768.0
    cup_radius: float = 360.96
    cup_scale: float = 1.0
    print_diameter_mm: float = DEFAULT_PRINT_DIAMETER_MM
    print_center_x_mm: float = DEFAULT_PRINT_DIAMETER_MM / 2.0
    print_center_y_mm: float = DEFAULT_PRINT_DIAMETER_MM / 2.0
    nozzle_mm: float = DEFAULT_NOZZLE_MM
    hatch_spacing_mm: float | None = None
    draw_feed: int = DEFAULT_DRAW_FEED
    travel_feed: int = DEFAULT_TRAVEL_FEED
    pattern: dict | None = None

    @property
    def spacing_mm(self):
        return self.nozzle_mm if self.hatch_spacing_mm is None else self.hatch_spacing_mm


def svg_to_gcode(svg_text, scale=1.0, feed_rate=DEFAULT_DRAW_FEED):
    """Convert simple SVG M/L paths into starter G-code for legacy clients."""
    gcode = [
        "G21",
        "G90",
        f"G1 F{int(feed_rate)}",
    ]

    for command, x_value, y_value in COMMAND_RE.findall(svg_text):
        x = float(x_value) * scale
        y = float(y_value) * scale
        move = "G0" if command.upper() == "M" else "G1"
        gcode.append(f"{move} X{x:.3f} Y{y:.3f}")

    gcode.append("M400")
    return gcode


def paths_to_gcode(paths, **kwargs):
    settings = GeneratorSettings(**kwargs)
    polygon_paths = _prepare_polygons(paths, settings)
    segments = hatch_segments(polygon_paths, settings.spacing_mm)
    pattern = _merge_pattern(settings.pattern)

    gcode = ["G21", "G90"]
    gcode.extend(_commands_for_state(pattern, pattern["idle"]))

    for segment in segments:
        start, end = segment
        gcode.append(
            f"G0 X{_format_number(start[0])} Y{_format_number(start[1])} F{int(settings.travel_feed)}"
        )
        gcode.extend(_commands_for_state(pattern, pattern["draw"]))
        gcode.append(
            f"G1 X{_format_number(end[0])} Y{_format_number(end[1])} F{int(settings.draw_feed)}"
        )
        gcode.extend(_commands_for_state(pattern, pattern["travel"]))

    gcode.append("M400")
    gcode.extend(_commands_for_state(pattern, pattern["idle"]))
    return gcode


def hatch_segments(polygons, spacing_mm):
    if spacing_mm <= 0:
        raise ValueError("hatch_spacing_mm must be greater than zero")

    clean_polygons = [polygon for polygon in polygons if len(polygon) >= 3]
    if not clean_polygons:
        return []

    min_y = min(point[1] for polygon in clean_polygons for point in polygon)
    max_y = max(point[1] for polygon in clean_polygons for point in polygon)
    start_y = math.ceil(min_y / spacing_mm) * spacing_mm
    segments = []
    row = 0
    y = start_y

    while y <= max_y + 1e-9:
        intersections = []
        for polygon in clean_polygons:
            intersections.extend(_scanline_intersections(polygon, y))

        intersections = _dedupe_sorted(intersections)
        row_segments = []
        for index in range(0, len(intersections) - 1, 2):
            left = intersections[index]
            right = intersections[index + 1]
            if right - left > 0.01:
                row_segments.append(((left, y), (right, y)))

        if row % 2 == 1:
            row_segments = [(end, start) for start, end in reversed(row_segments)]

        segments.extend(row_segments)
        row += 1
        y = start_y + row * spacing_mm

    return segments


def _prepare_polygons(paths, settings):
    if not isinstance(paths, list):
        raise ValueError("paths must be a list")

    polygons = []
    for path in paths:
        if not isinstance(path, dict) or not path.get("closed", False):
            continue

        raw_points = path.get("points", [])
        points = []
        for point in raw_points:
            try:
                points.append(_canvas_point_to_mm(float(point["x"]), float(point["y"]), settings))
            except (KeyError, TypeError, ValueError):
                raise ValueError("path points must contain numeric x and y values") from None

        if len(points) >= 3:
            if _same_mm_point(points[0], points[-1]):
                points = points[:-1]
            polygons.append(points)

    if not polygons:
        raise ValueError("at least one closed path is required")

    return polygons


def _canvas_point_to_mm(x, y, settings):
    center = settings.coordinate_size / 2.0
    scaled_x = center + (x - center) * settings.cup_scale
    scaled_y = center + (y - center) * settings.cup_scale
    mm_per_unit = settings.print_diameter_mm / (settings.cup_radius * 2.0)
    return (
        settings.print_center_x_mm + (scaled_x - center) * mm_per_unit,
        settings.print_center_y_mm + (scaled_y - center) * mm_per_unit,
    )


def _scanline_intersections(polygon, y):
    intersections = []
    for index, current in enumerate(polygon):
        next_point = polygon[(index + 1) % len(polygon)]
        x1, y1 = current
        x2, y2 = next_point

        if abs(y2 - y1) < 1e-9:
            continue

        lower = min(y1, y2)
        upper = max(y1, y2)
        if y < lower or y >= upper:
            continue

        ratio = (y - y1) / (y2 - y1)
        intersections.append(x1 + ratio * (x2 - x1))

    return intersections


def _dedupe_sorted(values):
    result = []
    for value in sorted(values):
        if not result or abs(result[-1] - value) > 1e-6:
            result.append(value)
    return result


def _merge_pattern(pattern):
    merged = {
        "pins": dict(DEFAULT_DEVICE_PATTERN["pins"]),
        "idle": dict(DEFAULT_DEVICE_PATTERN["idle"]),
        "draw": dict(DEFAULT_DEVICE_PATTERN["draw"]),
        "travel": dict(DEFAULT_DEVICE_PATTERN["travel"]),
    }

    if not isinstance(pattern, dict):
        return merged

    if isinstance(pattern.get("pins"), dict):
        for name in merged["pins"]:
            if name in pattern["pins"]:
                merged["pins"][name] = pattern["pins"][name]

    for section in ("idle", "draw", "travel"):
        if isinstance(pattern.get(section), dict):
            for name in merged["pins"]:
                if name in pattern[section]:
                    merged[section][name] = pattern[section][name]

    return merged


def _commands_for_state(pattern, state):
    commands = []
    pins = pattern["pins"]
    for name, enabled in state.items():
        if name not in pins:
            continue
        commands.append(f"M42 P{int(pins[name])} S{255 if bool(enabled) else 0}")
    return commands


def _format_number(value):
    return f"{float(value):.3f}"


def _same_mm_point(point_a, point_b):
    return abs(point_a[0] - point_b[0]) < 1e-6 and abs(point_a[1] - point_b[1]) < 1e-6
