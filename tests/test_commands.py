"""Real subprocess behaviour of the Runner, using harmless commands only."""

from __future__ import annotations

import logging
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from frappe_setup.commands import PromptResponse, Runner, command_error, looks_like_network_error
from frappe_setup.logging_config import setup_logging
from frappe_setup.redaction import REDACTOR

PY = sys.executable


def flush_logs() -> None:
    for h in logging.getLogger("frappe_setup").handlers:
        h.flush()


def test_success_and_output() -> None:
    r = Runner().run([PY, "-c", "print('hello'); print('world')"], timeout=30)
    assert r.ok and r.output.splitlines() == ["hello", "world"]


def test_failure_exit_code_and_stderr_merged() -> None:
    r = Runner().run([PY, "-c", "import sys; print('bad', file=sys.stderr); sys.exit(3)"], timeout=30)
    assert r.returncode == 3 and "bad" in r.output and not r.ok


def test_command_not_found() -> None:
    r = Runner().run(["definitely-not-a-command-xyz"], timeout=30)
    assert r.returncode == 127


def test_timeout_kills_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "child-alive"
    script = textwrap.dedent(
        f"""
        import subprocess, sys, time
        subprocess.Popen([sys.executable, "-c", "import time,pathlib; time.sleep(3); pathlib.Path({str(marker)!r}).write_text('x')"])
        time.sleep(30)
        """
    )
    start = time.monotonic()
    r = Runner().run([PY, "-c", script], timeout=1)
    assert r.timed_out and r.returncode == 124
    assert time.monotonic() - start < 15
    time.sleep(3.5)
    assert not marker.exists(), "grandchild survived the timeout"


def test_check_raises_setup_error_with_context() -> None:
    setup_logging("unit")
    from frappe_setup.errors import SetupError

    with pytest.raises(SetupError) as exc:
        Runner().run([PY, "-c", "import sys; print('No space left on device'); sys.exit(1)"], check=True, phase="bench")
    err = exc.value
    assert err.phase == "bench" and err.exit_code == 1
    assert err.cause and "disk" in err.cause.lower()
    assert err.retry_safe is True
    assert err.log_file


def test_long_output_is_bounded_but_capture_is_complete() -> None:
    code = "for i in range(5000): print(i)"
    streamed = Runner().run([PY, "-c", code], timeout=60)
    assert len(streamed.output.splitlines()) <= 200 and streamed.output.splitlines()[-1] == "4999"
    captured = Runner().capture([PY, "-c", code], timeout=60)
    assert captured.output.splitlines() == [str(i) for i in range(5000)]


def test_capture_preserves_blank_lines_and_separates_stderr() -> None:
    r = Runner().capture([PY, "-c", "import sys; print('a\\n\\nb'); print('err', file=sys.stderr)"])
    assert r.output == "a\n\nb\n" and r.stderr.strip() == "err"


def test_on_line_callback_receives_redacted_lines() -> None:
    REDACTOR.register("TopSecret123")
    seen: list[str] = []
    Runner().run([PY, "-c", "print('pw TopSecret123 here')"], on_line=seen.append, timeout=30)
    assert seen == ["pw ******** here"]


def test_network_retry(tmp_path: Path) -> None:
    counter = tmp_path / "count"
    script = textwrap.dedent(
        f"""
        import pathlib, sys
        p = pathlib.Path({str(counter)!r})
        n = int(p.read_text()) if p.exists() else 0
        p.write_text(str(n + 1))
        if n < 1:
            print("fatal: unable to access 'https://x/': Could not resolve host: x"); sys.exit(128)
        print("ok")
        """
    )
    r = Runner().run([PY, "-c", script], retries=2, timeout=60)
    assert r.ok and counter.read_text() == "2"


