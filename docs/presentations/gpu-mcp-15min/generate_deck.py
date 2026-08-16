#!/usr/bin/env python3
"""Generate the GPU MCP deck as PDF, PNG slides, and browser slides."""

from __future__ import annotations

import argparse
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.backends.backend_pdf import PdfPages
from matplotlib.patches import Circle, FancyBboxPatch, Rectangle


BG = "#071827"
PANEL = "#10283a"
PANEL_2 = "#16384b"
WHITE = "#f6fafc"
MUTED = "#a9bdc8"
TEAL = "#39d6c3"
CYAN = "#55b8ff"
PINK = "#ff66a6"
YELLOW = "#ffd166"
GREEN = "#63d49a"
RED = "#ff7272"
INK = "#071827"


class Slide:
    def __init__(self, number: int, title: str | None = None, *, title_size: float = 27) -> None:
        self.number = number
        self.fig = plt.figure(figsize=(13.333, 7.5), facecolor=BG)
        self.ax = self.fig.add_axes((0, 0, 1, 1))
        self.ax.set_xlim(0, 16)
        self.ax.set_ylim(9, 0)
        self.ax.axis("off")
        self.ax.set_facecolor(BG)
        if title:
            self.text(0.72, 0.45, title, title_size, WHITE, weight="bold")
            self.rect(0.72, 1.13, 1.12, 0.045, TEAL, radius=0)
            self.text(0.72, 8.62, "GPU MCP  •  polymer simulation group", 8.5, MUTED)
            self.text(15.15, 8.61, str(number), 9, MUTED, ha="center")

    def text(
        self,
        x: float,
        y: float,
        value: str,
        size: float,
        color: str = WHITE,
        *,
        weight: str = "normal",
        ha: str = "left",
        va: str = "top",
        family: str = "DejaVu Sans",
        linespacing: float = 1.18,
    ) -> None:
        self.ax.text(
            x,
            y,
            value,
            fontsize=size,
            color=color,
            fontweight=weight,
            horizontalalignment=ha,
            verticalalignment=va,
            fontfamily=family,
            linespacing=linespacing,
            transform=self.ax.transData,
        )

    def rect(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        fill: str,
        *,
        edge: str | None = None,
        lw: float = 1.0,
        radius: float = 0.14,
        alpha: float = 1.0,
    ) -> None:
        if radius <= 0:
            patch = Rectangle(
                (x, y),
                w,
                h,
                facecolor=fill,
                edgecolor="none" if edge is None else edge,
                linewidth=lw,
                alpha=alpha,
            )
        else:
            patch = FancyBboxPatch(
                (x, y),
                w,
                h,
                boxstyle=f"round,pad=0.01,rounding_size={radius}",
                facecolor=fill,
                edgecolor="none" if edge is None else edge,
                linewidth=lw,
                alpha=alpha,
            )
        self.ax.add_patch(patch)

    def circle(
        self,
        x: float,
        y: float,
        radius: float,
        fill: str,
        *,
        edge: str | None = None,
        lw: float = 1.0,
    ) -> None:
        self.ax.add_patch(
            Circle(
                (x, y),
                radius,
                facecolor=fill,
                edgecolor="none" if edge is None else edge,
                linewidth=lw,
            )
        )

    def line(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        color: str = MUTED,
        lw: float = 2,
        *,
        zorder: float = 2,
    ) -> None:
        self.ax.plot(
            [x1, x2],
            [y1, y2],
            color=color,
            linewidth=lw,
            solid_capstyle="round",
            zorder=zorder,
        )

    def pill(
        self,
        x: float,
        y: float,
        w: float,
        h: float,
        label: str,
        fill: str,
        color: str = WHITE,
        size: float = 9,
    ) -> None:
        self.rect(x, y, w, h, fill, radius=h / 2)
        self.text(x + w / 2, y + h / 2, label, size, color, weight="bold", ha="center", va="center")

    def number_dot(self, x: float, y: float, number: str, color: str) -> None:
        self.circle(x, y, 0.27, color)
        self.text(x, y + 0.005, number, 14, INK, weight="bold", ha="center", va="center")


