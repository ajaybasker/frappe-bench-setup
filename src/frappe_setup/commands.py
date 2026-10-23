"""The single place where external programs are executed.

Rules enforced here:
  * argv lists only — never a shell;
  * output is streamed to the (redacted) log and kept in a bounded tail for diagnosis;
  * children run in their own process group so Ctrl-C / timeouts terminate the whole tree;
  * secrets are delivered by answering prompts on a pseudo-terminal, never via argv.
"""

from __future__ import annotations

import errno
import os
import re
import selectors
import signal
import subprocess
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .errors import SetupError
from .logging_config import current_log_file, get_logger
from .redaction import REDACTOR, format_argv

LineCallback = Callable[[str], None]

TAIL_LINES = 200

NETWORK_ERROR_PATTERNS = [
    r"Could not resolve host",
    r"Temporary failure (?:in name resolution|resolving)",
    r"Connection (?:timed out|reset by peer|refused)",
    r"Failed to connect to",
    r"Network is unreachable",
    r"ETIMEDOUT|ECONNRESET|EAI_AGAIN|ENOTFOUND|ECONNREFUSED",
    r"Read timed out",
    r"unable to access '.*': (?:Could not|Failed|OpenSSL|gnutls)",
    r"early EOF",
    r"RPC failed",
    r"HTTP/2 stream \d+ was not closed cleanly",
    r"Max retries exceeded",
]
_NETWORK_RE = re.compile("|".join(NETWORK_ERROR_PATTERNS), re.IGNORECASE)


def looks_like_network_error(text: str) -> bool:
    return bool(_NETWORK_RE.search(text or ""))


@dataclass
class CommandResult:
    argv: list[str]
    returncode: int
    output: str  # combined stdout+stderr unless capture was split
    stderr: str = ""
    duration: float = 0.0
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    @property
    def display(self) -> str:
        return REDACTOR.redact_argv(self.argv)


@dataclass
class PromptResponse:
    """Answer `secret` (or plain text) whenever `pattern` appears on the PTY."""

    pattern: str
    answer: str
    max_uses: int = 2
    uses: int = field(default=0, init=False)

    def __post_init__(self) -> None:
        self._re = re.compile(self.pattern)


class CommandInterrupted(SetupError):
    pass


