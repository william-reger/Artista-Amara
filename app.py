#import eventlet
#eventlet.monkey_patch()

import json
import threading
from pathlib import Path
from uuid import uuid4

from flask import Flask, jsonify, request, send_from_directory
from flask_socketio import SocketIO, emit

from gcode_gen import paths_to_gcode, svg_to_gcode
from hardware import HardwareController
from machine_config import load_machine_config, save_machine_config
from marlin import MarlinUART
from print_jobs import PrintJobBusyError, PrintJobManager
from relay_sequencer import (
    RecipeValidationError,
    RelaySequencer,
    RelaySequencerBusyError,
    validate_recipe,
)
from usage_stats import UsageStatsStore


BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
RECIPE_DIR = BASE_DIR / "recipes"
SETTINGS_PATH = BASE_DIR / "machine_settings.json"
STATS_PATH = BASE_DIR / "usage_stats.json"
PRESET_DIR = BASE_DIR / "static" / "presets"
CUP_SIZES_MM = {
    "espresso": 45.0,
    "cappuchino": 70.0,
    "cafe_crema": 70.0,
}

app = Flask(__name__, static_folder="static", static_url_path="")
#socketio = SocketIO(app, cors_allowed_origins="*")
socketio = SocketIO(app, async_mode='threading', cors_allowed_origins="*")
UPLOAD_DIR.mkdir(exist_ok=True)
RECIPE_DIR.mkdir(exist_ok=True)

machine_config = load_machine_config(SETTINGS_PATH)


DEFAULT_CLEANING_RECIPE = {
    "version": 3,
    "name": "Cleaning",
    "phases": {
        "before_printing": [],
        "while_printjob": [],
        "while_printing": [],
        "after_printjob": [],
    },
}


class CleaningSession:
    def __init__(self):
        self._lock = threading.Lock()
        self._state = {
            "active": False,
            "running": False,
            "outputs": {
                "pump": False,
                "heater": False,
                "flow_stop": False,
                "vacuum": False,
            },
        }

    def get_state(self):
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self):
        return {
            "active": bool(self._state["active"]),
            "running": bool(self._state["running"]),
            "outputs": dict(self._state["outputs"]),
        }

    def set_running(self, running):
        with self._lock:
            self._state["running"] = bool(running)
            return self._snapshot_locked()

    def activate(self):
        with self._lock:
            self._state["active"] = True
            self._state["running"] = False
            return self._snapshot_locked()

    def deactivate(self):
        with self._lock:
            self._state["active"] = False
            self._state["running"] = False
            self._state["outputs"] = {name: False for name in self._state["outputs"]}
            return self._snapshot_locked()

    def is_active(self):
        with self._lock:
            return bool(self._state["active"])

    def set_output(self, name, enabled):
        with self._lock:
            self._state["outputs"][name] = bool(enabled)
            return self._snapshot_locked()


def ensure_cleaning_recipe():
    target = RECIPE_DIR / "Cleaning.json"
    if target.exists():
        return
    target.write_text(json.dumps(DEFAULT_CLEANING_RECIPE, indent=2) + "\n", encoding="utf-8")


def emit_machine_event(event, payload):
    socketio.emit(event, payload)


usage_stats = UsageStatsStore(STATS_PATH)
cleaning_session = CleaningSession()


def get_usage_stats_store():
    return app.config.get("USAGE_STATS", usage_stats)


def get_cleaning_session():
    return app.config.get("CLEANING_SESSION", cleaning_session)


def usage_stats_payload(payload=None):
    stats = payload if isinstance(payload, dict) else get_usage_stats_store().load()
    response = dict(stats)
    response["requires_cleaning"] = bool(response.get("prints_since_cleaning", 0))
    response["can_defer_cleaning"] = int(response.get("prints_since_cleaning", 0)) < 3
    return response


def emit_usage_stats(payload=None):
    emit_machine_event("usage_stats", usage_stats_payload(payload))


def emit_cleaning_state(payload=None):
    state = payload if isinstance(payload, dict) else get_cleaning_session().get_state()
    emit_machine_event("cleaning_state", state)


def apply_cleaning_output(name, enabled):
    config = current_machine_config()
    if name == "vacuum":
        get_hardware().set_output_pin("vacuum_relay", bool(enabled))
    else:
        get_marlin().send_command(
            config.pin_command(name, bool(enabled)),
            timeout=config.command_timeout_s,
        )
    relay_state = dict(get_cleaning_session().get_state()["outputs"])
    relay_state[name] = bool(enabled)
    emit_machine_event("relay_update", relay_state)
    return relay_state


