import json
import threading
import time
from pathlib import Path

from machine_config import MachineConfig


RELAY_OUTPUTS = ("pump", "heater", "flow_stop", "vacuum")
RELAY_ALIASES = {"air": "vacuum", "stop": "flow_stop"}
RECIPE_PHASES = ("before_printing", "while_printjob", "while_printing", "after_printjob")
PHASE_OUTPUTS = {
    "before_printing": RELAY_OUTPUTS,
    "while_printjob": ("vacuum",),
    "while_printing": ("vacuum",),
    "after_printjob": RELAY_OUTPUTS,
}


class RelaySequencerError(RuntimeError):
    pass


class RelaySequencerBusyError(RelaySequencerError):
    pass


class RecipeValidationError(ValueError):
    pass


def _normalize_output_name(name):
    key = str(name).strip().lower()
    key = RELAY_ALIASES.get(key, key)
    if key not in RELAY_OUTPUTS:
        raise RecipeValidationError(f"unknown output: {name}")
    return key


def _normalize_output_map(values, context, allowed_outputs=None):
    if not isinstance(values, dict) or not values:
        raise RecipeValidationError(f"{context} must define at least one output")

    allowed = set(allowed_outputs or RELAY_OUTPUTS)
    normalized = {}
    for name, enabled in values.items():
        output = _normalize_output_name(name)
        if output not in allowed:
            allowed_list = ", ".join(sorted(allowed))
            raise RecipeValidationError(f"{context}.{output} is not allowed in this phase; allowed: {allowed_list}")
        normalized[output] = bool(enabled)
    return normalized


def _require_non_negative_int(value, context, allow_zero=True):
    if isinstance(value, bool) or not isinstance(value, int):
        raise RecipeValidationError(f"{context} must be an integer")
    if value < 0 or (value == 0 and not allow_zero):
        comparator = "positive" if not allow_zero else "non-negative"
        raise RecipeValidationError(f"{context} must be {comparator}")
    return value


def _normalize_timeline_steps(steps, field_name="steps", time_key="time_ms"):
    if not isinstance(steps, list) or not steps:
        raise RecipeValidationError(f"{field_name} must be a non-empty list")

    normalized_steps = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            raise RecipeValidationError(f"{field_name}[{index}] must be an object")
        if time_key not in step:
            raise RecipeValidationError(f"{field_name}[{index}] missing {time_key}")
        if "set" not in step:
            raise RecipeValidationError(f"{field_name}[{index}] missing set")

        normalized_steps.append(
            {
                time_key: _require_non_negative_int(step[time_key], f"{field_name}[{index}].{time_key}"),
                "set": _normalize_output_map(step["set"], f"{field_name}[{index}].set"),
            }
        )
    return normalized_steps


def _normalize_version_2_recipe(recipe):
    name = str(recipe.get("name", "")).strip()
    if not name:
        raise RecipeValidationError("recipe name must not be empty")

    normalized_v2 = {
        "version": 2,
        "name": name,
        "steps": _normalize_timeline_steps(recipe.get("steps"), "steps", "time_ms"),
    }

    raw_loops = recipe.get("loops", [])
    if raw_loops is None:
        raw_loops = []
    if not isinstance(raw_loops, list):
        raise RecipeValidationError("loops must be a list")

    loops = []
    for index, loop in enumerate(raw_loops):
        if not isinstance(loop, dict):
            raise RecipeValidationError(f"loops[{index}] must be an object")

        start_ms = _require_non_negative_int(loop.get("start_ms"), f"loops[{index}].start_ms")
        repeat = _require_non_negative_int(loop.get("repeat"), f"loops[{index}].repeat", allow_zero=False)
        period_ms = _require_non_negative_int(loop.get("period_ms"), f"loops[{index}].period_ms", allow_zero=False)
        steps = _normalize_timeline_steps(loop.get("steps"), f"loops[{index}].steps", "offset_ms")

        loops.append(
            {
                "start_ms": start_ms,
                "repeat": repeat,
                "period_ms": period_ms,
                "steps": steps,
            }
        )

    normalized_v2["loops"] = loops
    _unused, timeline = _compile_version_2_timeline(normalized_v2)
    return {
        "version": 3,
        "name": name,
        "phases": {
            "before_printing": _timeline_to_actions(timeline),
            "while_printjob": [],
            "while_printing": [],
            "after_printjob": [],
        },
    }


def _compile_version_2_timeline(normalized):
    events = {}

    for step in normalized["steps"]:
        events.setdefault(step["time_ms"], {}).update(step["set"])

    for loop in normalized["loops"]:
        for repeat_index in range(loop["repeat"]):
            base_time = loop["start_ms"] + repeat_index * loop["period_ms"]
            for step in loop["steps"]:
                events.setdefault(base_time + step["offset_ms"], {}).update(step["set"])

    timeline = [
        {"time_ms": time_ms, "set": events[time_ms]}
        for time_ms in sorted(events)
    ]
    return normalized, timeline


