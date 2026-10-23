"""Terminal presentation: banner, steps, prompts, progress, tables.

Modules receive a `UI` object and never print directly, so tests can use `ScriptedUI`.
"""

from __future__ import annotations

import contextlib
import getpass
import sys
import time
from collections.abc import Callable, Iterator, Sequence
from typing import Any

from rich.console import Console
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text

from .compatibility import FAIL, INFO, MISSING, OK, WARN, CheckItem
from .errors import SetupError, UserAbort, ValidationError

BANNER = """\
╔══════════════════════════════════════════════╗
║               FRAPPE SETUP CLI               ║
║  Guided Development Environment Installer    ║
╚══════════════════════════════════════════════╝"""

_STATUS_STYLE = {
    OK: ("✔", "green"),
    WARN: ("!", "yellow"),
    FAIL: ("✘", "red"),
    MISSING: ("✘", "red"),
    INFO: ("i", "cyan"),
}


def _cell(value: str) -> Text:
    """Values are plain text (so '[system]' is shown literally) unless they carry closing
    rich markup tags written by this program, e.g. '[red](missing)[/]'."""
    return Text.from_markup(value) if "[/]" in value else Text(value)


class UI:
    """Interactive rich-based UI."""

    interactive = True

    def __init__(self, *, assume_yes: bool = False, verbose: bool = False, console: Console | None = None) -> None:
        self.console = console or Console(highlight=False)
        self.err = Console(stderr=True, highlight=False)
        self.assume_yes = assume_yes
        self.verbose = verbose
        self._step_total = 0

    # ------------------------------------------------------------ output
    def banner(self) -> None:
        self.console.print(Text(BANNER, style="bold cyan"))

    def set_steps(self, total: int) -> None:
        self._step_total = total

    def step(self, number: int, title: str) -> None:
        total = f"/{self._step_total}" if self._step_total else ""
        self.console.print()
        self.console.rule(f"[bold]Step {number}{total}: {title}", align="left", style="cyan")

    def info(self, message: str) -> None:
        self.console.print(message)

    def success(self, message: str) -> None:
        self.console.print(f"[green]✔[/] {message}")

    def warn(self, message: str) -> None:
        self.console.print(f"[yellow]![/] {message}")

    def error(self, message: str) -> None:
        self.err.print(f"[red]✘[/] {message}")

    def check_table(self, items: Sequence[CheckItem], title: str = "") -> None:
        table = Table(title=title or None, show_lines=False, expand=False, title_justify="left")
        table.add_column("", width=1)
        table.add_column("Check", style="bold")
        table.add_column("Found")
        table.add_column("Required")
        table.add_column("Notes")
        for item in items:
            symbol, style = _STATUS_STYLE.get(item.status, ("?", "white"))
            if item.status in (FAIL, MISSING) and item.fixable:
                symbol, style = "↻", "yellow"
            note = item.message
            if item.status in (FAIL, MISSING, WARN) and item.remedy:
                note = f"{note} → {item.remedy}" if note else item.remedy
            table.add_row(Text(symbol, style=style), Text(item.name), Text(item.found), Text(item.required), Text(note))
        self.console.print(table)

    def key_values(self, rows: Sequence[tuple[str, str]], title: str = "") -> None:
        table = Table(show_header=False, box=None, title=title or None, title_justify="left", pad_edge=False)
        table.add_column(style="bold cyan")
        table.add_column()
        for key, value in rows:
            table.add_row(Text(key), _cell(value))
        self.console.print(table)

    def panel(self, body: str, title: str = "", style: str = "cyan") -> None:
        self.console.print(Panel(body, title=title or None, border_style=style, expand=False))

    def show_error(self, exc: SetupError) -> None:
        lines = [f"[bold red]{exc.message}[/]"]
        if exc.phase:
            lines.append(f"[bold]Phase:[/]       {exc.phase}")
        if exc.command:
            lines.append(f"[bold]Command:[/]     {exc.command}")
        if exc.exit_code is not None:
            lines.append(f"[bold]Exit code:[/]   {exc.exit_code}")
        lines.append(f"[bold]Likely cause:[/] {exc.cause or 'Unknown — see the log for the full output.'}")
        if exc.remedy:
            lines.append(f"[bold]Fix:[/]         {exc.remedy}")
        if exc.retry_safe is not None:
            lines.append(f"[bold]Safe to retry:[/] {'yes' if exc.retry_safe else 'no — fix the cause first'}")
        if exc.log_file:
            lines.append(f"[bold]Log file:[/]    {exc.log_file}")
        if exc.details:
            lines.append("[bold]Last output:[/]")
            lines.extend(f"  [dim]{Text(d).plain}[/]" for d in exc.details[-10:])
        self.err.print(Panel("\n".join(lines), title="Setup failed", border_style="red", expand=False))

    # ------------------------------------------------------------ input
    def ask(
        self,
        prompt: str,
        default: str | None = None,
        validate: Callable[[str], Any] | None = None,
    ) -> str:
        while True:
            suffix = escape(f" [{default}]") if default else ""
            try:
                value = self.console.input(f"[bold]{prompt}[/]{suffix}: ").strip()
            except EOFError as exc:
                raise UserAbort("Input closed") from exc
            if not value and default is not None:
                value = default
            if validate is None:
                return value
            try:
                result = validate(value)
                return result if isinstance(result, str) else value
            except ValidationError as exc:
                self.error(str(exc))

    def ask_secret(self, prompt: str, *, confirm: bool = False, validate: Callable[[str], Any] | None = None) -> str:
        while True:
            try:
                value = getpass.getpass(f"{prompt}: ")
            except EOFError as exc:
                raise UserAbort("Input closed") from exc
            if validate:
                try:
                    validate(value)
                except ValidationError as exc:
                    self.error(str(exc))
                    continue
            if confirm:
                again = getpass.getpass("Confirm: ")
                if again != value:
                    self.error("Values do not match.")
                    continue
            return value

    def confirm(self, prompt: str, default: bool = False) -> bool:
        if self.assume_yes:
            self.console.print(f"[bold]{prompt}[/] [dim](--yes)[/] yes")
            return True
        hint = "Y/n" if default else "y/N"
        while True:
            try:
                value = self.console.input(f"[bold]{prompt}[/] [{hint}]: ").strip().lower()
            except EOFError as exc:
                raise UserAbort("Input closed") from exc
            if not value:
                return default
            if value in ("y", "yes"):
                return True
            if value in ("n", "no"):
                return False

    def choose(self, prompt: str, options: Sequence[tuple[str, str]], default: str | None = None) -> str:
        """options: (value, label). Returns the chosen value."""
        for index, (_value, label) in enumerate(options, 1):
            self.console.print(f"  [cyan]{index}[/]) {escape(label)}")
        values = [o[0] for o in options]
        default_index = str(values.index(default) + 1) if default in values else None
        while True:
            answer = self.ask(prompt, default_index)
            if answer.isdigit() and 1 <= int(answer) <= len(options):
                return values[int(answer) - 1]
            if answer in values:
                return answer
            self.error(f"Choose 1-{len(options)}.")

    def choose_many(self, prompt: str, options: Sequence[tuple[str, str]]) -> list[str]:
        for index, (_value, label) in enumerate(options, 1):
            self.console.print(f"  [cyan]{index}[/]) {escape(label)}")
        values = [o[0] for o in options]
        while True:
            answer = self.ask(f"{prompt} (comma-separated numbers, empty for none)", "")
            if not answer:
                return []
            picked: list[str] = []
            ok = True
            for part in answer.replace(" ", "").split(","):
                if part.isdigit() and 1 <= int(part) <= len(values):
                    if values[int(part) - 1] not in picked:
                        picked.append(values[int(part) - 1])
                else:
                    ok = False
            if ok:
                return picked
            self.error(f"Use numbers between 1 and {len(values)}.")

    # ------------------------------------------------------------ progress
    @contextlib.contextmanager
    def progress(self, title: str) -> Iterator[Callable[[str], None]]:
        """Spinner with the latest output line; yields a callback for command output."""
        if self.verbose or not self.console.is_terminal:
            self.console.print(f"[cyan]…[/] {title}")
            yield (
                (lambda line: self.console.print(f"  [dim]{Text(line).plain}[/]"))
                if self.verbose
                else (lambda _l: None)
            )
            return
        start = time.monotonic()
        last = {"line": ""}
        spinner = Spinner("dots", text=title)

        def render() -> Text:
            elapsed = int(time.monotonic() - start)
            t = Text.assemble((f"{title} ", "bold"), (f"[{elapsed // 60}:{elapsed % 60:02d}]", "dim"))
            if last["line"]:
                width = max(self.console.width - 6, 20)
                t.append("\n   " + last["line"][:width], style="dim")
            return t

        with Live(spinner, console=self.console, refresh_per_second=8, transient=True) as live:

            def on_line(line: str) -> None:
                last["line"] = line.strip()
                spinner.update(text=render())

            spinner.update(text=render())
            try:
                yield on_line
            finally:
                live.update(Text(""))
        self.console.print(f"[green]✔[/] {title} [dim]({int(time.monotonic() - start)}s)[/]")