def clear_cleaning_outputs():
    errors = []
    for output_name in ("pump", "heater", "flow_stop", "vacuum"):
        desired = False
        if output_name == "flow_stop":
            desired = True
        try:
            apply_cleaning_output(output_name, desired)
        except Exception as exc:
            errors.append(str(exc))
    if errors:
        raise RuntimeError("; ".join(errors))


def deactivate_cleaning_mode(clear_outputs=True):
    state = get_cleaning_session().deactivate()
    if clear_outputs:
        clear_cleaning_outputs()
    emit_cleaning_state(state)
    emit_hardware_state()
    return get_cleaning_session().get_state()


def handle_print_job_complete(job_id, recipe, recipe_name, moving_only):
    normalized_name = str(recipe_name or "").strip().lower()
    if moving_only and normalized_name == "cleaning":
        stats = get_usage_stats_store().record_cleaning_success()
        state = get_cleaning_session().activate()
        emit_usage_stats(stats)
        emit_cleaning_state(state)
        emit_machine_event(
            "cleaning_complete",
            {"job_id": job_id, "recipe": recipe_name or "Cleaning", "stats": usage_stats_payload(stats)},
        )
        emit_machine_event(
            "server_status",
            {"status": "cleaning_ready", "message": "Cleaning mode ready. Manual controls unlocked."},
        )
        return

    if moving_only:
        return

    stats = get_usage_stats_store().record_print_success()
    emit_usage_stats(stats)
    emit_machine_event("print_complete", {"job_id": job_id, "stats": usage_stats_payload(stats)})


marlin = MarlinUART(
    port=machine_config.serial_port,
    baudrate=machine_config.baudrate,
    timeout=machine_config.command_timeout_s,
    status_callback=lambda message: emit_machine_event("marlin_status", {"msg": message}),
)
hardware = HardwareController(machine_config, emit_event=emit_machine_event)
print_manager = PrintJobManager(
    marlin,
    hardware,
    config=machine_config,
    emit_event=emit_machine_event,
    on_job_complete=handle_print_job_complete,
)
relay_sequencer = RelaySequencer(
    marlin,
    hardware=hardware,
    config=machine_config,
    emit_event=emit_machine_event,
)

ensure_cleaning_recipe()


def get_print_manager():
    return app.config.get("PRINT_MANAGER", print_manager)


def get_hardware():
    return app.config.get("HARDWARE", hardware)


def get_relay_sequencer():
    return app.config.get("RELAY_SEQUENCER", relay_sequencer)


def get_marlin():
    return app.config.get("MARLIN", marlin)


def current_machine_config():
    return app.config.get("MACHINE_CONFIG", machine_config)


def sync_runtime_config(config):
    global machine_config
    machine_config = config
    app.config["MACHINE_CONFIG"] = config
    get_print_manager().config = config
    get_relay_sequencer().config = config
    if hasattr(get_relay_sequencer(), "hardware"):
        get_relay_sequencer().hardware = get_hardware()
    get_hardware().update_config(config)
    if hasattr(get_marlin(), "port"):
        get_marlin().port = config.serial_port
    if hasattr(get_marlin(), "baudrate"):
        get_marlin().baudrate = config.baudrate
    if hasattr(get_marlin(), "timeout"):
        get_marlin().timeout = config.command_timeout_s


sync_runtime_config(machine_config)


def emit_machine_config():
    emit_machine_event(
        "machine_config",
        current_machine_config().machine_config_payload(tank=get_hardware().get_tank_state()),
    )


def emit_hardware_state():
    emit_machine_event("hardware_inputs", get_hardware().get_hardware_state())


def apply_case_led_output():
    config = current_machine_config()
    state = get_hardware().set_case_led_enabled(config.case_led_enabled)
    enabled = bool(config.case_led_enabled and get_hardware().get_system_enabled())
    try:
        get_marlin().send_command(
            config.pin_command("case_led", enabled),
            timeout=config.command_timeout_s,
        )
    except Exception as exc:
        state = dict(state)
        state["case_led_command_error"] = str(exc)
        emit_machine_event(
            "warning",
            {"msg": f"Case LED command failed: {exc}", "level": "warning"},
        )
    emit_machine_event("hardware_inputs", state)
    return state