def _timeline_to_actions(timeline):
    actions = []
    previous_ms = 0
    for event in timeline:
        time_ms = int(event["time_ms"])
        if time_ms > previous_ms:
            actions.append({"type": "wait", "duration_ms": time_ms - previous_ms})
        actions.append({"type": "set", "outputs": dict(event["set"])})
        previous_ms = time_ms
    return actions


def _normalize_actions(actions, phase, context, allow_empty=True):
    if actions is None:
        actions = []
    if not isinstance(actions, list):
        raise RecipeValidationError(f"{context} must be a list")
    if not actions and not allow_empty:
        raise RecipeValidationError(f"{context} must be a non-empty list")

    allowed_outputs = PHASE_OUTPUTS[phase]
    normalized = []
    for index, action in enumerate(actions):
        action_context = f"{context}[{index}]"
        if not isinstance(action, dict):
            raise RecipeValidationError(f"{action_context} must be an object")
        action_type = str(action.get("type", "")).strip().lower()

        if action_type == "set":
            normalized.append(
                {
                    "type": "set",
                    "outputs": _normalize_output_map(
                        action.get("outputs"),
                        f"{action_context}.outputs",
                        allowed_outputs=allowed_outputs,
                    ),
                }
            )
            continue

        if action_type == "wait":
            normalized.append(
                {
                    "type": "wait",
                    "duration_ms": _require_non_negative_int(
                        action.get("duration_ms"),
                        f"{action_context}.duration_ms",
                        allow_zero=False,
                    ),
                }
            )
            continue

        if action_type == "repeat":
            normalized.append(
                {
                    "type": "repeat",
                    "count": _require_non_negative_int(
                        action.get("count"),
                        f"{action_context}.count",
                        allow_zero=False,
                    ),
                    "actions": _normalize_actions(
                        action.get("actions"),
                        phase,
                        f"{action_context}.actions",
                        allow_empty=False,
                    ),
                }
            )
            continue

        raise RecipeValidationError(f"{action_context}.type must be set, wait, or repeat")

    return normalized


def validate_recipe(recipe):
    if not isinstance(recipe, dict):
        raise RecipeValidationError("recipe must be a JSON object")

    version = recipe.get("version", 2)
    if version == 2:
        return _normalize_version_2_recipe(recipe)
    if version != 3:
        raise RecipeValidationError("recipe version must be 2 or 3")

    name = str(recipe.get("name", "")).strip()
    if not name:
        raise RecipeValidationError("recipe name must not be empty")

    raw_phases = recipe.get("phases", {})
    if raw_phases is None:
        raw_phases = {}
    if not isinstance(raw_phases, dict):
        raise RecipeValidationError("phases must be an object")

    phases = {}
    for phase in RECIPE_PHASES:
        phases[phase] = _normalize_actions(raw_phases.get(phase, []), phase, f"phases.{phase}")

    return {"version": 3, "name": name, "phases": phases}


def _compile_actions(actions, start_ms=0):
    events = {}
    current_ms = int(start_ms)

    for action in actions:
        action_type = action["type"]
        if action_type == "set":
            events.setdefault(current_ms, {}).update(action["outputs"])
            continue
        if action_type == "wait":
            current_ms += action["duration_ms"]
            continue
        if action_type == "repeat":
            for _repeat_index in range(action["count"]):
                nested_events, current_ms = _compile_actions(action["actions"], current_ms)
                for time_ms, changes in nested_events.items():
                    events.setdefault(time_ms, {}).update(changes)
            continue
        raise RecipeValidationError(f"unknown action type: {action_type}")

    return events, current_ms


def compile_recipe_phase(recipe, phase="before_printing"):
    if phase not in RECIPE_PHASES:
        raise RecipeValidationError(f"unknown recipe phase: {phase}")

    normalized = validate_recipe(recipe)
    events, duration_ms = _compile_actions(normalized["phases"][phase], 0)
    timeline = [
        {"time_ms": time_ms, "set": events[time_ms]}
        for time_ms in sorted(events)
    ]
    return normalized, timeline, duration_ms


def compile_recipe(recipe, phase="before_printing"):
    normalized, timeline, _duration_ms = compile_recipe_phase(recipe, phase=phase)
    return normalized, timeline


def recipe_phase_has_actions(recipe, phase):
    normalized = validate_recipe(recipe)
    return bool(normalized["phases"].get(phase))


def load_recipe(path):
    with Path(path).open("r", encoding="utf-8") as recipe_file:
        return validate_recipe(json.load(recipe_file))


