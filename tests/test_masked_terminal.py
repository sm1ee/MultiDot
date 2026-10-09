"""Masked input MOCK coverage using disposable PTYs and fake keys only."""
from __future__ import annotations

from contextlib import ExitStack
import io
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import termios
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import multidot_setup as setup
import multidot_terminal as terminal

MARKER = "LOCAL_MASKED_PTY_FAKE_KEY_ONLY"
UNICODE = "\ud55c\uae00-\U0001f511-e\u0301"
PROMPT = b"Runtime API key (masked): "
CHILD = r'''
import fcntl, os, resource, signal, sys, termios
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
sys.path.insert(0, os.environ["FIXTURE_SCRIPTS"])
import multidot_setup as setup
import multidot_terminal as terminal
MARKER = "LOCAL_MASKED_PTY_FAKE_KEY_ONLY"
mode = os.environ.get("FIXTURE_MODE", "normal")
if sys.stdin.isatty():
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
original = termios.tcgetattr(0) if sys.stdin.isatty() else None
old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])
if mode == "default_termination":
    # Python normally ignores SIGXFSZ; this fixture explicitly tests SIG_DFL.
    signal.signal(signal.SIGXFSZ, signal.SIG_DFL)
if mode == "custom_signal":
    def prior_handler(number, frame):
        print("PRIOR_HANDLER_RESTORED=" + str(termios.tcgetattr(0) == original), flush=True)
    signal.signal(signal.SIGTERM, prior_handler)
original_handlers = {number: signal.getsignal(number) for number in terminal._HANDLED_SIGNALS}
if mode == "write_error":
    write = terminal.os.write
    def fail_write(fd, value):
        if value == b"*":
            raise OSError(MARKER)
        return write(fd, value)
    terminal.os.write = fail_write
elif mode == "read_error":
    def fail_read(fd, count):
        raise OSError(MARKER)
    terminal.os.read = fail_read
elif mode == "eof_read":
    terminal.os.read = lambda fd, count: b""
elif mode == "output_cleanup_error":
    write = terminal.os.write
    def fail_cleanup(fd, value):
        if value.startswith(terminal._PASTE_OFF):
            raise OSError(MARKER)
        return write(fd, value)
    terminal.os.write = fail_cleanup
elif mode == "setup_error":
    change = terminal.termios.tcsetattr
    attempted = [False]
    def fail_after_change(fd, when, value):
        change(fd, when, value)
        if not attempted[0]:
            attempted[0] = True
            raise OSError(MARKER)
    terminal.termios.tcsetattr = fail_after_change
elif mode == "restore_eintr":
    change = terminal.termios.tcsetattr
    attempted = [False]
    def interrupt_restore(fd, when, value):
        if value == original and not attempted[0]:
            attempted[0] = True
            raise InterruptedError(MARKER)
        return change(fd, when, value)
    terminal.termios.tcsetattr = interrupt_restore
elif mode == "signal_during_restore":
    change = terminal.termios.tcsetattr
    def signal_restore(fd, when, value):
        if value == original:
            os.kill(os.getpid(), signal.SIGTERM)
        return change(fd, when, value)
    terminal.termios.tcsetattr = signal_restore
expected = {
    "unicode": "\ud55c\uae00-\U0001f511-e\u0301",
    "edited": "\ud55cB",
    "empty": "",
    "maximum_ascii": "X" * 8191,
}.get(mode, MARKER)
try:
    value = terminal.read_secret()
    print("RESULT_MATCH=" + str(value == expected), flush=True)
except setup.SetupError as error:
    print("ERROR=" + error.code, flush=True)
    print("ERROR_CONTEXT_CLEARED=" + str(error.__context__ is None and error.__cause__ is None), flush=True)
except EOFError:
    print("CANCELLED_EOF", flush=True)
except KeyboardInterrupt:
    print("CANCELLED_INTERRUPT", flush=True)
finally:
    restored = original is None or termios.tcgetattr(0) == original
    print("ATTRIBUTES_RESTORED=" + str(restored), flush=True)
    print("HANDLERS_RESTORED=" + str(all(signal.getsignal(number) == handler
          for number, handler in original_handlers.items())), flush=True)
    print("MASK_RESTORED=" + str(signal.pthread_sigmask(signal.SIG_BLOCK, []) == old_mask), flush=True)
'''


