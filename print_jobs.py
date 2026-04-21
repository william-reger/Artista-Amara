import threading
import time
import uuid

from machine_config import MachineConfig
from relay_sequencer import (
    RecipeValidationError,
    compile_recipe_phase,
    validate_recipe,
)


class PrintJobBusyError(RuntimeError):
    pass


class _PhaseRunner:
    def __init__(self, timeline, apply_changes, stop_event, sleep_interval_s=0.01):
        self.timeline = list(timeline)
        self.apply_changes = apply_changes
        self.stop_event = stop_event
        self.sleep_interval_s = float(sleep_interval_s)
        self._thread = None
        self._started_at = None

    def start(self):
        if not self.timeline:
            return
        self._started_at = time.monotonic()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self.stop_event.set()
        self.wait(1)

    def wait(self, timeout=None):
        if self._thread is not None:
            self._thread.join(timeout)
        return self._thread is None or not self._thread.is_alive()

    def _run(self):
        for event in self.timeline:
            if not self._wait_until(event["time_ms"]):
                return
            self.apply_changes(event["set"])

    def _wait_until(self, target_ms):
        while not self.stop_event.is_set():
            elapsed_ms = int(round((time.monotonic() - self._started_at) * 1000))
            if elapsed_ms >= target_ms:
                return True
            time.sleep(self.sleep_interval_s)
        return False