def handle_system_state_change(system_enabled, payload):
    if not system_enabled:
        try:
            deactivate_cleaning_mode(clear_outputs=True)
        except Exception:
            pass
        try:
            get_relay_sequencer().stop()
        except Exception:
            pass
        try:
            get_relay_sequencer().set_relays({name: False for name in current_machine_config().control_outputs})
        except Exception:
            pass
        try:
            if getattr(get_print_manager(), "is_active", lambda: False)():
                get_print_manager().abort()
        except Exception:
            pass
        emit_machine_event(
            "warning",
            {"msg": "System switch is off. Outputs and LEDs disabled.", "level": "warning"},
        )
    apply_case_led_output()
    emit_machine_event("hardware_inputs", get_hardware().get_hardware_state())
    emit_machine_event("tank_status", get_hardware().get_tank_state())


hardware.set_system_state_callback(handle_system_state_change)


def require_system_enabled():
    if get_hardware().get_system_enabled():
        return None
    return jsonify({"error": "System switch is off", "system_enabled": False}), 409


def require_tank_ready():
    state = get_hardware().get_tank_state()
    if not state.get("fault") and state.get("present"):
        return None
    message = state.get("message") or "Tank missing"
    emit_machine_event("warning", {"msg": message, "level": "error"})
    return jsonify({"error": message, "tank": state}), 409


def require_machine_idle():
    if getattr(get_print_manager(), "is_active", lambda: False)():
        return jsonify({"error": "Print job is active"}), 409
    if getattr(get_relay_sequencer(), "is_active", lambda: False)():
        return jsonify({"error": "Foam recipe is active"}), 409
    return None


def require_cleaning_active():
    if get_cleaning_session().is_active():
        return None
    return jsonify({"error": "Cleaning mode is not active"}), 409


def debug_guard_state():
    return {
        "system_enabled": bool(get_hardware().get_system_enabled()),
        "print_active": bool(getattr(get_print_manager(), "is_active", lambda: False)()),
        "foam_active": bool(getattr(get_relay_sequencer(), "is_active", lambda: False)()),
        "writes_allowed": bool(
            get_hardware().get_system_enabled()
            and not getattr(get_print_manager(), "is_active", lambda: False)()
            and not getattr(get_relay_sequencer(), "is_active", lambda: False)()
        ),
    }


def require_debug_write_access():
    guard = require_system_enabled()
    if guard is not None:
        return guard
    idle_guard = require_machine_idle()
    if idle_guard is not None:
        return idle_guard
    return None


def collect_debug_issues():
    issues = []
    hardware_state = get_hardware().get_hardware_state()
    marlin_state = get_marlin().get_debug_state() if hasattr(get_marlin(), "get_debug_state") else {}
    guards = debug_guard_state()

    gpio_info = hardware_state.get("gpio", {})
    if not gpio_info.get("ready"):
        issues.append(
            {
                "category": "GPIO",
                "severity": "error",
                "message": gpio_info.get("error") or "GPIO not initialized",
            }
        )
    if hardware_state.get("config_error"):
        issues.append(
            {
                "category": "Config",
                "severity": "error",
                "message": hardware_state["config_error"],
            }
        )
    if not guards["system_enabled"]:
        issues.append(
            {
                "category": "Safety",
                "severity": "warning",
                "message": "System switch is off",
            }
        )
    if guards["print_active"]:
        issues.append(
            {
                "category": "Safety",
                "severity": "warning",
                "message": "Print job is active; debug writes are locked",
            }
        )
    if guards["foam_active"]:
        issues.append(
            {
                "category": "Safety",
                "severity": "warning",
                "message": "Foam recipe is active; debug writes are locked",
            }
        )
    if marlin_state.get("last_error"):
        issues.append(
            {
                "category": "UART",
                "severity": "error" if marlin_state.get("last_error_type") != "timeout" else "warning",
                "message": marlin_state["last_error"],
            }
        )
    return issues


def get_debug_snapshot():
    relay_state = get_relay_sequencer().get_state()
    hardware_state = get_hardware().get_hardware_state()
    marlin_state = get_marlin().get_debug_state() if hasattr(get_marlin(), "get_debug_state") else {}
    return {
        "hardware": hardware_state,
        "relay_state": relay_state.get("relay_state", {}),
        "foam_state": relay_state,
        "tank_state": get_hardware().get_tank_state(),
        "marlin": marlin_state,
        "guards": debug_guard_state(),
        "issues": collect_debug_issues(),
        "cleaning_state": get_cleaning_session().get_state(),
        "usage_stats": usage_stats_payload(),
    }