def polymer_chain(slide: Slide, points: list[tuple[float, float]]) -> None:
    colors = [PINK, CYAN, TEAL, YELLOW]
    for first, second in zip(points, points[1:]):
        slide.line(first[0], first[1], second[0], second[1], MUTED, 1.8)
    for index, (x, y) in enumerate(points):
        slide.circle(x, y, 0.15, colors[index % len(colors)], edge=BG, lw=1)


def slide_1() -> Slide:
    s = Slide(1)
    s.rect(0, 0, 0.45, 9, TEAL, radius=0)
    s.text(1.0, 1.25, "GPU MCP", 18, TEAL, weight="bold")
    s.text(1.0, 2.0, "Let Codex operate\nthe GPU workflow", 38, WHITE, weight="bold", linespacing=1.04)
    s.text(1.04, 4.25, "Safe hands for prolonged polymer-simulation investigation", 17, MUTED)
    s.pill(1.05, 5.35, 4.55, 0.48, "NOT JUST JOB SUBMISSION", PANEL_2, YELLOW, 10)
    s.pill(5.82, 5.35, 5.15, 0.48, "AN ITERATIVE SCIENCE LOOP", PANEL_2, TEAL, 10)

    s.rect(12.1, 1.95, 2.65, 3.65, PANEL, edge=CYAN, lw=1.8, radius=0.24)
    s.text(13.42, 2.38, "CODEX", 13, CYAN, weight="bold", ha="center")
    s.text(13.42, 3.47, "MCP", 25, WHITE, weight="bold", ha="center")
    s.text(13.42, 4.58, "GPU", 16, TEAL, weight="bold", ha="center")
    s.text(11.58, 3.42, "→", 24, MUTED, weight="bold", ha="center")
    s.text(15.22, 3.42, "→", 24, MUTED, weight="bold", ha="center")

    points = [
        (1.2, 7.25),
        (2.4, 6.88),
        (3.65, 7.34),
        (4.95, 6.9),
        (6.3, 7.28),
        (7.7, 6.82),
        (9.0, 7.25),
        (10.4, 6.88),
        (11.75, 7.28),
        (13.15, 6.83),
        (14.65, 7.2),
    ]
    polymer_chain(s, points)
    s.text(1.0, 8.35, "Trusted lab GPU fleets  •  Python jobs  •  shared filesystems", 9.5, MUTED)
    return s


def slide_2() -> Slide:
    s = Slide(2, "MCP in one sentence")
    s.text(
        8,
        1.6,
        "MCP gives Codex a typed toolbelt connected to real systems.",
        22,
        WHITE,
        weight="bold",
        ha="center",
    )
    cards = [
        (0.72, "YOU", "State the\nscientific goal", PINK),
        (4.62, "CODEX", "Plans and\nchooses a tool", CYAN),
        (8.52, "MCP", "Validates a\ntyped call", TEAL),
        (12.42, "CLUSTER", "Acts and\nreturns facts", YELLOW),
    ]
    for x, title, body, color in cards:
        s.rect(x, 2.75, 2.85, 2.6, PANEL, edge=color, lw=1.6, radius=0.2)
        s.circle(x + 1.425, 3.37, 0.37, color)
        s.text(x + 1.425, 3.38, title[0], 17, INK, weight="bold", ha="center", va="center")
        s.text(x + 1.425, 4.07, title, 13.5, WHITE, weight="bold", ha="center")
        s.text(x + 1.425, 4.58, body, 10.5, MUTED, ha="center")
    for x in (4.03, 7.93, 11.83):
        s.text(x, 3.65, "→", 24, TEAL, weight="bold", ha="center")

    s.rect(1.65, 6.1, 12.7, 1.55, PANEL_2, edge=TEAL, lw=1.4, radius=0.22)
    s.text(2.2, 6.42, "MODEL CONTEXT PROTOCOL", 10.5, TEAL, weight="bold")
    s.text(2.2, 6.87, "Think “USB/API standard for AI tools” — not another model, and not magic autonomy.", 15, WHITE, weight="bold")
    s.text(2.2, 7.38, "MCP is the connection — the goal, reasoning, and scientific judgment remain separate.", 9.5, MUTED)
    return s