@unittest.skipUnless(os.name == "posix" and hasattr(os, "openpty"), "POSIX PTY required")
class MaskedTerminalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="multidot-masked-pty-")
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name) / "home"
        self.home.mkdir(mode=0o700)
        self.env = {"HOME": str(self.home), "PATH": os.defpath, "LANG": "C.UTF-8",
                    "FIXTURE_SCRIPTS": str(ROOT / "scripts")}
        self.sessions = []
        self.addCleanup(self.close_sessions)

    def close_sessions(self):
        for child, master, slave, _original in self.sessions:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=3)
            os.close(master)
            os.close(slave)

    def start(self, mode="normal", redirected=None):
        master, slave = os.openpty()
        os.set_blocking(master, False)
        original = termios.tcgetattr(slave)
        stdio = {name: slave for name in ("stdin", "stdout", "stderr")}
        if redirected:
            stdio[redirected] = subprocess.PIPE
        child = subprocess.Popen([sys.executable, "-c", CHILD],
                                 env={**self.env, "FIXTURE_MODE": mode},
                                 start_new_session=True, **stdio)
        self.sessions.append((child, master, slave, original))
        self.child, self.master, self.slave, self.original = self.sessions[-1]
        self.output = b""
        if redirected is None and mode != "setup_error":
            self.until(PROMPT)
        return child

    def read(self, timeout=0.05):
        if select.select([self.master], [], [], timeout)[0]:
            try:
                self.output += os.read(self.master, 65536)
            except OSError:
                pass

    def until(self, fragment):
        deadline = time.monotonic() + 5
        while fragment not in self.output and time.monotonic() < deadline:
            self.read()
        self.assertIn(fragment, self.output, "Expected public fixture output was not observed")

    def send(self, value):
        pending = value.encode("utf-8") if isinstance(value, str) else value
        deadline = time.monotonic() + 5
        while pending and time.monotonic() < deadline:
            readable, writable, _ = select.select([self.master], [self.master], [], 0.05)
            if readable:
                self.read(0)
            if writable:
                try:
                    count = os.write(self.master, pending[:1024])
                    pending = pending[count:]
                except BlockingIOError:
                    pass
        self.assertFalse(pending, "The disposable PTY stopped accepting fixture input")

    def finish(self, status=0, report=True):
        deadline = time.monotonic() + 5
        while self.child.poll() is None and time.monotonic() < deadline:
            self.read()
        self.assertEqual(self.child.wait(timeout=1), status)
        self.read()
        for stream in (self.child.stdout, self.child.stderr):
            if stream is not None:
                self.output += stream.read()
                stream.close()
        if self.child.stdin is not None:
            self.child.stdin.close()
        self.assertEqual(termios.tcgetattr(self.slave), self.original)
        self.assertNotIn(MARKER.encode(), self.output)
        self.assertNotIn(UNICODE.encode(), self.output)
        if report:
            for name in ("ATTRIBUTES", "HANDLERS", "MASK"):
                self.assertIn((name + "_RESTORED=True").encode(), self.output)
        self.assertEqual(list(self.home.iterdir()), [])

    def test_ascii_input_shows_exactly_one_mask_per_character(self):
        self.start()
        self.assertFalse(termios.tcgetattr(self.slave)[3] & termios.ECHO)
        self.send(MARKER + "\n")
        self.finish()
        self.assertIn(b"RESULT_MATCH=True", self.output)
        self.assertEqual(self.output.count(b"*"), len(MARKER))
        self.assertIn(terminal._PASTE_ON, self.output)
        self.assertIn(terminal._PASTE_OFF, self.output)

    def test_unicode_split_byte_input_decodes_before_each_mask(self):
        self.start("unicode")
        for value in UNICODE.encode():
            self.send(bytes([value]))
        self.send(b"\r")
        self.finish()
        self.assertIn(b"RESULT_MATCH=True", self.output)
        self.assertEqual(self.output.count(b"*"), len(UNICODE))

    def test_bracketed_unicode_paste_does_not_include_delimiters(self):
        self.start("unicode")
        self.send(b"\x1b[200~" + UNICODE.encode() + b"\x1b[201~\n")
        self.finish()
        self.assertIn(b"RESULT_MATCH=True", self.output)
        self.assertEqual(self.output.count(b"*"), len(UNICODE))

    def test_backspace_removes_whole_unicode_character_and_mask(self):
        self.start("edited")
        self.send("\x7f\ud55c\uae00\x7fA\x08B\n")
        self.finish()
        self.assertIn(b"RESULT_MATCH=True", self.output)
        self.assertEqual(self.output.count(b"\b \b"), 2)

    def test_empty_entry_is_returned_for_caller_validation(self):
        self.start("empty")
        self.send(b"\n")
        self.finish()
        self.assertIn(b"RESULT_MATCH=True", self.output)

    def test_ctrl_c_and_ctrl_d_cancel_with_attributes_restored(self):
        for control, outcome in ((b"\x03", b"CANCELLED_INTERRUPT"),
                                 (b"\x04", b"CANCELLED_EOF")):
            with self.subTest(control=control):
                self.start()
                self.send(MARKER.encode() + control)
                self.finish()
                self.assertIn(outcome, self.output)
                self.assertNotIn(b"RESULT_MATCH", self.output)

    def test_actual_read_eof_restores_attributes(self):
        self.start("eof_read")
        self.finish()
        self.assertIn(b"CANCELLED_EOF", self.output)

    def test_invalid_utf8_including_incomplete_codepoint_is_redacted(self):
        for value in (b"\xff", b"\xc3\n", b"\xf0\x80\x80\x80"):
            with self.subTest(value=value):
                self.start()
                self.send(value)
                self.finish()
                self.assertIn(b"ERROR=secret_input_invalid", self.output)
                self.assertNotIn(b"UnicodeDecodeError", self.output)

    def test_controls_and_newlines_inside_paste_abort_instead_of_answering(self):
        for control in (b"\n", b"\r", b"\t", b"\x03", b"\x04", b"\x00", b"\x7f"):
            with self.subTest(control=control):
                self.start()
                self.send(b"\x1b[200~" + MARKER.encode() + control +
                          b"yes\n\x1b[201~\nyes\n")
                self.finish()
                self.assertIn(b"ERROR=secret_input_invalid", self.output)
                self.assertNotIn(b"RESULT_MATCH", self.output)
                self.assertFalse(select.select([self.slave], [], [], 0)[0])

    def test_legacy_multiline_paste_is_rejected_and_pending_answers_flushed(self):
        self.start()
        self.send(MARKER + "\nyes\nyes\n")
        self.finish()
        self.assertIn(b"ERROR=secret_input_invalid", self.output)
        self.assertNotIn(b"RESULT_MATCH", self.output)
        self.assertFalse(select.select([self.slave], [], [], 0)[0])

    def test_rejected_bracketed_paste_waits_for_fragmented_tail(self):
        self.start()
        self.send(b"\x1b[200~" + MARKER.encode() + b"\n")
        self.until(b"*" * len(MARKER))
        time.sleep(0.05)
        self.assertFalse(termios.tcgetattr(self.slave)[3] & termios.ECHO)
        self.send(MARKER.encode() + b"\x1b[201~\nyes\n")
        self.finish()
        self.assertIn(b"ERROR=secret_input_invalid", self.output)
        self.assertFalse(select.select([self.slave], [], [], 0)[0])

    def test_unsupported_controls_escapes_and_unicode_controls_fail_closed(self):
        for value in (b"\t", b"\x1b", b"\x1b[A", b"\x1b[201~",
                      b"\x1b[200~\x1b[200~", "\u202e".encode(), "\u0085".encode()):
            with self.subTest(value=value):
                self.start()
                self.send(value)
                self.finish()
                self.assertIn(b"ERROR=secret_input_invalid", self.output)

    def test_read_write_setup_and_cleanup_errors_do_not_echo_values(self):
        for mode in ("read_error", "write_error", "setup_error", "output_cleanup_error"):
            with self.subTest(mode=mode):
                self.start(mode)
                if mode in ("write_error", "output_cleanup_error"):
                    self.send(MARKER + "\n")
                self.finish()
                self.assertIn(b"ERROR=secure_", self.output)
                self.assertIn(b"ERROR_CONTEXT_CLEARED=True", self.output)
                self.assertNotIn(b"Traceback", self.output)

    def test_interrupted_restore_syscall_is_retried(self):
        self.start("restore_eintr")
        self.send(MARKER + "\n")
        self.finish()
        self.assertIn(b"RESULT_MATCH=True", self.output)

    def test_default_termination_signals_restore_attributes_before_delivery(self):
        for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT, signal.SIGUSR1,
                       signal.SIGUSR2, signal.SIGALRM, signal.SIGXCPU, signal.SIGXFSZ):
            with self.subTest(signal=number):
                self.start("default_termination")
                self.send(MARKER)
                self.until(b"*" * len(MARKER))
                self.child.send_signal(number)
                self.finish(-number, report=False)

    def test_default_ignored_signals_do_not_abort_input(self):
        self.start()
        for number in terminal._DEFAULT_IGNORED_SIGNALS | {signal.SIGPIPE, signal.SIGXFSZ}:
            self.child.send_signal(number)
        self.send(MARKER + "\n")
        self.finish()
        self.assertIn(b"RESULT_MATCH=True", self.output)

    def test_external_sigint_restores_attributes_and_cancels(self):
        self.start()
        self.child.send_signal(signal.SIGINT)
        self.finish()
        self.assertIn(b"CANCELLED_INTERRUPT", self.output)

    def test_custom_signal_handler_runs_only_after_restoration(self):
        self.start("custom_signal")
        self.child.send_signal(signal.SIGTERM)
        self.finish()
        self.assertIn(b"PRIOR_HANDLER_RESTORED=True", self.output)
        self.assertIn(b"ERROR=secret_input_interrupted", self.output)

    def test_signal_arriving_during_cleanup_is_deferred_until_restore(self):
        self.start("signal_during_restore")
        self.send(MARKER + "\n")
        self.finish(-signal.SIGTERM, report=False)

    def test_input_character_and_consumed_byte_limits_are_bounded(self):
        for value in (b"X" * (terminal.MAX_SECRET_CHARACTERS + 1),
                      b"X\x7f" * (terminal.MAX_SECRET_BYTES // 2 + 1)):
            with self.subTest(length=len(value)):
                self.start()
                self.send(value)
                self.finish()
                self.assertIn(b"ERROR=secret_input_too_long", self.output)

    def test_maximum_supported_ascii_key_length_is_accepted(self):
        self.start("maximum_ascii")
        self.send(b"X" * 8191 + b"\n")
        self.finish()
        self.assertIn(b"RESULT_MATCH=True", self.output)
        self.assertEqual(self.output.count(b"*"), 8191)

    def test_each_redirected_standard_stream_is_refused_without_fallback(self):
        for stream in ("stdin", "stdout", "stderr"):
            with self.subTest(stream=stream):
                self.start(redirected=stream)
                self.finish()
                self.assertIn(b"ERROR=interactive_terminal_required", self.output)
                self.assertNotIn(PROMPT, self.output)

    def test_no_tty_and_fake_isatty_are_refused_before_read_or_prompt(self):
        class FakeTTY(io.StringIO):
            def isatty(self):
                return True
        for factory in (io.StringIO, FakeTTY):
            with self.subTest(factory=factory), ExitStack() as stack:
                output = factory()
                for name in ("stdin", "stdout", "stderr"):
                    stack.enter_context(patch.object(sys, name, output))
                read = stack.enter_context(patch.object(terminal.os, "read"))
                with self.assertRaises(setup.SetupError) as caught:
                    terminal.read_secret()
                self.assertEqual(caught.exception.code, "interactive_terminal_required")
                read.assert_not_called()
                self.assertEqual(output.getvalue(), "")

    def test_non_main_thread_is_refused_before_changing_terminal(self):
        errors = []
        def read():
            try:
                terminal.read_secret()
            except setup.SetupError as error:
                errors.append(error.code)
        with patch.object(terminal, "require_tty", return_value=(0, 1, 2)), \
                patch.object(terminal.termios, "tcsetattr") as change:
            worker = threading.Thread(target=read)
            worker.start()
            worker.join(timeout=3)
            self.assertFalse(worker.is_alive())
            change.assert_not_called()
        self.assertEqual(errors, ["secure_password_entry_unavailable"])

    def test_non_public_prompts_are_refused_before_terminal_changes(self):
        for prompt in (MARKER + "\n", "\u202e", "X" * 1025, b"bytes"):
            with self.subTest(prompt_type=type(prompt).__name__), \
                    patch.object(terminal, "require_tty", return_value=(0, 1, 2)), \
                    patch.object(terminal.termios, "tcsetattr") as change:
                with self.assertRaises(setup.SetupError) as caught:
                    terminal.read_secret(prompt)
                self.assertEqual(caught.exception.code, "secure_password_entry_unavailable")
                self.assertIsNone(caught.exception.__context__)
                change.assert_not_called()


if __name__ == "__main__":
    unittest.main()