def safe_recipe_path(name):
    safe_name = Path(str(name)).stem
    if not safe_name:
        raise ValueError("invalid recipe name")
    return RECIPE_DIR / f"{safe_name}.json"


def load_recipe_from_name(name):
    target = safe_recipe_path(name)
    if not target.exists():
        raise FileNotFoundError("recipe not found")
    try:
        return validate_recipe(json.loads(target.read_text(encoding="utf-8"))), target
    except json.JSONDecodeError as exc:
        raise RecipeValidationError(f"invalid recipe json: {exc}") from exc


@app.get("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/health")
def health():
    return jsonify({"status": "ok", "socketio": True})


@app.get("/api/machine-config")
def get_machine_config():
    payload = current_machine_config().machine_config_payload(tank=get_hardware().get_tank_state())
    try:
        payload["tof"] = get_hardware().read_tof_payload(allow_simulated=True)
    except Exception:
        pass
    payload.update(get_hardware().get_hardware_state())
    payload["tank_state"] = get_hardware().get_tank_state()
    return jsonify(payload)


@app.put("/api/machine-config")
def update_machine_config_endpoint():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({"error": "machine config must be a JSON object"}), 400
    try:
        config = current_machine_config().update_from_settings(payload)
        save_machine_config(config, SETTINGS_PATH)
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400

    sync_runtime_config(config)
    emit_machine_config()
    emit_hardware_state()
    emit_machine_event("tank_status", get_hardware().get_tank_state())
    response = config.machine_config_payload(tank=get_hardware().get_tank_state())
    response.update(get_hardware().get_hardware_state())
    response["tank_state"] = get_hardware().get_tank_state()
    return jsonify(response)


@app.get("/api/case-led")
def get_case_led():
    payload = get_hardware().get_hardware_state()
    return jsonify(payload)


@app.put("/api/case-led")
def set_case_led():
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict) or "enabled" not in payload:
        return jsonify({"error": "enabled must be provided"}), 400

    try:
        config = current_machine_config().update_from_settings({"case_led_enabled": bool(payload.get("enabled"))})
        save_machine_config(config, SETTINGS_PATH)
    except (TypeError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 400

    sync_runtime_config(config)
    state = apply_case_led_output()
    emit_machine_config()
    return jsonify(state)


@app.get("/api/recipes")
def list_recipes():
    recipes = sorted(path.stem for path in RECIPE_DIR.glob("*.json"))
    return jsonify({"recipes": recipes})


@app.get("/api/usage-stats")
def get_usage_stats():
    return jsonify(usage_stats_payload())


@app.get("/api/cleaning/state")
def get_cleaning_state():
    return jsonify(get_cleaning_session().get_state())


@app.get("/api/presets")
def list_presets():
    metadata = {}
    manifest_path = PRESET_DIR / "manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            entries = manifest if isinstance(manifest, list) else manifest.get("presets", [])
            for entry in entries:
                if isinstance(entry, dict) and entry.get("file"):
                    metadata[Path(str(entry["file"])).stem] = entry
        except (json.JSONDecodeError, OSError):
            metadata = {}

    presets = []
    for path in sorted(PRESET_DIR.glob("*.svg")):
        entry = metadata.get(path.stem, {})
        name = entry.get("name") or path.stem.replace("-", " ").replace("_", " ").title()
        presets.append(
            {
                "id": entry.get("id") or path.stem,
                "name": name,
                "caption": entry.get("caption") or "Filled SVG preset",
                "file": f"presets/{path.name}",
            }
        )
    return jsonify({"presets": presets})


@app.get("/api/recipes/<name>")
def get_recipe(name):
    try:
        recipe, _target = load_recipe_from_name(name)
        return jsonify(recipe)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except FileNotFoundError:
        return jsonify({"error": "recipe not found"}), 404
    except RecipeValidationError as exc:
        return jsonify({"error": str(exc)}), 500


@app.put("/api/recipes/<name>")
def save_recipe(name):
    try:
        target = safe_recipe_path(name)
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "recipe must be a JSON object"}), 400

    payload.setdefault("name", target.stem)
    try:
        recipe = validate_recipe(payload)
    except RecipeValidationError as exc:
        return jsonify({"error": str(exc)}), 400

    target.write_text(json.dumps(recipe, indent=2) + "\n", encoding="utf-8")
    emit_machine_event("server_status", {"status": "recipe_saved", "message": f"Recipe saved: {target.stem}"})
    return jsonify({"name": target.stem, "status": "saved", "recipe": recipe})