def slide_3() -> Slide:
    s = Slide(3, "The workflow friction we are removing")
    s.pill(0.85, 1.55, 6.65, 0.42, "TODAY: COMMAND-AND-REMEMBER", "#3a2430", RED, 9.5)
    s.pill(8.5, 1.55, 6.65, 0.42, "WITH GPU MCP: INVESTIGATE-AND-ITERATE", "#153c3a", TEAL, 9.5)
    manual = [
        "1   Check nvidia-smi on several hosts",
        "2   Pick a GPU from stale snapshots",
        "3   SSH / nohup / redirect logs",
        "4   Remember host, PID and output path",
        "5   Poll, interpret exit, clean up",
    ]
    managed = [
        "1   Define a goal + stopping condition",
        "2   Inspect code, inputs, GPUs, outputs",
        "3   Run bounded smoke / main experiments",
        "4   Analyze evidence; revise the setup",
        "5   Repeat until criterion or real blocker",
    ]
    for index, label in enumerate(manual):
        y = 2.28 + index * 0.83
        s.rect(0.85, y, 6.65, 0.62, PANEL, edge="#6b3746", lw=0.8, radius=0.1)
        s.text(1.15, y + 0.31, label, 10.5, WHITE if index < 2 else MUTED, va="center")
    for index, label in enumerate(managed):
        y = 2.28 + index * 0.83
        s.rect(8.5, y, 6.65, 0.62, PANEL, edge="#246e67", lw=0.8, radius=0.1)
        s.text(8.8, y + 0.31, label, 10.5, WHITE if index < 2 else MUTED, va="center")
    s.rect(0.85, 6.65, 14.3, 1.1, PANEL_2, radius=0.16)
    s.text(1.2, 6.94, "POLYMER-SIMULATION PAYOFF", 9.5, YELLOW, weight="bold")
    s.text(4.25, 6.88, "Not just less shepherding: a sustained investigate → run → learn loop on our machines.", 12.9, WHITE, weight="bold")
    return s


def slide_4() -> Slide:
    s = Slide(4, "What happens after you ask Codex")
    boxes = [
        (0.65, 2.48, 2.75, "CODEX CHAT", "control / login host", CYAN),
        (4.1, 2.48, 3.55, "GPU MCP SERVER", "typed tools + lifecycle", TEAL),
        (8.35, 2.48, 1.75, "SSH", "dedicated key", YELLOW),
        (10.8, 2.48, 4.3, "GPU HOST", "guarded Python runner", PINK),
    ]
    for x, y, w, title, body, color in boxes:
        s.rect(x, y, w, 1.6, PANEL, edge=color, lw=1.6, radius=0.18)
        s.text(x + w / 2, y + 0.4, title, 12, color, weight="bold", ha="center")
        s.text(x + w / 2, y + 1.02, body, 9.5, MUTED, ha="center")
    for x in (3.73, 8.02, 10.46):
        s.text(x, 3.12, "→", 22, TEAL, weight="bold", ha="center")

    s.rect(4.1, 4.72, 1.65, 1.4, PANEL_2, edge=CYAN, lw=1, radius=0.14)
    s.text(4.925, 5.03, "gpu-mcp.toml", 9.5, CYAN, weight="bold", ha="center")
    s.text(4.925, 5.52, "hosts +\npath policy", 8.5, MUTED, ha="center")
    s.rect(6.0, 4.72, 1.65, 1.4, PANEL_2, edge=TEAL, lw=1, radius=0.14)
    s.text(6.825, 5.03, "managed state", 9.5, TEAL, weight="bold", ha="center")
    s.text(6.825, 5.52, "reservation +\noutcome", 8.5, MUTED, ha="center")
    s.line(4.925, 4.08, 4.925, 4.72, MUTED, 1)
    s.line(6.825, 4.08, 6.825, 4.72, MUTED, 1)

    s.rect(10.8, 4.72, 4.3, 1.4, PANEL_2, edge=PINK, lw=1, radius=0.14)
    s.text(12.95, 5.03, "shared research repo", 9.5, PINK, weight="bold", ha="center")
    s.text(12.95, 5.52, "jobs/*.py  →  trajectories / logs", 8.6, MUTED, ha="center")
    s.line(12.95, 4.08, 12.95, 4.72, MUTED, 1)

    s.rect(1.55, 6.75, 12.9, 0.95, "#123346", edge=CYAN, lw=1.2, radius=0.18)
    s.text(8, 7.225, "MCP gives Codex safe machine-facing hands; the repo remains our scientific workspace.", 13.2, WHITE, weight="bold", ha="center", va="center")
    return s


