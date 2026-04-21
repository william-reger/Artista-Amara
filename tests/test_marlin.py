import unittest

from marlin import MarlinError, MarlinTimeoutError, MarlinUART


class FakeSerial:
    def __init__(self, responses=None, default_response=b"ok\n"):
        self.responses = list(responses or [])
        self.default_response = default_response
        self.writes = []
        self.closed = False

    def write(self, data):
        self.writes.append(data.decode("ascii").strip())

    def flush(self):
        pass

    def readline(self):
        if self.responses:
            response = self.responses.pop(0)
            return response if isinstance(response, bytes) else response.encode("utf-8")
        return self.default_response

    def close(self):
        self.closed = True


class MarlinUARTTest(unittest.TestCase):
    def test_send_command_waits_for_ok_and_reports_status(self):
        fake = FakeSerial([b"echo:G28\n", b"ok\n"])
        statuses = []
        uart = MarlinUART(
            timeout=0.2,
            status_callback=statuses.append,
            serial_factory=lambda port, baudrate, timeout: fake,
        )

        responses = uart.send_command("G28")
        uart.close()

        self.assertEqual(fake.writes, ["G28"])
        self.assertEqual(responses, ["echo:G28", "ok"])
        self.assertIn(">> G28", statuses)
        self.assertIn("ok", statuses)

    def test_error_response_raises(self):
        fake = FakeSerial([b"Error:Printer halted\n"])
        uart = MarlinUART(
            timeout=0.2,
            serial_factory=lambda port, baudrate, timeout: fake,
        )

        with self.assertRaises(MarlinError):
            uart.send_command("G1 X1")
        uart.close()

    def test_missing_ok_times_out(self):
        fake = FakeSerial([b"busy: processing\n", b"", b""], default_response=b"")
        uart = MarlinUART(
            timeout=0.01,
            serial_factory=lambda port, baudrate, timeout: fake,
        )

        with self.assertRaises(MarlinTimeoutError):
            uart.send_command("M400", timeout=0.01)
        uart.close()


if __name__ == "__main__":
    unittest.main()