class Runner:
    """Executes commands. Tests substitute a fake with the same interface."""

    def __init__(self, *, verbose: bool = False, dry_run: bool = False) -> None:
        self.verbose = verbose
        self.dry_run = dry_run
        self._current: subprocess.Popen[bytes] | None = None
        self._current_pid: int | None = None

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def which(program: str, path: str | None = None) -> str | None:
        import shutil

        return shutil.which(program, path=path)

    def _env(self, env: Mapping[str, str] | None, extra_path: Sequence[Path] | None) -> dict[str, str]:
        merged = dict(os.environ if env is None else env)
        if extra_path:
            parts = [str(p) for p in extra_path] + merged.get("PATH", "").split(os.pathsep)
            seen: list[str] = []
            for part in parts:
                if part and part not in seen:
                    seen.append(part)
            merged["PATH"] = os.pathsep.join(seen)
        # Keep child output stable and parseable.
        merged.setdefault("LC_ALL", "C.UTF-8")
        merged.setdefault("PYTHONUNBUFFERED", "1")
        return merged

    def terminate_current(self) -> None:
        pid = self._current_pid
        if pid is None:
            return
        try:
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                os.killpg(pid, 0)
            except (ProcessLookupError, PermissionError):
                return
            time.sleep(0.2)
        try:
            os.killpg(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    # --------------------------------------------------------------------- run
    def run(
        self,
        argv: Sequence[str],
        *,
        phase: str = "",
        cwd: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        extra_path: Sequence[Path] | None = None,
        timeout: float | None = None,
        input_text: str | None = None,
        check: bool = False,
        on_line: LineCallback | None = None,
        new_session: bool = True,
        retries: int = 0,
        retry_on: Callable[[CommandResult], bool] | None = None,
        quiet: bool = False,
    ) -> CommandResult:
        """Run argv; stream combined output to the log; optionally retry network failures."""
        argv = [str(a) for a in argv]
        attempt = 0
        log = get_logger(phase)
        while True:
            result = self._run_once(
                argv,
                log=log,
                cwd=cwd,
                env=self._env(env, extra_path),
                timeout=timeout,
                input_text=input_text,
                on_line=on_line,
                new_session=new_session,
                quiet=quiet,
            )
            should_retry = retry_on(result) if retry_on else looks_like_network_error(result.output)
            if result.ok or attempt >= retries or not should_retry:
                break
            attempt += 1
            delay = min(2**attempt, 20)
            log.warning("retrying in %ss (attempt %s/%s): %s", delay, attempt, retries, result.display)
            time.sleep(delay)
        if check and not result.ok:
            raise command_error(result, phase=phase)
        return result

    def _run_once(
        self,
        argv: list[str],
        *,
        log: object,
        cwd: Path | str | None,
        env: dict[str, str],
        timeout: float | None,
        input_text: str | None,
        on_line: LineCallback | None,
        new_session: bool,
        quiet: bool,
    ) -> CommandResult:
        logger = log  # LoggerAdapter
        display = REDACTOR.redact_argv(argv)
        logger.info("$ %s%s", display, f"   (cwd={cwd})" if cwd else "")  # type: ignore[attr-defined]
        if self.dry_run:
            return CommandResult(argv, 0, "", duration=0.0)
        start = time.monotonic()
        tail: deque[str] = deque(maxlen=TAIL_LINES)
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(cwd) if cwd else None,
                env=env,
                stdin=subprocess.PIPE if input_text is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=new_session,
            )
        except FileNotFoundError:
            logger.error("command not found: %s", argv[0])  # type: ignore[attr-defined]
            return CommandResult(argv, 127, f"{argv[0]}: command not found", duration=0.0)
        except PermissionError as exc:
            logger.error("cannot execute %s: %s", argv[0], exc)  # type: ignore[attr-defined]
            return CommandResult(argv, 126, str(exc), duration=0.0)

        self._current, self._current_pid = proc, proc.pid if new_session else None
        timed_out = False
        try:
            if input_text is not None and proc.stdin:
                try:
                    proc.stdin.write(input_text.encode())
                    proc.stdin.close()
                except BrokenPipeError:
                    pass
            assert proc.stdout is not None
            timed_out = self._pump(proc.stdout, proc, tail, logger, on_line, timeout, start, quiet)
            if timed_out:
                self.terminate_current() if new_session else proc.kill()
            proc.wait()
        except KeyboardInterrupt:
            logger.warning("interrupted by user; terminating: %s", display)  # type: ignore[attr-defined]
            self.terminate_current() if new_session else proc.send_signal(signal.SIGINT)
            proc.wait()
            raise
        finally:
            self._current = self._current_pid = None
        duration = time.monotonic() - start
        rc = proc.returncode if not timed_out else 124
        logger.info("exit %s after %.1fs%s", rc, duration, " (timeout)" if timed_out else "")  # type: ignore[attr-defined]
        return CommandResult(argv, rc, "\n".join(tail), duration=duration, timed_out=timed_out)

    def _pump(self, stream, proc, tail, logger, on_line, timeout, start, quiet) -> bool:  # type: ignore[no-untyped-def]
        sel = selectors.DefaultSelector()
        sel.register(stream, selectors.EVENT_READ)
        buffer = b""
        try:
            while True:
                if timeout is not None and time.monotonic() - start > timeout:
                    return True
                events = sel.select(timeout=0.5)
                if not events:
                    # No pending data and the child is gone. Do not block on EOF: daemons the
                    # child started may still hold the pipe open.
                    if proc.poll() is not None:
                        break
                    continue
                chunk = os.read(stream.fileno(), 65536)
                if not chunk:
                    break
                buffer += chunk
                buffer = self._emit_lines(buffer, tail, logger, on_line, quiet)
            if buffer:
                self._emit_lines(buffer + b"\n", tail, logger, on_line, quiet)
        finally:
            sel.close()
        return False

    @staticmethod
    def _emit_lines(buffer: bytes, tail, logger, on_line, quiet) -> bytes:  # type: ignore[no-untyped-def]
        parts = re.split(rb"\r\n|\n|\r", buffer)
        rest = parts.pop()
        for raw in parts:
            line = raw.decode("utf-8", errors="replace").rstrip()
            if not line:
                continue
            tail.append(line)
            if not quiet:
                logger.debug("| %s", line)
            if on_line:
                on_line(REDACTOR.redact(line))
        return rest

    # --------------------------------------------------------------------- pty
    def run_with_prompts(
        self,
        argv: Sequence[str],
        responses: Sequence[PromptResponse],
        *,
        phase: str = "",
        cwd: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        extra_path: Sequence[Path] | None = None,
        timeout: float | None = None,
        on_line: LineCallback | None = None,
    ) -> CommandResult:
        """Run argv on a pseudo-terminal and answer matching prompts.

        Used for Frappe commands that read passwords with getpass(); this keeps secrets out of
        argv (visible in `ps`) and out of shell history. A prompt repeated more than
        `max_uses` times (e.g. a rejected password) aborts the command.
        """
        import fcntl
        import pty
        import termios

        argv = [str(a) for a in argv]
        log = get_logger(phase)
        display = REDACTOR.redact_argv(argv)
        log.info("$ %s   (pty%s)", display, f", cwd={cwd}" if cwd else "")
        for r in responses:
            REDACTOR.register(r.answer)
        if self.dry_run:
            return CommandResult(argv, 0, "")

        master, slave = pty.openpty()
        # Wide terminal so progress output is not wrapped mid-prompt.
        try:
            import struct

            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 50, 200, 0, 0))
        except OSError:
            pass

        def _make_controlling_tty() -> None:  # runs in the child after setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)

        start = time.monotonic()
        tail: deque[str] = deque(maxlen=TAIL_LINES)
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(cwd) if cwd else None,
                env=self._env(env, extra_path),
                stdin=slave,
                stdout=slave,
                stderr=slave,
                start_new_session=True,
                preexec_fn=_make_controlling_tty,  # noqa: PLW1509
            )
        except FileNotFoundError:
            os.close(master)
            os.close(slave)
            return CommandResult(argv, 127, f"{argv[0]}: command not found")
        os.close(slave)
        self._current, self._current_pid = proc, proc.pid
        pending = ""  # text since last newline, scanned for prompts
        timed_out = False
        failure: str | None = None
        try:
            while True:
                if timeout is not None and time.monotonic() - start > timeout:
                    timed_out = True
                    break
                try:
                    import select

                    ready, _, _ = select.select([master], [], [], 0.5)
                except InterruptedError:
                    continue
                if not ready:
                    if proc.poll() is not None:
                        # Child exited; drain remaining output once more.
                        try:
                            ready, _, _ = select.select([master], [], [], 0.2)
                        except OSError:
                            ready = []
                        if not ready:
                            break
                    else:
                        continue
                try:
                    chunk = os.read(master, 65536)
                except OSError as exc:
                    if exc.errno == errno.EIO:  # slave closed: child finished
                        break
                    raise
                if not chunk:
                    break
                text = chunk.decode("utf-8", errors="replace")
                pending += text
                lines = re.split(r"\r\n|\n|\r", pending)
                pending = lines.pop()
                for line in lines:
                    line = line.rstrip()
                    if line:
                        tail.append(line)
                        log.debug("| %s", line)
                        if on_line:
                            on_line(REDACTOR.redact(line))
                for response in responses:
                    if response._re.search(pending):
                        response.uses += 1
                        log.info("answering prompt %r (%s/%s)", response.pattern, response.uses, response.max_uses)
                        if response.uses > response.max_uses:
                            failure = f"prompt {response.pattern!r} repeated; the value was rejected"
                            break
                        os.write(master, (response.answer + "\n").encode())
                        tail.append(pending.strip())
                        pending = ""
                        break
                if failure:
                    break
            if timed_out or failure:
                self.terminate_current()
            proc.wait()
        except KeyboardInterrupt:
            log.warning("interrupted by user; terminating: %s", display)
            self.terminate_current()
            proc.wait()
            raise
        finally:
            self._current = self._current_pid = None
            try:
                os.close(master)
            except OSError:
                pass
        if pending.strip():
            tail.append(pending.strip())
        rc = proc.returncode
        if timed_out:
            rc = 124
        elif failure:
            tail.append(failure)
            rc = rc if rc not in (0, None) else 1
        duration = time.monotonic() - start
        log.info("exit %s after %.1fs", rc, duration)
        return CommandResult(argv, rc, "\n".join(tail), duration=duration, timed_out=timed_out)


def command_error(
    result: CommandResult,
    *,
    phase: str,
    message: str | None = None,
    remedy: str | None = None,
    retry_safe: bool | None = None,
) -> SetupError:
    """Build a SetupError from a failed command, enriched by the diagnosis knowledge base."""
    from .diagnosis import diagnose

    finding = diagnose(result.output)
    log = current_log_file()
    if result.timed_out:
        cause = "The command did not finish within its time limit."
    else:
        cause = finding.cause if finding else None
    return SetupError(
        message=message or f"Command failed with exit code {result.returncode}",
        phase=phase,
        command=result.display,
        exit_code=result.returncode,
        log_file=str(log) if log else None,
        cause=cause,
        remedy=remedy or (finding.remedy if finding else None),
        retry_safe=retry_safe if retry_safe is not None else (finding.retry_safe if finding else None),
        details=[REDACTOR.redact(line) for line in result.output.splitlines()[-15:]],
    )


__all__ = [
    "CommandInterrupted",
    "CommandResult",
    "PromptResponse",
    "Runner",
    "command_error",
    "format_argv",
    "looks_like_network_error",
]