def slide_5() -> Slide:
    s = Slide(5, "The MCP is an ensemble of tools")
    s.text(
        8,
        1.42,
        "No single tool is “the MCP” — the value comes from how the tools compose.",
        16.5,
        WHITE,
        weight="bold",
        ha="center",
    )
    groups = [
        (
            0.62,
            "DISCOVER",
            "What is happening?",
            [("cluster_info", "fleet snapshot"), ("check_gpus", "claimable GPUs")],
            CYAN,
        ),
        (
            4.47,
            "RUN + MANAGE",
            "Do the work",
            [("run_python_on_gpu", "reserve + launch"), ("manage_gpu_job", "lifecycle control")],
            TEAL,
        ),
        (
            8.32,
            "DIAGNOSE + RECOVER",
            "Resolve ambiguity",
            [
                ("check_gpu_processes", "inspect processes"),
                ("list_gpu_reservations", "inspect claims"),
                ("kill_gpu_process", "confirmed rescue"),
            ],
            PINK,
        ),
        (
            12.17,
            "GOVERN",
            "Change guardrails",
            [
                ("preview_policy_reload", "validate + diff"),
                ("reload_policy", "activate preview"),
                ("reject_policy_reload", "discard preview"),
            ],
            YELLOW,
        ),
    ]
    for x, title, question, tools, color in groups:
        s.rect(x, 2.16, 3.22, 4.48, PANEL, edge=color, lw=1.45, radius=0.2)
        s.rect(x, 2.16, 3.22, 0.59, color, radius=0.2)
        s.text(x + 1.61, 2.455, title, 8.8, INK, weight="bold", ha="center", va="center")
        s.text(x + 0.26, 3.04, question, 10.4, color, weight="bold")
        for index, (tool, purpose) in enumerate(tools):
            y = 3.72 + index * 0.91
            s.text(x + 0.26, y, tool, 8.6, WHITE, weight="bold", family="DejaVu Sans Mono")
            s.text(x + 0.26, y + 0.37, purpose, 8.6, MUTED)

    for x in (4.26, 8.11, 11.96):
        s.text(x, 4.38, "→", 19, TEAL, weight="bold", ha="center")

    s.rect(1.25, 7.15, 13.5, 0.74, PANEL_2, edge=TEAL, lw=1.2, radius=0.18)
    s.text(
        8,
        7.52,
        "The tools are the hands: observe  →  claim  →  run  →  verify  →  intervene  →  govern",
        13.2,
        WHITE,
        weight="bold",
        ha="center",
        va="center",
    )
    return s


def slide_6() -> Slide:
    s = Slide(6, "The everyday run loop: three tools")
    cards = [
        (
            0.62,
            "1",
            "check_gpus",
            "What can I claim now?",
            "Live GPU samples\n+ reservation overlay",
            "available • busy\nreserved • unknown",
            CYAN,
        ),
        (
            5.52,
            "2",
            "run_python_on_gpu",
            "Start this approved script",
            "Validate paths → reserve\natomically → launch",
            "job_id • reservation\noutput pointer • cadence",
            PINK,
        ),
        (
            10.42,
            "3",
            "manage_gpu_job",
            "What happened — or intervene",
            "status • stop • retry\nfinish • update_cadence",
            "lifecycle • outcome\nnext check",
            TEAL,
        ),
    ]
    for x, number, tool, question, action, result, color in cards:
        s.rect(x, 1.65, 4.35, 5.5, PANEL, edge=color, lw=1.55, radius=0.22)
        s.circle(x + 0.62, 2.27, 0.3, color)
        s.text(x + 0.62, 2.27, number, 14.5, INK, weight="bold", ha="center", va="center")
        s.text(x + 1.08, 2.0, tool, 11.5, color, weight="bold", family="DejaVu Sans Mono")
        s.text(x + 0.38, 2.95, question, 12.5, WHITE, weight="bold")
        s.text(x + 0.38, 3.69, "DOES", 8.5, color, weight="bold")
        s.text(x + 0.38, 4.08, action, 10.5, MUTED, linespacing=1.35)
        s.line(x + 0.38, 5.13, x + 3.97, 5.13, PANEL_2, 1.2)
        s.text(x + 0.38, 5.48, "RETURNS", 8.5, color, weight="bold")
        s.text(x + 0.38, 5.87, result, 10.5, WHITE, weight="bold", linespacing=1.35)
    for x in (5.25, 10.15):
        s.text(x, 4.24, "→", 21, TEAL, weight="bold", ha="center")
    s.text(
        8,
        7.66,
        "The job_id connects one experiment's launch to the evidence for the next decision.",
        12.5,
        YELLOW,
        weight="bold",
        ha="center",
    )
    return s


