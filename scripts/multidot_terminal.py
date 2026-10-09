"""Bounded, masked POSIX-terminal input for Python 3.11 and newer.

Only public prompts, ASCII mask characters and fixed terminal controls are
written. A mask represents one Unicode code point, including combining marks.
The returned string is intentionally the caller's responsibility: do not log it.
Python cannot guarantee erasure of immutable strings from process memory.

Catchable asynchronous interruptions abort entry. Ignored signals, including a
normal window resize, keep their original behavior. Original terminal settings
and signal handlers are restored before an interruption's original disposition
is invoked. Resuming after a
suspension aborts this entry. SIGKILL, SIGSTOP, process crashes, a disconnected
terminal, or permanently failing terminal syscalls cannot offer restoration.

Bracketed paste is enabled while reading. Its delimiters are never key data;
newlines or controls inside a paste abort the entire entry. Legacy unbracketed
single-line UTF-8 paste works too. Pending input is discarded on every exit;
queued text after Enter is rejected rather than becoming a later answer. An
unbracketed newline delivered alone is indistinguishable from a typed Enter.
"""
from __future__ import annotations

import codecs
from contextlib import contextmanager
import os
import select
import signal
import sys
import termios
import threading
import time
import unicodedata

from multidot_setup import SetupError

MAX_SECRET_BYTES = 16384
MAX_SECRET_CHARACTERS = 8191
_ESCAPE_TIMEOUT = 0.5
_ENTER_QUIET_SECONDS = 0.05
_PASTE_ON = b"\x1b[?2004h"
_PASTE_OFF = b"\x1b[?2004l"
# Include platform-specific and real-time signals, not just common ways to
# interrupt a prompt. Synchronous process faults still cannot be recovered by a
# Python signal handler; the guarantee covers signals delivered asynchronously.
_HANDLED_SIGNALS = frozenset(signal.valid_signals()) - {
    signal.SIGKILL, signal.SIGSTOP,
}
_DEFAULT_IGNORED_SIGNALS = frozenset(
    getattr(signal, name) for name in (
        "SIGCHLD", "SIGCONT", "SIGURG", "SIGWINCH",
    ) if hasattr(signal, name)
)
_PUBLIC_ERRORS = frozenset({
    "interactive_terminal_required", "secure_password_entry_unavailable",
    "secret_input_invalid", "secret_input_too_long", "secret_input_interrupted",
    "secure_terminal_restore_failed",
})


class _TerminalSignal(BaseException):
    def __init__(self, number):
        self.number = number


def require_tty():
    """Refuse missing/redirected streams, even if a stream spoofs isatty()."""
    try:
        streams = (sys.stdin, sys.stdout, sys.stderr)
        if not all(stream.isatty() for stream in streams):
            raise ValueError
        descriptors = tuple(stream.fileno() for stream in streams)
        if not all(os.isatty(fd) for fd in descriptors):
            raise ValueError
        # Writing masks to a different terminal is not an interactive session.
        if len({os.fstat(fd).st_rdev for fd in descriptors}) != 1:
            raise ValueError
    except Exception:
        raise SetupError("interactive_terminal_required") from None
    return descriptors


def _write(fd, value):
    """Write only caller-supplied public bytes, including partial writes."""
    while value:
        count = os.write(fd, value)
        if count <= 0:
            raise OSError
        value = value[count:]


def _signal_received(number, _frame):
    raise _TerminalSignal(number)


def _set_attributes(fd, when, attributes):
    # A signal can interrupt a syscall even while Python has signals deferred.
    while True:
        try:
            termios.tcsetattr(fd, when, attributes)
            return
        except InterruptedError:
            continue


@contextmanager
def _terminal_session(input_fd, output_fd, previous_handlers):
    """Make changing modes and restoration atomic with respect to our signals."""
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, _HANDLED_SIGNALS)
    original = None
    changed = paste_enabled = False
    restore_failed = False
    try:
        for number in _HANDLED_SIGNALS:
            handler = signal.getsignal(number)
            if handler == signal.SIG_IGN or (
                    handler == signal.SIG_DFL and number in _DEFAULT_IGNORED_SIGNALS):
                continue
            previous_handlers[number] = handler
            signal.signal(number, _signal_received)
        original = termios.tcgetattr(input_fd)
        masked = [*original[:6], list(original[6])]
        for name in ("BRKINT", "ICRNL", "INLCR", "IGNCR", "INPCK", "ISTRIP",
                     "IXON", "IXOFF", "PARMRK"):
            masked[0] &= ~getattr(termios, name, 0)
        masked[2] &= ~(termios.CSIZE | termios.PARENB)
        masked[2] |= termios.CS8
        for name in ("ECHO", "ECHONL", "ECHOE", "ECHOK", "ECHOCTL", "ECHOKE",
                     "ICANON", "ISIG", "IEXTEN"):
            masked[3] &= ~getattr(termios, name, 0)
        masked[6][termios.VMIN] = 1
        masked[6][termios.VTIME] = 0
        changed = True
        _set_attributes(input_fd, termios.TCSAFLUSH, masked)
        paste_enabled = True
        _write(output_fd, _PASTE_ON)
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        yield
    finally:
        # Do not let a second termination signal interrupt cleanup. Restore
        # signal dispositions only after input is flushed and echo is restored.
        signal.pthread_sigmask(signal.SIG_BLOCK, _HANDLED_SIGNALS)
        try:
            if changed:
                try:
                    _set_attributes(input_fd, termios.TCSAFLUSH, original)
                except Exception:
                    restore_failed = True
            if paste_enabled:
                try:
                    _write(output_fd, _PASTE_OFF + b"\n")
                except Exception:
                    # Output failure must never prevent terminal restoration.
                    restore_failed = True
        finally:
            try:
                for number, handler in previous_handlers.items():
                    signal.signal(number, handler)
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        if restore_failed:
            raise SetupError("secure_terminal_restore_failed") from None