class PrintJobManager:
    def __init__(self, marlin, hardware, config=None, emit_event=None, sleep_interval_s=0.01):
        self.marlin = marlin
        self.hardware = hardware
        self.config = config or MachineConfig()
        self.emit_event = emit_event or (lambda event, payload: None)
        self.sleep_interval_s = float(sleep_interval_s)
        self._lock = threading.Lock()
        self._output_lock = threading.Lock()
        self._abort_event = threading.Event()
        self._thread = None
        self._active_job_id = None
        self._output_state = {name: False for name in self.config.control_outputs}
        self._printjob_vacuum = None
        self._printing_vacuum = None
        self._printing_window_active = False
        self._printjob_runner = None
        self._printing_runner = None
        self._printjob_stop_event = None
        self._printing_stop_event = None

    def is_active(self):
        return self._thread is not None and self._thread.is_alive()

    def start_print(self, gcode, recipe=None, recipe_name=None):
        if hasattr(self.hardware, "get_system_enabled") and not self.hardware.get_system_enabled():
            raise ValueError("System switch is off")

        lines = [str(line).strip() for line in gcode if str(line).strip()]
        if not lines:
            raise ValueError("gcode must contain at least one command")

        normalized_recipe = validate_recipe(recipe) if recipe else None

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise PrintJobBusyError("a print job is already active")

            job_id = uuid.uuid4().hex
            self._abort_event.clear()
            self._active_job_id = job_id
            self._thread = threading.Thread(
                target=self._run_job,
                args=(job_id, lines, normalized_recipe, recipe_name),
                daemon=True,
            )
            self._thread.start()
            return job_id

    def abort(self):
        self._abort_event.set()
        self._stop_parallel_phases()
        self.marlin.clear_queue()
        try:
            self.marlin.send_command(
                self.config.abort_command,
                timeout=self.config.command_timeout_s,
            )
        except Exception as exc:
            self.emit_event(
                "warning",
                {"msg": f"Marlin abort command failed: {exc}", "level": "warning"},
            )
        finally:
            self._safe_state(suppress_errors=True)
            self.emit_event("warning", {"msg": "Print aborted", "level": "warning"})

        return self._active_job_id

    def wait_for_idle(self, timeout=None):
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return thread is None or not thread.is_alive()

    def _run_job(self, job_id, gcode, recipe, recipe_name):
        completed = False
        try:
            self._reset_recipe_runtime()
            self._safe_state()
            self._run_recipe_phase_sync(recipe, "before_printing")
            self._emit_progress(0, 0, len(gcode))
            self._send_checked("G28")
            self._apply_tof_offset()

            self._start_printjob_phase(recipe, recipe_name)
            for index, line in enumerate(gcode, start=1):
                self._raise_if_aborted()
                self._send_print_line(line, recipe, recipe_name)
                self._emit_progress(index, index, len(gcode))

            self._stop_printing_phase()
            self._stop_printjob_phase()
            self._run_recipe_phase_sync(recipe, "after_printjob")
            completed = True
            self.emit_event(
                "print_progress",
                {"percent": 100, "line": len(gcode), "total": len(gcode)},
            )
        except Exception as exc:
            self._stop_parallel_phases()
            level = "warning" if self._abort_event.is_set() else "error"
            self.emit_event("warning", {"msg": str(exc), "level": level})
        finally:
            self._stop_parallel_phases()
            self._safe_state(suppress_errors=not completed)
            with self._lock:
                if self._active_job_id == job_id:
                    self._active_job_id = None

    def _run_recipe_phase_sync(self, recipe, phase):
        if not recipe:
            return

        _normalized, timeline, _duration_ms = compile_recipe_phase(recipe, phase)
        started_at = time.monotonic()
        for event in timeline:
            self._wait_until_recipe_time(started_at, event["time_ms"])
            self._apply_recipe_outputs(event["set"])
            self.emit_event(
                "recipe_phase",
                {"phase": phase, "elapsed_ms": event["time_ms"]},
            )

    def _wait_until_recipe_time(self, started_at, target_ms):
        while True:
            self._raise_if_aborted()
            elapsed_ms = int(round((time.monotonic() - started_at) * 1000))
            if elapsed_ms >= target_ms:
                return
            time.sleep(self.sleep_interval_s)

    def _start_printjob_phase(self, recipe, recipe_name):
        if not recipe:
            return
        _normalized, timeline, _duration_ms = compile_recipe_phase(recipe, "while_printjob")
        if not timeline:
            return

        self._printjob_stop_event = threading.Event()
        self._printjob_runner = _PhaseRunner(
            timeline,
            lambda changes: self._apply_vacuum_phase_changes("printjob", changes),
            self._printjob_stop_event,
            sleep_interval_s=self.sleep_interval_s,
        )
        self._printjob_runner.start()
        self.emit_event("recipe_phase", {"phase": "while_printjob", "recipe": recipe_name, "status": "running"})

    def _stop_printjob_phase(self):
        if self._printjob_runner is not None:
            self._printjob_runner.stop()
        self._printjob_runner = None
        self._printjob_stop_event = None
        with self._output_lock:
            self._printjob_vacuum = None
            self._apply_vacuum_owner_locked()

    def _start_printing_phase(self, recipe, recipe_name):
        self._stop_printing_phase()
        with self._output_lock:
            self._printing_window_active = True
            self._printing_vacuum = None
            self._apply_vacuum_owner_locked()

        if not recipe:
            return
        _normalized, timeline, _duration_ms = compile_recipe_phase(recipe, "while_printing")
        if not timeline:
            return

        self._printing_stop_event = threading.Event()
        self._printing_runner = _PhaseRunner(
            timeline,
            lambda changes: self._apply_vacuum_phase_changes("printing", changes),
            self._printing_stop_event,
            sleep_interval_s=self.sleep_interval_s,
        )
        self._printing_runner.start()
        self.emit_event("recipe_phase", {"phase": "while_printing", "recipe": recipe_name, "status": "running"})

    def _stop_printing_phase(self):
        if self._printing_runner is not None:
            self._printing_runner.stop()
        self._printing_runner = None
        self._printing_stop_event = None
        with self._output_lock:
            self._printing_window_active = False
            self._printing_vacuum = None
            self._apply_vacuum_owner_locked()

    def _stop_parallel_phases(self):
        self._stop_printing_phase()
        self._stop_printjob_phase()

    def _reset_recipe_runtime(self):
        self._stop_parallel_phases()
        with self._output_lock:
            self._printjob_vacuum = None
            self._printing_vacuum = None
            self._printing_window_active = False

    def _apply_vacuum_phase_changes(self, owner, changes):
        if "vacuum" not in changes:
            return
        with self._output_lock:
            if owner == "printing":
                self._printing_vacuum = bool(changes["vacuum"])
            else:
                self._printjob_vacuum = bool(changes["vacuum"])
            self._apply_vacuum_owner_locked()

    def _apply_vacuum_owner_locked(self):
        if self._printing_window_active:
            desired = False if self._printing_vacuum is None else bool(self._printing_vacuum)
        else:
            desired = False if self._printjob_vacuum is None else bool(self._printjob_vacuum)
        self._apply_output_locked("vacuum", desired)

    def _apply_recipe_outputs(self, changes, allow_abort=False):
        for name, enabled in changes.items():
            if name == "vacuum":
                with self._output_lock:
                    self._apply_output_locked("vacuum", bool(enabled))
            else:
                self._apply_marlin_output(name, bool(enabled), allow_abort=allow_abort)

    def _apply_marlin_output(self, name, enabled, allow_abort=False):
        if not allow_abort:
            self._raise_if_aborted()
        line = self.config.pin_command(name, enabled)
        self.marlin.send_command(line, timeout=self.config.command_timeout_s)
        self.emit_event("marlin_status", {"msg": f">> {line}"})
        with self._output_lock:
            if self._output_state.get(name) != bool(enabled):
                self._output_state[name] = bool(enabled)
                self.emit_event("relay_update", dict(self._output_state))

    def _apply_output_locked(self, name, enabled):
        enabled = bool(enabled)
        if self._output_state.get(name) == enabled:
            return
        if name == "vacuum":
            if self.hardware is None:
                raise RecipeValidationError("vacuum hardware output is not configured")
            self.hardware.set_output_pin("vacuum_relay", enabled)
        else:
            self.marlin.send_command(
                self.config.pin_command(name, enabled),
                timeout=self.config.command_timeout_s,
            )
        self._output_state[name] = enabled
        self.emit_event("relay_update", dict(self._output_state))

    def _apply_tof_offset(self):
        probe_x, probe_y = self.config.tof_probe_position
        self._send_checked(f"G0 X{probe_x:.3f} Y{probe_y:.3f}")

        try:
            distance_mm = self.hardware.read_tof_mm()
        except NotImplementedError:
            distance_mm = self.config.default_tof_distance_mm
            self.emit_event(
                "warning",
                {
                    "msg": "ToF sensor not configured; using default Z offset",
                    "level": "warning",
                },
            )

        z_value = self.config.surface_z(distance_mm)
        self.emit_event(
            "tof_reading",
            {
                "distance_mm": float(distance_mm),
                "offset_x_mm": self.config.tof_offset_x_mm,
                "offset_y_mm": self.config.tof_offset_y_mm,
                "offset_z_mm": self.config.tof_offset_z_mm,
                "probe_x_mm": probe_x,
                "probe_y_mm": probe_y,
                "z_mm": z_value,
            },
        )
        self._send_checked(f"G92 Z{z_value:.3f}")

    def _send_print_line(self, line, recipe, recipe_name):
        if self.config.is_pump_command(line, True):
            self._set_stop(False)
            self._send_checked(line)
            self._mark_output_state("pump", True)
            self._start_printing_phase(recipe, recipe_name)
            return

        if self.config.is_pump_command(line, False):
            self._send_checked(line)
            self._mark_output_state("pump", False)
            self._stop_printing_phase()
            self._set_stop(True)
            return

        self._send_checked(line)

    def _safe_state(self, suppress_errors=False):
        errors = []
        for name, enabled in (
            ("pump", False),
            ("heater", False),
            ("flow_stop", True),
        ):
            try:
                self._apply_marlin_output(name, enabled, allow_abort=True)
            except Exception as exc:
                if not suppress_errors:
                    raise
                errors.append(str(exc))
        try:
            with self._output_lock:
                self._printjob_vacuum = None
                self._printing_vacuum = None
                self._printing_window_active = False
                self._output_state["vacuum"] = None
                self._apply_output_locked("vacuum", False)
        except Exception as exc:
            if not suppress_errors:
                raise
            errors.append(str(exc))

        for error in errors:
            self.emit_event("warning", {"msg": error, "level": "warning"})

    def _set_stop(self, enabled, allow_abort=False):
        self._send_checked(self.config.pin_command("flow_stop", enabled), allow_abort)
        self._mark_output_state("flow_stop", enabled)

    def _send_checked(self, line, allow_abort=False):
        if not allow_abort:
            self._raise_if_aborted()
        responses = self.marlin.send_command(
            line,
            timeout=self.config.command_timeout_s,
        )
        self.emit_event("marlin_status", {"msg": f">> {line}"})
        return responses

    def _mark_output_state(self, name, enabled):
        with self._output_lock:
            if self._output_state.get(name) == bool(enabled):
                return
            self._output_state[name] = bool(enabled)
            self.emit_event("relay_update", dict(self._output_state))

    def _emit_progress(self, completed, line, total):
        percent = 100 if total == 0 else round((completed / total) * 100, 1)
        self.emit_event(
            "print_progress",
            {"percent": percent, "line": line, "total": total},
        )

    def _raise_if_aborted(self):
        if self._abort_event.is_set():
            raise RuntimeError("Print aborted")