def slide_7() -> Slide:
    s = Slide(7, "Heartbeat = ownership lease, not process liveness")
    s.text(
        8,
        1.4,
        "It says “this MCP server still owns the reservation” — not “the Python process is alive.”",
        15.5,
        WHITE,
        weight="bold",
        ha="center",
    )

    boxes = [
        (
            0.72,
            "OWNER MCP SERVER",
            "One heartbeat per managed job\nBackground lease renewal\nOnly this server_instance_id writes",
            CYAN,
        ),
        (
            6.22,
            "SHARED RESERVATION",
            "server_instance_id\nlast_heartbeat_at\nheartbeat_interval_sec",
            TEAL,
        ),
        (
            11.72,
            "OTHER SESSIONS",
            "Read the lease\nNever adopt the old heartbeat\nInspect remotely only when stale",
            PINK,
        ),
    ]
    for x, title, body, color in boxes:
        s.rect(x, 2.18, 3.56, 2.25, PANEL, edge=color, lw=1.5, radius=0.2)
        s.text(x + 1.78, 2.58, title, 10.2, color, weight="bold", ha="center")
        s.text(
            x + 1.78,
            3.13,
            body,
            9.2,
            WHITE if title != "SHARED RESERVATION" else MUTED,
            weight="bold" if title == "SHARED RESERVATION" else "normal",
            family="DejaVu Sans Mono" if title == "SHARED RESERVATION" else "DejaVu Sans",
            ha="center",
            linespacing=1.38,
        )
    s.text(5.24, 3.13, "→", 24, TEAL, weight="bold", ha="center")
    s.text(10.74, 3.13, "→", 24, TEAL, weight="bold", ha="center")

    decisions = [
        (0.72, "FRESH LEASE", "GPU stays reserved", GREEN),
        (5.56, "STALE + ALIVE / UNKNOWN", "Still reserved; never guess", YELLOW),
        (10.4, "STALE + PROCESS-GONE PROOF", "Cleanup may release GPU", CYAN),
    ]
    for x, title, body, color in decisions:
        s.rect(x, 5.05, 4.32, 1.48, PANEL_2, edge=color, lw=1.25, radius=0.17)
        s.text(x + 2.16, 5.42, title, 9.2, color, weight="bold", ha="center")
        s.text(x + 2.16, 5.98, body, 11.2, WHITE, weight="bold", ha="center")

    s.rect(1.25, 7.12, 13.5, 0.83, "#3a2932", edge=PINK, lw=1.2, radius=0.18)
    s.text(
        8,
        7.535,
        "A missed heartbeat triggers inspection. It never proves that the GPU is free.",
        14.1,
        WHITE,
        weight="bold",
        ha="center",
        va="center",
    )
    return s


