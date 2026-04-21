import json
from dataclasses import asdict, dataclass, field
from pathlib import Path


BOARD_TO_BCM = {
    3: 2,
    5: 3,
    7: 4,
    8: 14,
    10: 15,
    11: 17,
    12: 18,
    13: 27,
    15: 22,
    16: 23,
    18: 24,
    19: 10,
    21: 9,
    22: 25,
    23: 11,
    24: 8,
    26: 7,
    27: 0,
    28: 1,
    29: 5,
    31: 6,
    32: 12,
    33: 13,
    35: 19,
    36: 16,
    37: 26,
    38: 20,
    40: 21,
}


@dataclass(frozen=True)
class ControlPin:
    name: str
    pin: int
    active_high: bool = True

    def pwm_value(self, enabled):
        active = bool(enabled)
        if not self.active_high:
            active = not active
        return 255 if active else 0

    def m42(self, enabled):
        return f"M42 P{self.pin} S{self.pwm_value(enabled)}"


@dataclass(frozen=True)
class HeaderInputPin:
    name: str
    board_pin: int
    pull_up: bool = True
    active_low: bool = True
    bounce_time_ms: int = 80

    @property
    def bcm_pin(self):
        return BOARD_TO_BCM.get(self.board_pin)

    def is_active(self, raw_value):
        value = bool(raw_value)
        return not value if self.active_low else value


@dataclass(frozen=True)
class HeaderOutputPin:
    name: str
    board_pin: int
    active_high: bool = True

    @property
    def bcm_pin(self):
        return BOARD_TO_BCM.get(self.board_pin)

    def output_value(self, enabled):
        value = bool(enabled)
        return value if self.active_high else not value


