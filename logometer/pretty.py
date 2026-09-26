"""
Nicer terminal rendering using `rich`, used automatically when it's
installed and we're writing to a real terminal. Falls back to the
plain ANSI formatting in cli.py otherwise — this module is never
required, only opportunistic.

NOTE: this module is exercised by the fallback-detection path in
cli.py, but rendering itself could not be visually verified in the
environment this was built in (no network access to install `rich`).
The plain-text path is the one that's been thoroughly tested end to
end — treat this as a reasonable-effort addition, and file an issue
if the layout looks off in your terminal.
"""
from __future__ import annotations

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.text import Text
    HAS_RICH = True
except ImportError:
    HAS_RICH = False


def render_window(window, verdict, explanation: str | None, console: "Console") -> None:
    """Render one window's verdict with rich. Only call this if
    HAS_RICH is True."""
    label = f"{window.start_label} \u2013 {window.end_label}"

    if not verdict.is_anomaly:
        text = Text(f"  [{label}]  ok   errors: {window.error_count}   baseline: ~{verdict.baseline_mean:.1f}")
        text.stylize("dim")
        console.print(text)
        return

    body = Text()
    body.append(f"errors: {window.error_count}   ", style="bold")
    if window.warn_count:
        # a first-seen WARN shape can flag a window with zero errors
        body.append(f"warns: {window.warn_count}   ", style="bold")
    body.append(f"baseline: ~{verdict.baseline_mean:.1f}")
    if verdict.error_score:
        body.append(f"   score: {verdict.error_score:.1f}x", style="bold red" if verdict.error_score >= 3 else "yellow")

    if verdict.new_shapes:
        body.append("\n")
        levels = {l.shape: l.level for l in window.lines if l.shape}
        for shape in verdict.new_shapes:
            kind = "warning" if levels.get(shape) == "WARN" else "error"
            body.append(f"\n  New {kind} signature: ", style="yellow")
            body.append(shape, style="italic")
    elif not explanation:
        body.append("\n\n  (error rate spike \u2014 no brand-new error signature)", style="dim")

    if explanation:
        body.append("\n\n  ")
        body.append(explanation, style="cyan")

    console.print(Panel(body, title=f"\u26a0 ANOMALY  [{label}]", border_style="red", expand=False))