def slide_8() -> Slide:
    s = Slide(8, "Hooks are Codex’s attention layer")
    flow = [
        (0.72, 3.0, "LOCAL STATE", "outcome appears\nor check becomes due", PINK),
        (4.32, 3.05, "COMPANION HOOK", "notice — do not interpret", TEAL),
        (8.02, 2.55, "CODEX", "call status", CYAN),
        (11.22, 3.95, "MCP STATUS", "interpret • report\nfinalize • release", YELLOW),
    ]
    for x, w, title, body, color in flow:
        s.rect(x, 1.45, w, 1.52, PANEL, edge=color, lw=1.4, radius=0.17)
        s.text(x + w / 2, 1.82, title, 10, color, weight="bold", ha="center")
        s.text(x + w / 2, 2.35, body, 9.2, WHITE, weight="bold", ha="center")
    for x in (3.99, 7.69, 10.89):
        s.text(x, 2.05, "→", 21, TEAL, weight="bold", ha="center")

    branches = [
        (
            0.72,
            "POLICY GUARD",
            "Pre • Post • Stop\n\nStale or symlinked policy\nblocks normal work.",
            RED,
        ),
        (
            4.52,
            "PRETOOLUSE",
            "Outcome / due reminder\nSmoke-before-main nudge\nEarly-poll warning",
            CYAN,
        ),
        (
            8.32,
            "POSTTOOLUSE",
            "Recheck policy drift\n\nJob-reminder branches\nstay silent.",
            YELLOW,
        ),
        (
            12.12,
            "STOP",
            "Wait locally for outcome / due\nContinue the same open turn\nNo model turns while waiting",
            TEAL,
        ),
    ]
    for x, title, body, color in branches:
        s.rect(x, 3.52, 3.16, 2.85, PANEL, edge=color, lw=1.35, radius=0.18)
        s.rect(x, 3.52, 3.16, 0.54, color, radius=0.18)
        s.text(x + 1.58, 3.79, title, 9.3, INK, weight="bold", ha="center", va="center")
        s.text(x + 0.28, 4.45, body, 9.2, WHITE, linespacing=1.38)

    s.rect(1.08, 7.04, 13.84, 0.9, PANEL_2, edge=TEAL, lw=1.2, radius=0.18)
    s.text(
        8,
        7.49,
        "Hooks never SSH, parse outcomes, heartbeat, or release a GPU — status remains the authority.",
        12.5,
        WHITE,
        weight="bold",
        ha="center",
        va="center",
    )
    return s


def slide_9() -> Slide:
    s = Slide(9, "The supporting tools: see, recover, govern")
    columns = [
        (
            0.62,
            "SEE THE FLEET",
            CYAN,
            [
                ("cluster_info", "Reachability, GPU counts,\naverage utilization, load."),
                ("check_gpu_processes", "Host / GPU / PID / user / command\nand memory for compute processes."),
            ],
        ),
        (
            5.52,
            "RECOVER SAFELY",
            PINK,
            [
                ("list_gpu_reservations", "Active claims for me or everyone;\noptional bounded refresh."),
                ("kill_gpu_process", "Two-step, fingerprint-confirmed\nrescue for one owned PID."),
            ],
        ),
        (
            10.42,
            "GOVERN POLICY",
            YELLOW,
            [
                ("preview_policy_reload", "Validate the changed policy, show\nthe diff, issue a one-time token."),
                ("reload_policy", "Activate that exact approved hash."),
                ("reject_policy_reload", "Discard the token; change nothing."),
            ],
        ),
    ]
    for x, title, color, tools in columns:
        s.rect(x, 1.55, 4.35, 5.82, PANEL, edge=color, lw=1.5, radius=0.2)
        s.rect(x, 1.55, 4.35, 0.62, color, radius=0.2)
        s.text(x + 2.175, 1.86, title, 9.5, INK, weight="bold", ha="center", va="center")
        for index, (tool, purpose) in enumerate(tools):
            y = 2.62 + index * 1.48
            s.text(x + 0.35, y, tool, 9.3, color, weight="bold", family="DejaVu Sans Mono")
            s.text(x + 0.35, y + 0.44, purpose, 9.2, WHITE, linespacing=1.32)
            if index < len(tools) - 1:
                s.line(x + 0.35, y + 1.18, x + 4.0, y + 1.18, PANEL_2, 1)
    s.rect(1.25, 7.72, 13.5, 0.55, PANEL_2, radius=0.15)
    s.text(
        8,
        7.995,
        "These tools make the normal run loop observable, recoverable, and policy-bounded.",
        11.5,
        WHITE,
        weight="bold",
        ha="center",
        va="center",
    )
    return s