def _read_byte(fd, timeout=None):
    if timeout is not None and not select.select([fd], [], [], timeout)[0]:
        raise SetupError("secret_input_invalid")
    value = os.read(fd, 1)
    if not value:
        raise EOFError
    return value


def _discard_rejected_paste(fd, tail):
    """Consume a fragmented rejected paste while echo is still disabled.

    This is bounded recovery, not another input field. An absent terminator or
    arbitrarily delayed input cannot be distinguished from future keystrokes.
    """
    deadline = time.monotonic() + _ESCAPE_TIMEOUT
    for _ in range(MAX_SECRET_BYTES):
        if tail == b"\x1b[201~":
            # Allow the rest of the same paste burst to reach the final flush.
            select.select([fd], [], [], _ENTER_QUIET_SECONDS)
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        try:
            value = _read_byte(fd, remaining)
        except (EOFError, OSError, SetupError):
            return
        tail = (tail + value)[-6:]


def _collect(input_fd, output_fd):
    decoder = codecs.getincrementaldecoder("utf-8")("strict")
    characters = []
    consumed = 0
    pasted = False
    tail = b""

    def reject(code="secret_input_invalid"):
        if pasted:
            _discard_rejected_paste(input_fd, tail)
        raise SetupError(code)

    def next_byte(timeout=None):
        nonlocal consumed, tail
        try:
            value = _read_byte(input_fd, timeout)
        except SetupError:
            reject()
        tail = (tail + value)[-6:]
        consumed += 1
        if consumed > MAX_SECRET_BYTES:
            reject("secret_input_too_long")
        return value

    while True:
        value = next_byte()
        if value == b"\x1b":
            if decoder.getstate()[0]:
                reject()
            sequence = b"".join(next_byte(_ESCAPE_TIMEOUT) for _ in range(5))
            if sequence == b"[200~" and not pasted:
                pasted = True
                continue
            if sequence == b"[201~" and pasted:
                pasted = False
                continue
            reject()
        if value[0] < 32 or value == b"\x7f":
            if pasted:
                reject()
            if value == b"\x03":
                raise KeyboardInterrupt
            if value == b"\x04":
                raise EOFError
            if decoder.getstate()[0]:
                reject()
            if value in (b"\r", b"\n"):
                decoder.decode(b"", final=True)
                if select.select([input_fd], [], [], _ENTER_QUIET_SECONDS)[0]:
                    reject()
                return "".join(characters)
            if value in (b"\x08", b"\x7f"):
                if characters:
                    characters.pop()
                    _write(output_fd, b"\b \b")
                continue
            reject()
        try:
            character = decoder.decode(value)
        except UnicodeError:
            reject()
        if character:
            if unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"}:
                reject()
            if len(characters) >= MAX_SECRET_CHARACTERS:
                reject("secret_input_too_long")
            characters.append(character)
            _write(output_fd, b"*")


def _propagate_signal(number, previous_handlers):
    handler = previous_handlers.get(number, signal.SIG_DFL)
    if handler == signal.SIG_DFL:
        # Redeliver only after _terminal_session restored the original handler.
        os.kill(os.getpid(), number)
    elif callable(handler):
        handler(number, None)
    # Ignored signals, returning custom handlers, or resumed suspension still
    # cancel entry instead of exposing a partial secret to the caller.
    raise SetupError("secret_input_interrupted")


def read_secret(prompt="Runtime API key (masked): ") -> str:
    """Read a UTF-8 secret from one real POSIX TTY, never with echoing fallback.

    The prompt must be public printable ASCII. Call from the main thread.
    Ctrl-C raises KeyboardInterrupt and Ctrl-D/terminal EOF raises EOFError.
    Other input or I/O failures raise SetupError with a fixed, value-free code.
    """
    previous_handlers = {}
    signal_number = None
    try:
        input_fd, output_fd, _ = require_tty()
        if (threading.current_thread() is not threading.main_thread()
                or not hasattr(signal, "pthread_sigmask")):
            raise SetupError("secure_password_entry_unavailable")
        if (not isinstance(prompt, str) or len(prompt) > 1024
                or any(not 32 <= ord(character) <= 126 for character in prompt)):
            raise SetupError("secure_password_entry_unavailable")
        sys.stdout.flush()
        sys.stderr.flush()
        with _terminal_session(input_fd, output_fd, previous_handlers):
            _write(output_fd, prompt.encode("ascii"))
            return _collect(input_fd, output_fd)
    except _TerminalSignal as interruption:
        signal_number = interruption.number
    except KeyboardInterrupt:
        failure = KeyboardInterrupt()
    except EOFError:
        failure = EOFError()
    except Exception as error:
        code = (error.code if isinstance(error, SetupError)
                and type(error.code) is str and error.code in _PUBLIC_ERRORS else
                "secret_input_invalid" if isinstance(error, UnicodeError) else
                "secure_password_entry_unavailable")
        failure = SetupError(code)
    # Raise outside the original exception handler, so even __context__ does
    # not retain an I/O or decoding exception that could contain input bytes.
    if signal_number is not None:
        _propagate_signal(signal_number, previous_handlers)
    raise failure
