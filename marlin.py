import queue
import threading
import time
from dataclasses import dataclass, field


class MarlinError(RuntimeError):
    pass


class MarlinTimeoutError(MarlinError):
    pass


@dataclass
class _CommandRequest:
    line: str
    timeout: float
    done: threading.Event = field(default_factory=threading.Event)
    responses: list[str] = field(default_factory=list)
    error: Exception | None = None


class MarlinUART:
    def __init__(
        self,
        port="/dev/serial0",
        baudrate=115200,
        timeout=2,
        status_callback=None,
        serial_factory=None,
    ):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.status_callback = status_callback or (lambda message: None)
        self.serial_factory = serial_factory
        self.serial = None
        self._queue = queue.Queue()
        self._worker = None
        self._stop_worker = threading.Event()
        self._worker_lock = threading.Lock()

    def connect(self):
        if self.serial is None:
            if self.serial_factory is not None:
                self.serial = self.serial_factory(
                    self.port,
                    self.baudrate,
                    self.timeout,
                )
            else:
                import serial

                self.serial = serial.Serial(
                    self.port,
                    self.baudrate,
                    timeout=self.timeout,
                )
        return self.serial

    def start(self):
        with self._worker_lock:
            if self._worker is not None and self._worker.is_alive():
                return
            self._stop_worker.clear()
            self._worker = threading.Thread(target=self._run_worker, daemon=True)
            self._worker.start()

    def send_command(self, line, timeout=None):
        command = str(line).strip()
        if not command:
            raise ValueError("Marlin command must not be empty")

        timeout = self.timeout if timeout is None else float(timeout)
        request = _CommandRequest(command, timeout)
        self.start()
        self._queue.put(request)

        if not request.done.wait(timeout + 1):
            raise MarlinTimeoutError(f"Timed out waiting for worker on {command}")
        if request.error is not None:
            raise request.error
        return request.responses

    def send_line(self, line):
        return self.send_command(line)

    def send_gcode(self, lines):
        responses = []
        for line in lines:
            if str(line).strip():
                responses.extend(self.send_command(line))
        return responses

    def clear_queue(self):
        while True:
            try:
                request = self._queue.get_nowait()
            except queue.Empty:
                return
            request.error = MarlinError("Command queue cleared")
            request.done.set()
            self._queue.task_done()

    def close(self):
        self._stop_worker.set()
        self.clear_queue()
        if self._worker is not None:
            self._worker.join(timeout=1)
            self._worker = None
        if self.serial is not None:
            self.serial.close()
            self.serial = None

    def _run_worker(self):
        while not self._stop_worker.is_set():
            try:
                request = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue

            try:
                request.responses = self._send_and_wait_for_ok(
                    request.line,
                    request.timeout,
                )
            except Exception as exc:
                request.error = exc
            finally:
                request.done.set()
                self._queue.task_done()

    def _send_and_wait_for_ok(self, line, timeout):
        serial_port = self.connect()
        serial_port.write((line.strip() + "\n").encode("ascii"))
        serial_port.flush()
        self._emit_status(f">> {line.strip()}")

        responses = []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            raw = serial_port.readline()
            if not raw:
                continue

            message = raw.decode("utf-8", errors="replace").strip()
            if not message:
                continue

            responses.append(message)
            self._emit_status(message)
            lowered = message.lower()
            if lowered == "ok" or lowered.startswith("ok "):
                return responses
            if lowered.startswith("error") or "error:" in lowered:
                raise MarlinError(message)

        raise MarlinTimeoutError(f"Timed out waiting for ok after {line}")

    def _emit_status(self, message):
        self.status_callback(message)