@app.post("/api/upload")
def upload_file():
    file = request.files.get("file")
    if file is None or not file.filename:
        return jsonify({"error": "missing file"}), 400

    target = UPLOAD_DIR / Path(file.filename).name
    file.save(target)
    return jsonify({"filename": target.name})


@app.post("/api/gcode")
def generate_gcode():
    payload = request.get_json(silent=True) or {}
    paths = payload.get("paths")
    if paths is not None:
        cup_size = str(payload.get("cup_size", "")).strip().lower()
        if cup_size not in CUP_SIZES_MM:
            return jsonify({"error": "cup_size must be one of: espresso, cappuchino, cafe_crema"}), 400

        config = current_machine_config()
        print_center_x, print_center_y = config.print_center
        try:
            cup_scale = float(payload.get("cup_scale", 1.0))
            gcode = paths_to_gcode(
                paths,
                coordinate_size=float(payload.get("coordinate_size", 768)),
                cup_radius=float(payload.get("cup_radius", 360.96)),
                cup_scale=cup_scale,
                print_diameter_mm=CUP_SIZES_MM[cup_size],
                print_center_x_mm=print_center_x,
                print_center_y_mm=print_center_y,
                nozzle_mm=config.nozzle_mm,
                hatch_spacing_mm=config.nozzle_mm,
                draw_feed=int(payload.get("draw_feed", 1200)),
                travel_feed=int(payload.get("travel_feed", 3000)),
                pattern=payload.get("pattern"),
            )
        except (TypeError, ValueError) as exc:
            return jsonify({"error": str(exc)}), 400

        filename = f"{uuid4().hex}.gcode"
        target = UPLOAD_DIR / filename
        target.write_text("\n".join(gcode) + "\n", encoding="utf-8")
        return jsonify(
            {
                "filename": filename,
                "line_count": len(gcode),
                "gcode": gcode,
                "cup_size": cup_size,
                "print_diameter_mm": CUP_SIZES_MM[cup_size],
                "cup_scale": cup_scale,
                "effective_print_diameter_mm": CUP_SIZES_MM[cup_size] * cup_scale,
                "nozzle_mm": config.nozzle_mm,
                "print_center_x_mm": print_center_x,
                "print_center_y_mm": print_center_y,
            }
        )

    svg = payload.get("svg", "")
    if not svg:
        return jsonify({"error": "missing paths or svg"}), 400

    gcode = svg_to_gcode(svg)
    filename = f"{uuid4().hex}.gcode"
    target = UPLOAD_DIR / filename
    target.write_text("\n".join(gcode) + "\n", encoding="utf-8")
    return jsonify({"filename": filename, "line_count": len(gcode), "gcode": gcode})