@dataclass(frozen=True)
class MachineConfig:
    bed_width_mm: float = 200.0
    bed_height_mm: float = 200.0
    print_center_x_mm: float | None = None
    print_center_y_mm: float | None = None
    tof_offset_x_mm: float = 0.0
    tof_offset_y_mm: float = 0.0
    tof_offset_z_mm: float = 0.0
    default_tof_distance_mm: float = 0.0
    cup_present_threshold_mm: float = 90.0
    nozzle_mm: float = 4.0
    tof_i2c_address: int = 0x29
    led_pin_bcm: int = 9
    serial_port: str = "/dev/serial0"
    baudrate: int = 115200
    command_timeout_s: float = 2.0
    abort_command: str = "M410"
    case_led_enabled: bool = False
    tank_detect_pin_board: int = 24
    tank_led_pin_board: int = 19
    tank_servo_pin_board: int = 40
    tank_present_pull_up: bool = True
    tank_present_active_low: bool = True
    tank_servo_inserted_angle: float = 0.0
    tank_servo_removed_angle: float = 45.0
    tank_servo_settle_ms: int = 300
    pins: dict[str, ControlPin] = field(
        default_factory=lambda: {
            "heater": ControlPin("heater", 30, True),
            "pump": ControlPin("pump", 29, True),
            "flow_stop": ControlPin("flow_stop", 28, True),
            "case_led": ControlPin("case_led", 27, True),
        }
    )
    pin_aliases: dict[str, str] = field(
        default_factory=lambda: {
            "stop": "flow_stop",
            "led_filament": "case_led",
            "filament": "case_led",
            "case_light": "case_led",
        }
    )
    input_pins: dict[str, HeaderInputPin] = field(
        default_factory=lambda: {
            "system_switch": HeaderInputPin("system_switch", 7, True, True, 120),
            "tank_present": HeaderInputPin("tank_present", 24, True, True, 80),
            "quick_button_1": HeaderInputPin("quick_button_1", 29, True, True, 80),
            "quick_button_2": HeaderInputPin("quick_button_2", 31, True, True, 80),
            "quick_button_3": HeaderInputPin("quick_button_3", 26, True, True, 80),
        }
    )
    output_pins: dict[str, HeaderOutputPin] = field(
        default_factory=lambda: {
            "vacuum_relay": HeaderOutputPin("vacuum_relay", 23, True),
        }
    )

    @property
    def print_center(self):
        return (
            self.print_center_x_mm if self.print_center_x_mm is not None else self.bed_width_mm / 2,
            self.print_center_y_mm if self.print_center_y_mm is not None else self.bed_height_mm / 2,
        )

    @property
    def tof_probe_position(self):
        center_x, center_y = self.print_center
        return center_x - self.tof_offset_x_mm, center_y - self.tof_offset_y_mm

    @property
    def control_outputs(self):
        outputs = []
        for name in ("pump", "heater", "flow_stop"):
            if self.resolve_optional_pin_name(name) in self.pins:
                outputs.append(name)
        if "vacuum_relay" in self.output_pins:
            outputs.append("vacuum")
        return tuple(outputs)

    def resolve_pin_name(self, name):
        key = str(name).strip().lower()
        key = self.pin_aliases.get(key, key)
        if key not in self.pins:
            raise KeyError(key)
        return key

    def resolve_optional_pin_name(self, name):
        key = str(name).strip().lower()
        return self.pin_aliases.get(key, key)

    def surface_z(self, distance_mm):
        return float(distance_mm) + self.tof_offset_z_mm

    def cup_present(self, distance_mm):
        return float(distance_mm) <= float(self.cup_present_threshold_mm)

    def offsets_payload(self):
        return {
            "x_mm": self.tof_offset_x_mm,
            "y_mm": self.tof_offset_y_mm,
            "z_mm": self.tof_offset_z_mm,
        }

    def hardware_inputs_payload(self):
        return {
            name: {
                "board_pin": pin.board_pin,
                "bcm_pin": pin.bcm_pin,
                "pull_up": pin.pull_up,
                "active_low": pin.active_low,
                "bounce_time_ms": pin.bounce_time_ms,
            }
            for name, pin in self.input_pins.items()
        }

    def hardware_outputs_payload(self):
        return {
            name: {
                "board_pin": pin.board_pin,
                "bcm_pin": pin.bcm_pin,
                "active_high": pin.active_high,
            }
            for name, pin in self.output_pins.items()
        }

    def tank_config_payload(self):
        return {
            "detect_pin_board": self.tank_detect_pin_board,
            "detect_pin_bcm": BOARD_TO_BCM.get(self.tank_detect_pin_board),
            "led_pin_board": self.tank_led_pin_board,
            "led_pin_bcm": BOARD_TO_BCM.get(self.tank_led_pin_board),
            "servo_pin_board": self.tank_servo_pin_board,
            "servo_pin_bcm": BOARD_TO_BCM.get(self.tank_servo_pin_board),
            "present_pull_up": self.tank_present_pull_up,
            "present_active_low": self.tank_present_active_low,
            "servo_inserted_angle": self.tank_servo_inserted_angle,
            "servo_removed_angle": self.tank_servo_removed_angle,
            "servo_settle_ms": self.tank_servo_settle_ms,
        }

    def machine_config_payload(self, tof=None, tank=None):
        payload = {
            "bed_width_mm": self.bed_width_mm,
            "bed_height_mm": self.bed_height_mm,
            "print_center_x_mm": self.print_center[0],
            "print_center_y_mm": self.print_center[1],
            "configured_print_center_x_mm": self.print_center_x_mm,
            "configured_print_center_y_mm": self.print_center_y_mm,
            "tof_offset_x_mm": self.tof_offset_x_mm,
            "tof_offset_y_mm": self.tof_offset_y_mm,
            "tof_offset_z_mm": self.tof_offset_z_mm,
            "cup_present_threshold_mm": self.cup_present_threshold_mm,
            "nozzle_mm": self.nozzle_mm,
            "tof_i2c_address": self.tof_i2c_address,
            "led_pin_bcm": self.led_pin_bcm,
            "case_led_enabled": self.case_led_enabled,
            "tank_servo_inserted_angle": self.tank_servo_inserted_angle,
            "tank_servo_removed_angle": self.tank_servo_removed_angle,
            "tank_servo_settle_ms": self.tank_servo_settle_ms,
            "tank": self.tank_config_payload(),
            "inputs": self.hardware_inputs_payload(),
            "outputs": self.hardware_outputs_payload(),
        }
        if tof is not None:
            payload["tof"] = tof
        if tank is not None:
            payload["tank_state"] = tank
        return payload

    def update_from_settings(self, settings):
        updates = dict(settings or {})
        if "cup_present_threshold_mm" in updates and float(updates["cup_present_threshold_mm"]) <= 0:
            raise ValueError("cup_present_threshold_mm must be greater than 0")
        if "nozzle_mm" in updates and float(updates["nozzle_mm"]) <= 0:
            raise ValueError("nozzle_mm must be greater than 0")
        if "tank_servo_settle_ms" in updates and int(updates["tank_servo_settle_ms"]) < 0:
            raise ValueError("tank_servo_settle_ms must be non-negative")

        current = dict(self.__dict__)
        if "pins" in updates:
            updates["pins"] = _coerce_control_pins(updates["pins"], current["pins"])
        if "input_pins" in updates:
            updates["input_pins"] = _coerce_input_pins(updates["input_pins"], current["input_pins"])
        if "output_pins" in updates:
            updates["output_pins"] = _coerce_output_pins(updates["output_pins"], current["output_pins"])
        current.update(updates)
        return MachineConfig(**current)

    def pin_command(self, name, enabled):
        return self.pins[self.resolve_pin_name(name)].m42(enabled)

    def is_pump_command(self, line, enabled):
        pin = self.pins[self.resolve_pin_name("pump")]
        expected = pin.m42(enabled).upper()
        return " ".join(line.strip().upper().split()) == expected