def slide_10() -> Slide:
    s = Slide(10, "The real goal: sustained scientific investigation", title_size=25)
    s.rect(0.72, 1.38, 14.56, 1.25, PANEL_2, edge=PINK, lw=1.35, radius=0.18)
    s.text(1.05, 1.7, "/GOAL", 10.5, PINK, weight="bold")
    s.text(
        2.22,
        1.59,
        "Explain why this bead–spring melt misses [criterion]. Inspect, run bounded tests,\n"
        "revise, and stop only at evidence or a genuine blocker.",
        12.3,
        WHITE,
        weight="bold",
    )

    points = [(2.2, 4.5), (5.2, 3.48), (8.4, 3.48), (11.8, 4.5), (9.4, 6.0), (5.25, 6.0)]
    for first, second in zip(points, points[1:] + points[:1]):
        s.line(first[0], first[1], second[0], second[1], PANEL_2, 3, zorder=0.5)

    nodes = [
        (2.2, 4.5, "1", "INSPECT", "code • inputs • prior runs", CYAN),
        (5.2, 3.48, "2", "HYPOTHESIZE", "physics • numerics", PINK),
        (8.4, 3.48, "3", "MODIFY", "simulation • analysis", YELLOW),
        (11.8, 4.5, "4", "SMOKE + RUN", "managed GPU experiment", TEAL),
        (9.4, 6.0, "5", "STATUS + ANALYZE", "outcome • artifacts", GREEN),
        (5.25, 6.0, "6", "DECIDE", "converged • revise • blocked", PINK),
    ]
    for x, y, number, title, body, color in nodes:
        s.rect(x - 1.3, y - 0.55, 2.6, 1.1, PANEL, edge=color, lw=1.25, radius=0.15)
        s.circle(x - 1.02, y, 0.2, color)
        s.text(x - 1.02, y, number, 10.5, INK, weight="bold", ha="center", va="center")
        s.text(x - 0.72, y - 0.3, title, 8.8, color, weight="bold")
        s.text(x - 0.72, y + 0.12, body, 7.8, MUTED)

    s.circle(7.15, 4.75, 0.77, TEAL, edge=BG, lw=2)
    s.text(7.15, 4.75, "KEEP\nITERATING", 10.2, INK, weight="bold", ha="center", va="center")

    s.rect(1.05, 7.3, 13.9, 0.63, PANEL_2, edge=TEAL, lw=1.1, radius=0.16)
    s.text(
        8,
        7.615,
        "/goal keeps the objective  •  heartbeat protects the run  •  Stop returns Codex to the next decision",
        11.2,
        WHITE,
        weight="bold",
        ha="center",
        va="center",
    )
    return s


def slide_11() -> Slide:
    s = Slide(11, "Bounded autonomy: the safety model")
    layers = [
        ("1", "REPO POLICY", "approved hosts and script / write roots", CYAN),
        ("2", "GUARDED PYTHON", "static scan + runtime audit hook; bounded writes", PINK),
        ("3", "GPU RESERVATION", "atomic claim prevents cooperative double-booking", TEAL),
        ("4", "MANAGED JOB", "heartbeat, outcome record, explicit job_id", GREEN),
        ("5", "FAIL CLOSED", "unknown process state stays reserved", YELLOW),
    ]
    for index, (number, title, body, color) in enumerate(layers):
        y = 1.55 + index * 1.18
        s.rect(0.75, y, 8.4, 0.92, PANEL, edge=color, lw=1.1, radius=0.14)
        s.circle(1.25, y + 0.46, 0.25, color)
        s.text(1.25, y + 0.46, number, 12, INK, weight="bold", ha="center", va="center")
        s.text(1.75, y + 0.22, title, 9.5, color, weight="bold")
        s.text(1.75, y + 0.61, body, 10.2, WHITE)

    s.rect(9.95, 1.55, 5.2, 2.63, "#123b35", edge=GREEN, lw=1.4, radius=0.2)
    s.text(10.45, 1.92, "WHAT IT IS", 11, GREEN, weight="bold")
    s.text(10.45, 2.55, "A cooperative guardrail\nfor trusted lab workflows\n\nA safe action surface\nfor sustained iteration", 13, WHITE, weight="bold")
    s.rect(9.95, 4.55, 5.2, 2.63, "#3a2630", edge=RED, lw=1.4, radius=0.2)
    s.text(10.45, 4.92, "WHAT IT IS NOT", 11, RED, weight="bold")
    s.text(10.45, 5.55, "Not Slurm or a queue\nNot a hostile-code sandbox\nNot a validator of force fields\nor equilibration", 12.5, WHITE, weight="bold")
    return s


