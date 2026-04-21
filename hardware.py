import math
import threading
import time


class HardwareController:
    def __init__(self, config=None, emit_event=None, initialize=True):
        self.config = config
        self.emit_event = emit_event or (lambda event, payload: None)
        self.relays = {}
        self.config_error = None
        self._tof = None
        self._led = None
        self._led_color = None
        self._led_status = "ready"
        self._led_lock = threading.Lock()
        self._led_stop = threading.Event()
        self._led_thread = None
        self._cup_monitor = None
        self._cup_monitor_stop = threading.Event()
        self._gpio = None
        self._gpio_ready = False
        self._system_state_callback = None
        self._poll_thread = None
        self._poll_stop = threading.Event()
        self._input_states = {}
        self._input_state_lock = threading.Lock()
        self._case_led_enabled = bool(getattr(config, "case_led_enabled", False))
        self._case_led_output_active = False
        self._tank_led_pwm = None
        self._tank_servo_pwm = None
        self._tank_servo_connected = False
        self._tank_servo_angle = None
        self._tank_fault = None
        self._last_tank_signature = None
        if initialize:
            self.initialize()

    def initialize(self):
        self._tof = None
        self._led = None
        self._gpio = None
        self._gpio_ready = False
        self.config_error = None
        self._tank_fault = None
        self._last_tank_signature = None
        self._tank_servo_connected = False
        self._tank_servo_angle = None
        self._teardown_gpio_pwm()
        self._input_states = {name: False for name in getattr(self.config, "input_pins", {})}
        self.relays = {name: False for name in getattr(self.config, "output_pins", {})}
        self._init_gpio()
        self._init_tof()
        self._init_led()
        self._sync_case_led_output()
        self._ensure_led_thread()
        self._ensure_input_polling()
        self._sync_tank_state(force=True, emit_warning=True)
        if self.config_error:
            self.set_led_status("config_error")
        elif not self.get_system_enabled():
            self.set_led_status("system_off")
        else:
            self.set_led_status("ready")

    def set_system_state_callback(self, callback):
        self._system_state_callback = callback

    def update_config(self, config):
        self.config = config
        self._case_led_enabled = bool(getattr(config, "case_led_enabled", False))
        self.initialize()

    def _teardown_gpio_pwm(self):
        for pwm in (self._tank_led_pwm, self._tank_servo_pwm):
            if pwm is not None:
                try:
                    pwm.stop()
                except Exception:
                    pass
        self._tank_led_pwm = None
        self._tank_servo_pwm = None

    def _init_gpio(self):
        try:
            import RPi.GPIO as GPIO

            GPIO.setwarnings(False)
            GPIO.setmode(GPIO.BOARD)
            self._gpio = GPIO
            for name, pin in self.config.input_pins.items():
                pull = GPIO.PUD_UP if pin.pull_up else GPIO.PUD_DOWN
                GPIO.setup(pin.board_pin, GPIO.IN, pull_up_down=pull)
                self._input_states[name] = pin.is_active(GPIO.input(pin.board_pin))
            for pin in self.config.output_pins.values():
                GPIO.setup(pin.board_pin, GPIO.OUT, initial=GPIO.LOW)
            GPIO.setup(self.config.tank_led_pin_board, GPIO.OUT, initial=GPIO.LOW)
            self._tank_led_pwm = GPIO.PWM(self.config.tank_led_pin_board, 100)
            self._tank_led_pwm.start(0)
            GPIO.setup(self.config.tank_servo_pin_board, GPIO.OUT, initial=GPIO.LOW)
            self._tank_servo_pwm = GPIO.PWM(self.config.tank_servo_pin_board, 50)
            self._tank_servo_pwm.start(0)
            self._gpio_ready = True
        except Exception as exc:
            self._gpio = None
            self._gpio_ready = False
            self._mark_config_error(f"GPIO inputs not configured: {exc}")

    def _init_tof(self):
        try:
            import board
            import busio
            import adafruit_vl53l0x

            i2c = busio.I2C(board.SCL, board.SDA)
            self._tof = adafruit_vl53l0x.VL53L0X(
                i2c,
                address=getattr(self.config, "tof_i2c_address", 0x29),
            )
        except Exception as exc:
            self._mark_config_error(f"ToF sensor not configured: {exc}")

    def _init_led(self):
        try:
            from rpi_ws281x import Color, PixelStrip

            pin = getattr(self.config, "led_pin_bcm", 9)
            self._led_color = Color
            self._led = PixelStrip(1, pin, 800000, 10, False, 90, 0)
            self._led.begin()
        except Exception as exc:
            pin = getattr(self.config, "led_pin_bcm", 9)
            self._mark_config_error(f"WS2812 LED on BCM GPIO {pin} not configured: {exc}")

    def _mark_config_error(self, message):
        self.config_error = message if self.config_error is None else f"{self.config_error}; {message}"
        self.emit_event("warning", {"msg": message, "level": "error"})

    def read_tof_mm(self):
        if self._tof is None:
            raise NotImplementedError("ToF sensor integration is not configured yet.")
        try:
            return float(self._tof.range)
        except Exception as exc:
            self.set_led_status("config_error")
            raise RuntimeError(f"ToF read failed: {exc}") from exc

    def read_tof_payload(self, allow_simulated=True):
        try:
            distance_mm = float(self.read_tof_mm())
            simulated = False
        except NotImplementedError:
            if not allow_simulated:
                raise
            distance_mm = float(getattr(self.config, "default_tof_distance_mm", 0.0))
            simulated = True

        z_value = self.config.surface_z(distance_mm)
        return {
            "distance_mm": distance_mm,
            "raw_distance_mm": distance_mm,
            "z_mm": z_value,
            "cup_present": self.config.cup_present(distance_mm),
            "simulated": simulated,
            "offsets": self.config.offsets_payload(),
        }

    def set_led(self, red, green, blue):
        if not self.get_system_enabled():
            red = green = blue = 0
        red = max(0, min(255, int(red)))
        green = max(0, min(255, int(green)))
        blue = max(0, min(255, int(blue)))
        if self._led is None:
            return False
        self._led.setPixelColor(0, self._led_color(red, green, blue))
        self._led.show()
        return True

    def set_led_status(self, status):
        with self._led_lock:
            if not self.get_system_enabled():
                status = "system_off"
            elif self.config_error and status != "config_error":
                status = "config_error"
            self._led_status = status
        self.emit_event("led_status", {"status": status})
        self._ensure_led_thread()

    def get_led_status(self):
        with self._led_lock:
            return self._led_status

    def _ensure_led_thread(self):
        if self._led_thread is not None and self._led_thread.is_alive():
            return
        self._led_stop.clear()
        self._led_thread = threading.Thread(target=self._run_led_loop, daemon=True)
        self._led_thread.start()

    def _error_brightness(self, step):
        return 0.25 + 0.75 * ((math.sin(step / 7) + 1) / 2)

    def _set_tank_led_level(self, brightness, red_only=False):
        if self._tank_led_pwm is None:
            return False
        if not self.get_system_enabled():
            brightness = 0.0
        brightness = max(0.0, min(1.0, float(brightness)))
        self._tank_led_pwm.ChangeDutyCycle(round(brightness * 100.0, 1))
        return True

    def _run_led_loop(self):
        step = 0
        while not self._led_stop.is_set():
            status = self.get_led_status()
            if status == "system_off":
                self.set_led(0, 0, 0)
                self._set_tank_led_level(0.0)
                time.sleep(0.15)
                continue
            if status == "printing":
                self.set_led(255, 255, 255)
                head_delay = 0.2
                head_brightness = 1.0
            elif status == "finished_wait_cup":
                head_brightness = 1.0 - ((step % 30) / 29)
                if step % 30 == 29:
                    head_brightness = 1.0
                value = int(255 * head_brightness)
                self.set_led(value, value, value)
                head_delay = 0.08
            elif status == "config_error":
                head_brightness = self._error_brightness(step)
                self.set_led(int(255 * head_brightness), 0, 0)
                head_delay = 0.08
            else:
                head_brightness = 0.18 + 0.55 * ((math.sin(step / 10) + 1) / 2)
                value = int(255 * head_brightness)
                self.set_led(value, value, value)
                head_delay = 0.1

            tank = self.get_tank_state()
            if tank["fault"] or tank["status"] == "missing":
                self._set_tank_led_level(self._error_brightness(step))
            elif tank["present"]:
                self._set_tank_led_level(0.18)
            else:
                self._set_tank_led_level(0.0)

            step += 1
            time.sleep(head_delay)

    def _ensure_input_polling(self):
        if self._poll_thread is not None and self._poll_thread.is_alive():
            return
        self._poll_stop.clear()
        self._poll_thread = threading.Thread(target=self._poll_inputs_loop, daemon=True)
        self._poll_thread.start()

    def _poll_inputs_loop(self):
        while not self._poll_stop.is_set():
            if self._gpio_ready:
                for name, pin in self.config.input_pins.items():
                    try:
                        active = pin.is_active(self._gpio.input(pin.board_pin))
                    except Exception:
                        continue
                    self._update_input_state(name, active)
            time.sleep(0.05)

    def _update_input_state(self, name, active):
        active = bool(active)
        with self._input_state_lock:
            previous = self._input_states.get(name)
            if previous == active:
                return
            self._input_states[name] = active
        self._handle_input_change(name, active)

    def _handle_input_change(self, name, active):
        payload = self.get_hardware_state()
        if name == "system_switch":
            self._sync_case_led_output()
            self._sync_tank_state(force=True, emit_warning=False)
            self.set_led_status("ready" if self.get_system_enabled() else "system_off")
            self.emit_event("hardware_inputs", payload)
            self.emit_event(
                "server_status",
                {
                    "status": "system_enabled" if self.get_system_enabled() else "system_disabled",
                    "message": "System switch enabled" if self.get_system_enabled() else "System switch is off",
                },
            )
            if self._system_state_callback is not None:
                self._system_state_callback(self.get_system_enabled(), payload)
            return

        if name == "tank_present":
            self._sync_tank_state(force=True, emit_warning=True)
            self.emit_event("hardware_inputs", self.get_hardware_state())
            return

        self.emit_event(
            "quick_button",
            {
                "name": name,
                "pressed": active,
                "system_enabled": self.get_system_enabled(),
            },
        )
        self.emit_event("hardware_inputs", payload)

    def get_system_enabled(self):
        with self._input_state_lock:
            switch_closed = bool(self._input_states.get("system_switch", False))
        return not switch_closed

    def set_simulated_input_state(self, name, active):
        self._update_input_state(name, active)

    def get_quick_button_states(self):
        with self._input_state_lock:
            return {
                name: bool(self._input_states.get(name, False))
                for name in ("quick_button_1", "quick_button_2", "quick_button_3")
            }

    def set_case_led_enabled(self, enabled):
        self._case_led_enabled = bool(enabled)
        self._sync_case_led_output()
        self.emit_event("hardware_inputs", self.get_hardware_state())
        return self.get_hardware_state()

    def _write_output_pin(self, pin_name, enabled):
        pin = self.config.output_pins[pin_name]
        active = pin.output_value(enabled)
        self.relays[pin_name] = bool(enabled)
        if not self._gpio_ready:
            return False
        self._gpio.output(pin.board_pin, self._gpio.HIGH if active else self._gpio.LOW)
        return True

    def set_output_pin(self, pin_name, enabled):
        result = self._write_output_pin(pin_name, enabled)
        self.emit_event("hardware_inputs", self.get_hardware_state())
        return result

    def _sync_case_led_output(self):
        should_enable = self.get_system_enabled() and self._case_led_enabled
        self._case_led_output_active = bool(should_enable)

    def _angle_to_duty_cycle(self, angle):
        angle = max(0.0, min(180.0, float(angle)))
        return 2.5 + (angle / 18.0)

    def _move_tank_servo(self, angle, force=False):
        if not force and not self.get_system_enabled():
            raise RuntimeError("System switch is off")
        if self._tank_servo_pwm is None:
            raise NotImplementedError("Tank servo integration is not configured yet.")
        duty = self._angle_to_duty_cycle(angle)
        self._tank_servo_pwm.ChangeDutyCycle(duty)
        time.sleep(max(0.0, float(self.config.tank_servo_settle_ms)) / 1000.0)
        self._tank_servo_pwm.ChangeDutyCycle(0)
        self._tank_servo_angle = float(angle)

    def _tank_present(self):
        with self._input_state_lock:
            return bool(self._input_states.get("tank_present", False))

    def get_tank_state(self):
        if self._tank_fault:
            return {
                "present": self._tank_present(),
                "servo_connected": self._tank_servo_connected,
                "fault": True,
                "status": "fault",
                "message": self._tank_fault,
                "simulated": True,
                "angle": self._tank_servo_angle,
            }
        if not self.get_system_enabled():
            return {
                "present": self._tank_present(),
                "servo_connected": False,
                "fault": False,
                "status": "system_off",
                "message": "System switch is off",
                "simulated": False,
                "angle": self._tank_servo_angle,
            }
        present = self._tank_present()
        return {
            "present": present,
            "servo_connected": self._tank_servo_connected,
            "fault": False,
            "status": "inserted" if present and self._tank_servo_connected else ("missing" if not present else "connecting"),
            "message": "Tank inserted" if present else "Tank missing",
            "simulated": False,
            "angle": self._tank_servo_angle,
        }

    def _emit_tank_state(self, emit_warning=False, force=False):
        payload = self.get_tank_state()
        signature = (
            payload["present"],
            payload["servo_connected"],
            payload["fault"],
            payload["status"],
            payload["message"],
            payload["simulated"],
            payload["angle"],
        )
        changed = signature != self._last_tank_signature
        if changed or force:
            self._last_tank_signature = signature
            self.emit_event("tank_status", payload)
            if emit_warning and (payload["fault"] or payload["status"] == "missing"):
                self.emit_event("warning", {"msg": payload["message"], "level": "error"})
        return payload

    def _sync_tank_state(self, force=False, emit_warning=False):
        if not self.get_system_enabled():
            self._tank_servo_connected = False
            self._set_tank_led_level(0.0)
            return self._emit_tank_state(force=force)
        if self._tank_servo_pwm is None:
            self._tank_fault = "Tank servo not configured"
            return self._emit_tank_state(emit_warning=emit_warning, force=True)

        if self._tank_present():
            try:
                self._move_tank_servo(self.config.tank_servo_inserted_angle)
                self._tank_servo_connected = True
                self._tank_fault = None
            except Exception as exc:
                self._tank_servo_connected = False
                self._tank_fault = f"Tank servo move failed: {exc}"
                return self._emit_tank_state(emit_warning=emit_warning, force=True)
        else:
            try:
                self._move_tank_servo(self.config.tank_servo_removed_angle, force=True)
            except Exception:
                pass
            self._tank_servo_connected = False
        return self._emit_tank_state(emit_warning=emit_warning, force=force)

    def get_hardware_state(self):
        with self._input_state_lock:
            system_switch_closed = bool(self._input_states.get("system_switch", False))
        return {
            "system_enabled": not system_switch_closed,
            "system_switch_closed": system_switch_closed,
            "quick_buttons": self.get_quick_button_states(),
            "case_led_enabled": bool(self._case_led_enabled),
            "case_led_output_active": bool(self._case_led_output_active),
            "output_states": dict(self.relays),
            "tank_present": self._tank_present(),
        }

    def start_finished_wait_cup_monitor(self, emit_event=None, poll_interval_s=1.0, timeout_s=300.0):
        self._cup_monitor_stop.set()
        if self._cup_monitor is not None and self._cup_monitor.is_alive():
            self._cup_monitor.join(timeout=0.2)
        self._cup_monitor_stop.clear()
        self.set_led_status("finished_wait_cup")
        callback = emit_event or self.emit_event
        self._cup_monitor = threading.Thread(
            target=self._monitor_cup_removed,
            args=(callback, poll_interval_s, timeout_s),
            daemon=True,
        )
        self._cup_monitor.start()

    def _monitor_cup_removed(self, emit_event, poll_interval_s, timeout_s):
        deadline = time.monotonic() + timeout_s
        while not self._cup_monitor_stop.is_set() and time.monotonic() < deadline:
            try:
                payload = self.read_tof_payload(allow_simulated=True)
                emit_event("tof_reading", payload)
                if not payload["cup_present"]:
                    self.set_led_status("ready")
                    emit_event(
                        "server_status",
                        {"status": "cup_removed", "message": "Cup removed"},
                    )
                    return
            except Exception as exc:
                self._mark_config_error(f"Cup removal ToF check failed: {exc}")
                self.set_led_status("config_error")
                return
            time.sleep(poll_interval_s)
        self.set_led_status("ready")

    def set_servo_angle(self, angle):
        self._move_tank_servo(angle)
        inserted_delta = abs(float(angle) - float(self.config.tank_servo_inserted_angle))
        removed_delta = abs(float(angle) - float(self.config.tank_servo_removed_angle))
        self._tank_servo_connected = inserted_delta <= removed_delta
        self._emit_tank_state(force=True)

    def is_button_pressed(self):
        raise NotImplementedError("Button integration is not configured yet.")

    def set_relay(self, relay, enabled):
        return self.set_output_pin(relay, enabled)

    def close(self):
        self._cup_monitor_stop.set()
        self._poll_stop.set()
        self._led_stop.set()
        self.set_led(0, 0, 0)
        self._set_tank_led_level(0.0)
        for pin_name in list(self.relays):
            self._write_output_pin(pin_name, False)
        self._teardown_gpio_pwm()
        if self._gpio_ready:
            try:
                self._gpio.cleanup()
            except Exception:
                pass