@app.post("/api/start-print")
def start_print():
    guard = require_system_enabled()
    if guard is not None:
        return guard

    tank_guard = require_tank_ready()
    if tank_guard is not None:
        return tank_guard

    payload = request.get_json(silent=True) or {}
    gcode = payload.get("gcode")
    moving_only = bool(payload.get("moving_only"))
    if not isinstance(gcode, list):
        return jsonify({"error": "gcode must be a list of commands"}), 400

    recipe = None
    recipe_name = None
    try:
        if isinstance(payload.get("recipe"), str) and payload["recipe"].strip():
            recipe, target = load_recipe_from_name(payload["recipe"])
            recipe_name = target.stem
        elif isinstance(payload.get("recipe"), dict):
            recipe = validate_recipe(payload["recipe"])
            recipe_name = recipe["name"]
    except FileNotFoundError:
        return jsonify({"error": "recipe not found"}), 404
    except (ValueError, RecipeValidationError) as exc:
        return jsonify({"error": str(exc)}), 400

    if get_cleaning_session().is_active():
        try:
            deactivate_cleaning_mode(clear_outputs=True)
        except Exception as exc:
            return jsonify({"error": f"Cleaning outputs could not be reset: {exc}"}), 409

    try:
        job_id = get_print_manager().start_print(
            gcode,
            recipe=recipe,
            recipe_name=recipe_name,
            moving_only=moving_only,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except PrintJobBusyError as exc:
        return jsonify({"error": str(exc)}), 409

    return jsonify({"job_id": job_id, "status": "started", "moving_only": moving_only}), 202


@app.post("/api/abort")
def abort_print():
    job_id = get_print_manager().abort()
    return jsonify({"job_id": job_id, "status": "aborted"})


@app.post("/api/cleaning/start")
def start_cleaning():
    guard = require_system_enabled()
    if guard is not None:
        return guard

    tank_guard = require_tank_ready()
    if tank_guard is not None:
        return tank_guard

    idle_guard = require_machine_idle()
    if idle_guard is not None:
        return idle_guard

    ensure_cleaning_recipe()
    config = current_machine_config()
    center_x, center_y = config.print_center
    try:
        recipe, _target = load_recipe_from_name("Cleaning")
        state = get_cleaning_session().set_running(True)
        emit_cleaning_state(state)
        gcode = [
            f"G0 X{center_x:.3f} Y{center_y:.3f} F1800",
            "G0 Z0.000 F300",
        ]
        job_id = get_print_manager().start_print(
            gcode,
            recipe=recipe,
            recipe_name="Cleaning",
            moving_only=True,
        )
    except FileNotFoundError:
        emit_cleaning_state(get_cleaning_session().deactivate())
        return jsonify({"error": "Cleaning recipe not found"}), 404
    except (ValueError, RecipeValidationError) as exc:
        emit_cleaning_state(get_cleaning_session().deactivate())
        return jsonify({"error": str(exc)}), 400
    except PrintJobBusyError as exc:
        emit_cleaning_state(get_cleaning_session().deactivate())
        return jsonify({"error": str(exc)}), 409

    return jsonify({"job_id": job_id, "status": "started", "recipe": "Cleaning"}), 202


@app.post("/api/cleaning/output")
def set_cleaning_output():
    guard = require_system_enabled()
    if guard is not None:
        return guard
    active_guard = require_cleaning_active()
    if active_guard is not None:
        return active_guard

    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name", "")).strip().lower()
    if name not in {"pump", "heater", "flow_stop", "vacuum"}:
        return jsonify({"error": "name must be one of: pump, heater, flow_stop, vacuum"}), 400
    if "enabled" not in payload:
        return jsonify({"error": "enabled must be provided"}), 400

    try:
        apply_cleaning_output(name, bool(payload.get("enabled")))
        state = get_cleaning_session().set_output(name, bool(payload.get("enabled")))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 409

    emit_cleaning_state(state)
    emit_hardware_state()
    return jsonify(state)


@app.post("/api/cleaning/stop")
def stop_cleaning():
    active_guard = require_cleaning_active()
    if active_guard is not None:
        return active_guard

    try:
        state = deactivate_cleaning_mode(clear_outputs=True)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 409

    return jsonify({"status": "stopped", "cleaning": state})


@app.post("/api/motion/home")
def home_machine():
    guard = require_system_enabled()
    if guard is not None:
        return guard
    idle_guard = require_machine_idle()
    if idle_guard is not None:
        return idle_guard

    config = current_machine_config()
    try:
        responses = get_marlin().send_command("G28", timeout=config.command_timeout_s)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 409

    emit_machine_event("marlin_status", {"msg": "Manual homing complete"})
    return jsonify({"status": "homed", "command": "G28", "responses": responses})


@app.post("/api/motion/jog")
def jog_axis():
    guard = require_system_enabled()
    if guard is not None:
        return guard
    idle_guard = require_machine_idle()
    if idle_guard is not None:
        return idle_guard

    payload = request.get_json(silent=True) or {}
    axis = str(payload.get("axis", "")).strip().upper()
    if axis not in {"X", "Y", "Z"}:
        return jsonify({"error": "axis must be X, Y, or Z"}), 400
    try:
        distance_mm = float(payload.get("distance_mm"))
    except (TypeError, ValueError):
        return jsonify({"error": "distance_mm must be numeric"}), 400
    if distance_mm == 0:
        return jsonify({"error": "distance_mm must not be zero"}), 400

    feed = 300 if axis == "Z" else 1800
    command = f"G0 {axis}{distance_mm:.3f} F{feed}"
    config = current_machine_config()
    try:
        responses = []
        responses.extend(get_marlin().send_command("G91", timeout=config.command_timeout_s))
        responses.extend(get_marlin().send_command(command, timeout=config.command_timeout_s))
        responses.extend(get_marlin().send_command("G90", timeout=config.command_timeout_s))
    except Exception as exc:
        try:
            get_marlin().send_command("G90", timeout=config.command_timeout_s)
        except Exception:
            pass
        return jsonify({"error": str(exc)}), 409

    emit_machine_event("marlin_status", {"msg": f"Manual jog {axis} {distance_mm:.3f} mm"})
    return jsonify(
        {
            "status": "moved",
            "axis": axis,
            "distance_mm": distance_mm,
            "commands": ["G91", command, "G90"],
            "responses": responses,
        }
    )


@app.post("/api/foam/start")
def foam_start():
    guard = require_system_enabled()
    if guard is not None:
        return guard

    tank_guard = require_tank_ready()
    if tank_guard is not None:
        return tank_guard

    payload = request.get_json(silent=True) or {}

    try:
        if isinstance(payload.get("recipe"), str):
            recipe, target = load_recipe_from_name(payload["recipe"])
            recipe_name = target.stem
        elif isinstance(payload.get("recipe"), dict):
            recipe = validate_recipe(payload["recipe"])
            recipe_name = recipe["name"]
        else:
            return jsonify({"error": "recipe must be a recipe name or object"}), 400

        started_name = get_relay_sequencer().start(recipe, recipe_name=recipe_name)
    except FileNotFoundError:
        return jsonify({"error": "recipe not found"}), 404
    except (ValueError, RecipeValidationError) as exc:
        return jsonify({"error": str(exc)}), 400
    except RelaySequencerBusyError as exc:
        return jsonify({"error": str(exc)}), 409

    return jsonify({"status": "started", "recipe": started_name}), 202


@app.post("/api/foam/stop")
def foam_stop():
    state = get_relay_sequencer().stop()
    return jsonify({"status": "stopping", "state": state})


@app.get("/api/foam/state")
def foam_state():
    return jsonify(get_relay_sequencer().get_state())


@app.post("/api/relays")
def set_relays():
    guard = require_system_enabled()
    if guard is not None:
        return guard

    tank_guard = require_tank_ready()
    if tank_guard is not None:
        return tank_guard

    payload = request.get_json(silent=True) or {}
    values = payload.get("set") if isinstance(payload.get("set"), dict) else payload

    if not isinstance(values, dict) or not values:
        return jsonify({"error": "request must contain relay states"}), 400

    try:
        state = get_relay_sequencer().set_relays(values)
    except RecipeValidationError as exc:
        return jsonify({"error": str(exc)}), 400

    return jsonify(state)


@app.post("/api/tank")
def move_tank():
    guard = require_system_enabled()
    if guard is not None:
        return guard

    payload = request.get_json(silent=True) or {}
    try:
        angle = float(payload.get("angle"))
    except (TypeError, ValueError):
        return jsonify({"error": "angle must be numeric"}), 400

    try:
        get_hardware().set_servo_angle(angle)
        simulated = False
    except NotImplementedError:
        simulated = True
        emit_machine_event(
            "warning",
            {"msg": "Tank servo not configured; simulated movement only", "level": "warning"},
        )
    except RuntimeError as exc:
        return jsonify({"error": str(exc)}), 409

    state = get_hardware().get_tank_state()
    emit_machine_event("tank_status", state)
    return jsonify({"angle": angle, "status": "ok", "simulated": simulated, "tank": state})


@app.get("/api/tof")
def read_tof():
    try:
        distance_mm = float(get_hardware().read_tof_mm())
        simulated = False
    except NotImplementedError:
        distance_mm = current_machine_config().default_tof_distance_mm
        simulated = True
        emit_machine_event(
            "warning",
            {"msg": "ToF sensor not configured; using default reading", "level": "warning"},
        )

    z_value = current_machine_config().surface_z(distance_mm)
    payload = {
        "distance_mm": distance_mm,
        "z_mm": z_value,
        "simulated": simulated,
    }
    emit_machine_event("tof_reading", payload)
    return jsonify(payload)


@app.get("/api/debug/state")
def debug_state():
    return jsonify(get_debug_snapshot())


@app.post("/api/debug/uart/test")
def debug_uart_test():
    payload = request.get_json(silent=True) or {}
    test_name = str(payload.get("name", "ping")).strip().lower()
    command_map = {
        "ping": "M115",
        "firmware": "M115",
        "position": "M114",
        "home": "G28",
    }
    command = command_map.get(test_name, "M115")
    config = current_machine_config()
    try:
        responses = get_marlin().send_command(command, timeout=config.command_timeout_s)
        if hasattr(get_marlin(), "mark_test_result"):
            get_marlin().mark_test_result(test_name, True, command=command, responses=responses)
        snapshot = get_debug_snapshot()
        emit_machine_event("debug_snapshot", snapshot)
        return jsonify({"status": "ok", "test": test_name, "command": command, "responses": responses, "debug": snapshot})
    except Exception as exc:
        if hasattr(get_marlin(), "mark_test_result"):
            get_marlin().mark_test_result(test_name, False, command=command, error=str(exc))
        snapshot = get_debug_snapshot()
        emit_machine_event("debug_snapshot", snapshot)
        return jsonify({"error": str(exc), "test": test_name, "command": command, "debug": snapshot}), 409


@app.post("/api/debug/uart/command")
def debug_uart_command():
    guard = require_debug_write_access()
    if guard is not None:
        return guard

    payload = request.get_json(silent=True) or {}
    command = str(payload.get("command", "")).strip()
    if not command:
        return jsonify({"error": "command must not be empty"}), 400

    config = current_machine_config()
    try:
        responses = get_marlin().send_command(command, timeout=config.command_timeout_s)
        snapshot = get_debug_snapshot()
        emit_machine_event("debug_snapshot", snapshot)
        return jsonify({"status": "ok", "command": command, "responses": responses, "debug": snapshot})
    except Exception as exc:
        snapshot = get_debug_snapshot()
        emit_machine_event("debug_snapshot", snapshot)
        return jsonify({"error": str(exc), "command": command, "debug": snapshot}), 409


@app.post("/api/debug/output")
def debug_output():
    guard = require_debug_write_access()
    if guard is not None:
        return guard

    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name", "")).strip().lower()
    if not name:
        return jsonify({"error": "name must be provided"}), 400
    if "enabled" not in payload:
        return jsonify({"error": "enabled must be provided"}), 400
    enabled = bool(payload.get("enabled"))

    config = current_machine_config()
    try:
        if name in config.output_pins:
            written = get_hardware().set_output_pin(name, enabled)
            result = {"target": "gpio", "written": bool(written)}
        elif name == "vacuum":
            written = get_hardware().set_output_pin("vacuum_relay", enabled)
            result = {"target": "gpio", "written": bool(written), "resolved_name": "vacuum_relay"}
        elif config.resolve_optional_pin_name(name) in config.pins:
            resolved = config.resolve_pin_name(name)
            responses = get_marlin().send_command(
                config.pin_command(resolved, enabled),
                timeout=config.command_timeout_s,
            )
            if resolved == "case_led":
                get_hardware().set_case_led_enabled(enabled)
            result = {"target": "marlin", "responses": responses, "resolved_name": resolved}
        else:
            return jsonify({"error": f"unknown debug output: {name}"}), 400
    except Exception as exc:
        snapshot = get_debug_snapshot()
        emit_machine_event("debug_snapshot", snapshot)
        return jsonify({"error": str(exc), "name": name, "debug": snapshot}), 409

    snapshot = get_debug_snapshot()
    emit_machine_event("debug_snapshot", snapshot)
    return jsonify({"status": "ok", "name": name, "enabled": enabled, "result": result, "debug": snapshot})


@socketio.on("connect")
def handle_connect():
    emit(
        "server_status",
        {
            "status": "connected",
            "message": "Backend verbunden",
        },
    )
    emit("relay_update", get_relay_sequencer().get_state()["relay_state"])
    emit("foam_status", get_relay_sequencer().get_state())
    emit("hardware_inputs", get_hardware().get_hardware_state())
    emit("tank_status", get_hardware().get_tank_state())
    emit("machine_config", current_machine_config().machine_config_payload(tank=get_hardware().get_tank_state()))
    emit("usage_stats", usage_stats_payload())
    emit("cleaning_state", get_cleaning_session().get_state())
    emit("debug_snapshot", get_debug_snapshot())


@socketio.on("disconnect")
def handle_disconnect():
    print("SocketIO client disconnected")


@socketio.on("client_ping")
def handle_client_ping(payload=None):
    payload = payload or {}
    emit(
        "server_pong",
        {
            "status": "ok",
            "timestamp": payload.get("timestamp"),
        },
    )


if __name__ == "__main__":
    socketio.run(app, host="0.0.0.0", port=5000, debug=True)