def slide_12() -> Slide:
    s = Slide(12, "Three things to remember")
    takeaways = [
        (0.72, "1", "Hands, not just visibility", "Codex can act through typed tools;\nthe server keeps authority.", CYAN),
        (5.54, "2", "A loop, not a launch", "Goal + tools + heartbeat + hooks\nkeep investigation moving.", TEAL),
        (10.36, "3", "Evidence, not magic", "We define credible physics\nand the stopping condition.", PINK),
    ]
    for x, number, title, body, color in takeaways:
        s.rect(x, 1.72, 4.35, 3.2, PANEL, edge=color, lw=1.5, radius=0.22)
        s.circle(x + 0.67, 2.38, 0.33, color)
        s.text(x + 0.67, 2.38, number, 16, INK, weight="bold", ha="center", va="center")
        s.text(x + 0.45, 3.18, title, 14.2, WHITE, weight="bold")
        s.text(x + 0.45, 3.86, body, 11, MUTED)

    s.rect(1.9, 5.75, 12.2, 1.34, PANEL_2, edge=TEAL, lw=1.4, radius=0.23)
    s.text(8, 6.08, "GOOD FIRST GOAL", 9.5, TEAL, weight="bold", ha="center")
    s.text(
        8,
        6.48,
        "Give Codex one simulation question, a bounded experiment path,\nand a verifiable stopping condition.",
        13.2,
        WHITE,
        weight="bold",
        ha="center",
    )
    s.text(8, 7.7, "Questions?", 27, YELLOW, weight="bold", ha="center")
    return s


SLIDE_BUILDERS = [
    slide_1,
    slide_2,
    slide_3,
    slide_4,
    slide_5,
    slide_6,
    slide_7,
    slide_8,
    slide_9,
    slide_10,
    slide_11,
    slide_12,
]


def browser_deck_html(image_names: list[str]) -> str:
    sections = "\n".join(
        f'<section class="slide"><img src="slides/{name}" alt="Slide {index}"></section>'
        for index, name in enumerate(image_names, start=1)
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GPU MCP for Polymer Simulators</title>
<style>
html,body{{margin:0;width:100%;height:100%;background:#020b12;color:white;overflow:hidden;font-family:system-ui,sans-serif}}
.slide{{display:none;width:100%;height:100%;align-items:center;justify-content:center}}
.slide.active{{display:flex}}
.slide img{{width:min(100vw,177.78vh);height:min(56.25vw,100vh);object-fit:contain;box-shadow:0 0 40px #000}}
#counter{{position:fixed;right:16px;bottom:10px;color:#a9bdc8;font-size:14px}}
</style>
</head>
<body>
{sections}
<div id="counter"></div>
<script>
const slides=[...document.querySelectorAll('.slide')];let i=0;
function show(n){{i=Math.max(0,Math.min(slides.length-1,n));slides.forEach((s,j)=>s.classList.toggle('active',j===i));document.querySelector('#counter').textContent=`${{i+1}} / ${{slides.length}}`;}}
addEventListener('keydown',e=>{{if(['ArrowRight','PageDown',' '].includes(e.key))show(i+1);if(['ArrowLeft','PageUp'].includes(e.key))show(i-1);if(e.key==='Home')show(0);if(e.key==='End')show(slides.length-1);}});
addEventListener('click',()=>show(i+1));show(0);
</script>
</body>
</html>
"""


def build(output_dir: Path) -> tuple[Path, Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    slide_dir = output_dir / "slides"
    slide_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = output_dir / "GPU_MCP_for_Polymer_Simulators.pdf"
    html_path = output_dir / "GPU_MCP_for_Polymer_Simulators.html"
    image_names: list[str] = []

    with PdfPages(pdf_path) as pdf:
        for index, builder in enumerate(SLIDE_BUILDERS, start=1):
            slide = builder()
            image_name = f"slide-{index:02d}.png"
            image_names.append(image_name)
            slide.fig.savefig(
                slide_dir / image_name,
                dpi=160,
                facecolor=BG,
                edgecolor="none",
            )
            pdf.savefig(slide.fig, facecolor=BG, edgecolor="none")
            plt.close(slide.fig)

    html_path.write_text(browser_deck_html(image_names), encoding="utf-8")
    return pdf_path, html_path, slide_dir


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parent)
    args = parser.parse_args()
    for path in build(args.output_dir.resolve()):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