def test_non_network_failure_is_not_retried(tmp_path: Path) -> None:
    counter = tmp_path / "count"
    script = f"import pathlib,sys; p=pathlib.Path({str(counter)!r}); p.write_text(p.read_text()+'x' if p.exists() else 'x'); sys.exit(1)"
    Runner().run([PY, "-c", script], retries=3, timeout=30)
    assert counter.read_text() == "x"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Could not resolve host: github.com", True),
        ("npm ERR! code ETIMEDOUT", True),
        ("fatal: early EOF", True),
        ("Access denied for user 'root'", False),
        ("", False),
    ],
)
def test_network_classifier(text: str, expected: bool) -> None:
    assert looks_like_network_error(text) is expected


GETPASS_PROGRAM = textwrap.dedent(
    """
    import getpass, hashlib, sys
    h = lambda s: hashlib.sha256(s.encode()).hexdigest()
    root = getpass.getpass("MySQL root password: ")
    a = getpass.getpass("Set Administrator password: ")
    b = getpass.getpass("Re-enter Administrator password: ")
    print("tty" if sys.stdin.isatty() else "notty")
    print("root-ok" if h(root) == "3f6d41580ba447041da235f7b9cfa943587bccea9964fef5b3fa9930356dc307" else "root-bad")
    print("admin-ok" if a == b and h(a) == "eb665939d17a4708a04333c04ea361d97468b7b4b57095831bfa325ec220da2c" else "admin-bad")
    """
)


def test_pty_answers_getpass_prompts_without_argv_exposure() -> None:
    path = setup_logging("unit")
    responses = [
        PromptResponse(r"MySQL root password:\s*$", "rootpw-123"),
        PromptResponse(r"Set Administrator password:\s*$", "adminpw-456"),
        PromptResponse(r"Re-enter Administrator password:\s*$", "adminpw-456"),
    ]
    r = Runner().run_with_prompts([PY, "-c", GETPASS_PROGRAM], responses, timeout=60)
    assert r.ok, r.output
    assert "tty" in r.output and "root-ok" in r.output and "admin-ok" in r.output
    assert "rootpw-123" not in " ".join(r.argv)
    flush_logs()
    log = path.read_text()
    assert "rootpw-123" not in log and "adminpw-456" not in log
    assert "rootpw-123" not in r.output and "adminpw-456" not in r.output  # echo was off


def test_pty_rejected_password_aborts_instead_of_looping() -> None:
    program = "import getpass\nwhile True:\n    getpass.getpass('MySQL root password: ')\n    print('Access denied')\n"
    r = Runner().run_with_prompts(
        [PY, "-c", program], [PromptResponse(r"MySQL root password:\s*$", "wrong-pw", max_uses=2)], timeout=60
    )
    assert not r.ok
    assert "repeated" in r.output


def test_pty_timeout() -> None:
    r = Runner().run_with_prompts([PY, "-c", "import time; time.sleep(30)"], [], timeout=1)
    assert r.timed_out and r.returncode == 124


def test_interrupt_terminates_child(tmp_path: Path) -> None:
    runner = Runner()
    result: dict[str, object] = {}

    def target() -> None:
        try:
            runner.run([PY, "-c", "import time; time.sleep(60)"], timeout=120)
        except KeyboardInterrupt:  # pragma: no cover - not raised in a thread
            result["kbi"] = True

    t = threading.Thread(target=target)
    t.start()
    for _ in range(50):
        if runner._current_pid:
            break
        time.sleep(0.1)
    assert runner._current_pid
    start = time.monotonic()
    runner.terminate_current()
    t.join(timeout=20)
    assert not t.is_alive() and time.monotonic() - start < 15


def test_command_error_redacts_command_and_details() -> None:
    from frappe_setup.commands import CommandResult

    REDACTOR.register("Secr3t-Value")
    res = CommandResult(["tool", "--admin-password", "Secr3t-Value"], 2, "failed with Secr3t-Value")
    err = command_error(res, phase="site")
    assert "Secr3t-Value" not in (err.command or "")
    assert all("Secr3t-Value" not in d for d in err.details)