def _coerce_control_pins(raw_pins, defaults):
    if not isinstance(raw_pins, dict):
        return defaults
    pins = dict(defaults)
    for name, value in raw_pins.items():
        normalized_name = str(name).strip().lower()
        normalized_name = {
            "stop": "flow_stop",
            "led_filament": "case_led",
            "filament": "case_led",
            "case_light": "case_led",
        }.get(normalized_name, normalized_name)
        if normalized_name in {"vacuum", "air", "valve"}:
            continue
        if normalized_name not in pins:
            continue
        if isinstance(value, ControlPin):
            pins[normalized_name] = ControlPin(normalized_name, pins[normalized_name].pin, value.active_high)
            continue
        if not isinstance(value, dict):
            raise ValueError(f"pin {name} must be an object")
        pins[normalized_name] = ControlPin(
            normalized_name,
            pins[normalized_name].pin,
            bool(value.get("active_high", True)),
        )
    return pins


def _coerce_input_pins(raw_pins, defaults):
    if not isinstance(raw_pins, dict):
        return defaults
    pins = dict(defaults)
    for name, value in raw_pins.items():
        if isinstance(value, HeaderInputPin):
            pins[name] = value
            continue
        if not isinstance(value, dict):
            raise ValueError(f"input pin {name} must be an object")
        pins[name] = HeaderInputPin(
            str(value.get("name", name)),
            int(value.get("board_pin", value.get("pin"))),
            bool(value.get("pull_up", True)),
            bool(value.get("active_low", True)),
            int(value.get("bounce_time_ms", 80)),
        )
    return pins


def _coerce_output_pins(raw_pins, defaults):
    if not isinstance(raw_pins, dict):
        return defaults
    pins = dict(defaults)
    for name, value in raw_pins.items():
        normalized_name = {
            "case_led_relay": "vacuum_relay",
            "vacuum": "vacuum_relay",
        }.get(str(name).strip().lower(), str(name).strip().lower())
        if normalized_name not in pins:
            continue
        if isinstance(value, HeaderOutputPin):
            pins[normalized_name] = HeaderOutputPin(normalized_name, value.board_pin, value.active_high)
            continue
        if not isinstance(value, dict):
            raise ValueError(f"output pin {name} must be an object")
        pins[normalized_name] = HeaderOutputPin(
            normalized_name,
            int(value.get("board_pin", value.get("pin"))),
            bool(value.get("active_high", True)),
        )
    return pins


def load_machine_config(path):
    target = Path(path)
    if not target.exists():
        return MachineConfig()

    payload = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("machine config must be a JSON object")

    return MachineConfig().update_from_settings(payload)


def save_machine_config(config, path):
    target = Path(path)
    payload = {
        "bed_width_mm": config.bed_width_mm,
        "bed_height_mm": config.bed_height_mm,
        "print_center_x_mm": config.print_center_x_mm,
        "print_center_y_mm": config.print_center_y_mm,
        "tof_offset_x_mm": config.tof_offset_x_mm,
        "tof_offset_y_mm": config.tof_offset_y_mm,
        "tof_offset_z_mm": config.tof_offset_z_mm,
        "cup_present_threshold_mm": config.cup_present_threshold_mm,
        "nozzle_mm": config.nozzle_mm,
        "tof_i2c_address": config.tof_i2c_address,
        "led_pin_bcm": config.led_pin_bcm,
        "case_led_enabled": config.case_led_enabled,
        "tank_detect_pin_board": config.tank_detect_pin_board,
        "tank_led_pin_board": config.tank_led_pin_board,
        "tank_servo_pin_board": config.tank_servo_pin_board,
        "tank_present_pull_up": config.tank_present_pull_up,
        "tank_present_active_low": config.tank_present_active_low,
        "tank_servo_inserted_angle": config.tank_servo_inserted_angle,
        "tank_servo_removed_angle": config.tank_servo_removed_angle,
        "tank_servo_settle_ms": config.tank_servo_settle_ms,
        "pins": {name: asdict(pin) for name, pin in config.pins.items()},
        "input_pins": {name: asdict(pin) for name, pin in config.input_pins.items()},
        "output_pins": {name: asdict(pin) for name, pin in config.output_pins.items()},
    }
    target.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