class RelaySequencer:
    def __init__(self, marlin, hardware=None, config=None, emit_event=None, sleep_interval_s=0.01):
        self.marlin = marlin
        self.hardware = hardware
        self.config = config or MachineConfig()
        self.emit_event = emit_event or (lambda event, payload: None)
        self.sleep_interval_s = float(sleep_interval_s)
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None
        self._state = {name: False for name in self.config.control_outputs}
        self._status = "idle"
        self._recipe_name = None
        self._error = None
        self._started_at = None
        self._timeline = []

    def start(self, recipe, recipe_name=None, phase="before_printing"):
        normalized, timeline, _duration_ms = compile_recipe_phase(recipe, phase=phase)
        name = recipe_name or normalized["name"]

        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RelaySequencerBusyError("a foam sequence is already active")

            self._stop_event.clear()
            self._timeline = timeline
            self._recipe_name = name
            self._error = None
            self._status = "running"
            self._started_at = time.monotonic()
            self._thread = threading.Thread(
                target=self._run_recipe,
                args=(name, timeline),
                daemon=True,
            )
            self._thread.start()

        self._emit_status("running", recipe_name=name, elapsed_ms=0)
        return name

    def stop(self):
        self._stop_event.set()
        return self.get_state()

    def wait_for_idle(self, timeout=None):
        thread = self._thread
        if thread is not None:
            thread.join(timeout)
        return thread is None or not thread.is_alive()

    def is_active(self):
        thread = self._thread
        return thread is not None and thread.is_alive()

    def set_relays(self, values):
        partial = _normalize_output_map(values, "relay state")
        with self._lock:
            self._apply_state_changes(partial)
            return self._snapshot_locked()

    def get_state(self):
        with self._lock:
            return self._snapshot_locked()

    def _run_recipe(self, recipe_name, timeline):
        final_status = "completed"
        final_error = None

        try:
            for event in timeline:
                self._wait_until(event["time_ms"])
                with self._lock:
                    self._apply_state_changes(event["set"])
                self._emit_status("running", recipe_name=recipe_name, elapsed_ms=event["time_ms"])

            with self._lock:
                self._apply_state_changes({name: False for name in self.config.control_outputs})
        except Exception as exc:
            final_error = str(exc)
            final_status = "stopped" if self._stop_event.is_set() else "error"
            with self._lock:
                self._safe_state_locked()
            if final_status == "error":
                self.emit_event("warning", {"msg": str(exc), "level": "error"})
        else:
            if self._stop_event.is_set():
                final_status = "stopped"
            with self._lock:
                self._safe_state_locked()
        finally:
            elapsed_ms = self._elapsed_ms()
            with self._lock:
                self._status = final_status
                self._error = final_error
                self._emit_status_locked(final_status, recipe_name=recipe_name, elapsed_ms=elapsed_ms)
                self._thread = None
                if final_status in {"completed", "stopped", "error"}:
                    self._recipe_name = None
                    self._started_at = None
                    self._timeline = []

    def _wait_until(self, target_ms):
        while True:
            self._raise_if_stopped()
            if self._elapsed_ms() >= target_ms:
                return
            time.sleep(self.sleep_interval_s)

    def _elapsed_ms(self):
        if self._started_at is None:
            return 0
        return max(0, int(round((time.monotonic() - self._started_at) * 1000)))

    def _raise_if_stopped(self):
        if self._stop_event.is_set():
            raise RelaySequencerError("Foam sequence stopped")

    def _safe_state_locked(self):
        self._apply_state_changes({name: False for name in self.config.control_outputs}, suppress_relay_emit=False)

    def _apply_state_changes(self, changes, suppress_relay_emit=False):
        changed = False
        for name, enabled in changes.items():
            resolved = _normalize_output_name(name)
            enabled = bool(enabled)
            if self._state.get(resolved) == enabled:
                continue
            if resolved == "vacuum":
                if self.hardware is None:
                    raise RelaySequencerError("vacuum hardware output is not configured")
                self.hardware.set_output_pin("vacuum_relay", enabled)
            else:
                self.marlin.send_command(
                    self.config.pin_command(resolved, enabled),
                    timeout=self.config.command_timeout_s,
                )
            self._state[resolved] = enabled
            changed = True

        if changed and not suppress_relay_emit:
            self.emit_event("relay_update", dict(self._state))

    def _snapshot_locked(self):
        return {
            "status": self._status,
            "recipe": self._recipe_name,
            "relay_state": dict(self._state),
            "elapsed_ms": self._elapsed_ms(),
            "error": self._error,
            "timeline_length": len(self._timeline),
        }

    def _emit_status(self, status, recipe_name=None, elapsed_ms=None):
        with self._lock:
            self._emit_status_locked(status, recipe_name=recipe_name, elapsed_ms=elapsed_ms)

    def _emit_status_locked(self, status, recipe_name=None, elapsed_ms=None):
        payload = {
            "status": status,
            "recipe": recipe_name if recipe_name is not None else self._recipe_name,
            "elapsed_ms": self._elapsed_ms() if elapsed_ms is None else int(elapsed_ms),
            "relay_state": dict(self._state),
        }
        if self._error:
            payload["error"] = self._error
        self.emit_event("foam_status", payload)