class ScriptedUI(UI):
    """Non-interactive UI: answers come from a script; unexpected questions fail loudly."""

    interactive = False

    def __init__(
        self, answers: dict[str, Any] | None = None, *, assume_yes: bool = False, console: Console | None = None
    ) -> None:
        super().__init__(
            assume_yes=assume_yes, console=console or Console(file=sys.stdout, force_terminal=False, width=120)
        )
        self.answers = dict(answers or {})
        self.asked: list[str] = []

    def _lookup(self, prompt: str) -> Any:
        self.asked.append(prompt)
        for key, value in self.answers.items():
            if key in prompt:
                return value
        return None

    def ask(self, prompt: str, default: str | None = None, validate: Callable[[str], Any] | None = None) -> str:
        value = self._lookup(prompt)
        if value is None:
            if default is None:
                raise UserAbort(f"Non-interactive mode: no value for {prompt!r}")
            value = default
        if validate:
            result = validate(str(value))
            return result if isinstance(result, str) else str(value)
        return str(value)

    def ask_secret(self, prompt: str, *, confirm: bool = False, validate: Callable[[str], Any] | None = None) -> str:
        value = self._lookup(prompt)
        if value is None:
            raise UserAbort(f"Non-interactive mode: no value for secret {prompt!r}")
        if validate:
            validate(str(value))
        return str(value)

    def confirm(self, prompt: str, default: bool = False) -> bool:
        value = self._lookup(prompt)
        if value is not None:
            return bool(value)
        if self.assume_yes:
            return True
        raise UserAbort(f"Non-interactive mode: confirmation required for {prompt!r} (use --yes)")

    def choose(self, prompt: str, options: Sequence[tuple[str, str]], default: str | None = None) -> str:
        value = self._lookup(prompt)
        if value is None:
            if default is None:
                raise UserAbort(f"Non-interactive mode: no choice for {prompt!r}")
            return default
        if value not in [o[0] for o in options]:
            raise UserAbort(f"Non-interactive mode: {value!r} is not a valid choice for {prompt!r}")
        return str(value)

    def choose_many(self, prompt: str, options: Sequence[tuple[str, str]]) -> list[str]:
        value = self._lookup(prompt)
        return list(value or [])
