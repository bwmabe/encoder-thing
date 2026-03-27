#!/usr/bin/env python3
"""
encode.py — x265 batch encoder with TUI

Usage: encode.py [-o dir] [--crf n] [--preset p] [--ivtc] [-y]
                 [--deint filter] [--crop [value]] <input|dir> [...]

Keys during encode:
  p / space  pause / resume
  s          skip current file
  q / ^C     quit
"""

import argparse
import os
import queue
import re
import select
import signal
import subprocess
import sys
import termios
import threading
import time
import tty
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import List, Optional

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

VIDEO_EXTS = {".mkv", ".mp4", ".avi"}
QUEUE_WINDOW = 10  # rows visible in the queue panel (keep height stable)


# ── data model ────────────────────────────────────────────────────────────────

class Status(Enum):
    QUEUED    = "queued"
    DETECTING = "detecting"
    ENCODING  = "encoding"
    PAUSED    = "paused"
    DONE      = "done"
    FAILED    = "failed"
    SKIPPED   = "skipped"


@dataclass
class Job:
    input:   Path
    output:  Path
    status:  Status = Status.QUEUED
    elapsed: float  = 0.0
    height:  int    = 0
    width:   int    = 0
    crf:     int    = 0
    preset:  str    = ""
    error:   str    = ""


@dataclass
class Progress:
    out_time_ms:  int = 0
    duration_ms:  int = 0
    fps:          str = "?"
    speed:        str = "?"


# ── formatting helpers ─────────────────────────────────────────────────────────

def fmt_hms(secs: float) -> str:
    secs = max(0, int(secs))
    h, r = divmod(secs, 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def fmt_elapsed(secs: float) -> str:
    secs = max(0, int(secs))
    m, s = divmod(secs, 60)
    if secs < 3600:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


_ICON = {
    Status.QUEUED:    ("  ",  "dim"),
    Status.DETECTING: ("⟳ ",  "yellow"),
    Status.ENCODING:  ("▶ ",  "cyan"),
    Status.PAUSED:    ("⏸ ",  "yellow"),
    Status.DONE:      ("✓ ",  "green"),
    Status.FAILED:    ("✗ ",  "red"),
    Status.SKIPPED:   ("– ",  "dim"),
}

_LABEL = {
    Status.QUEUED:    ("queued",     "dim"),
    Status.DETECTING: ("detecting",  "yellow"),
    Status.ENCODING:  ("encoding",   "cyan"),
    Status.PAUSED:    ("paused",     "yellow"),
    Status.DONE:      ("done",       "green"),
    Status.FAILED:    ("failed",     "red"),
    Status.SKIPPED:   ("skipped",    "dim"),
}


# ── display ───────────────────────────────────────────────────────────────────

def make_display(
    jobs: List[Job],
    idx: int,
    prog: Progress,
    paused: bool,
    all_done: bool = False,
) -> Group:
    # ── queue panel ──
    tbl = Table(box=None, padding=(0, 1), show_header=False, expand=True)
    tbl.add_column("icon",   width=2,  no_wrap=True)
    tbl.add_column("name",   ratio=1,  no_wrap=True, overflow="ellipsis")
    tbl.add_column("status", width=10, justify="right")
    tbl.add_column("time",   width=8,  justify="right")

    n = len(jobs)
    half = QUEUE_WINDOW // 2
    win_start = max(0, min(idx - half, n - QUEUE_WINDOW))
    win_end   = min(n, win_start + QUEUE_WINDOW)

    # filler row so the panel height never changes
    def filler():
        tbl.add_row(Text(""), Text(""), Text(""), Text(""))

    if win_start > 0:
        tbl.add_row(Text(f"  ↑ {win_start} above", style="dim"), Text(""), Text(""), Text(""))
    else:
        filler()

    for i in range(win_start, win_end):
        job = jobs[i]
        icon_s, icon_c  = _ICON[job.status]
        label_s, label_c = _LABEL[job.status]
        bold = i == idx and job.status in (Status.ENCODING, Status.PAUSED, Status.DETECTING)
        tbl.add_row(
            Text(icon_s, style=icon_c),
            Text(job.input.name, style="bold" if bold else ""),
            Text(label_s, style=label_c),
            Text(fmt_elapsed(job.elapsed) if job.elapsed > 0 else "", style="dim"),
        )

    remaining = n - win_end
    if remaining > 0:
        tbl.add_row(Text(f"  ↓ {remaining} more", style="dim"), Text(""), Text(""), Text(""))
    else:
        filler()

    queue_panel = Panel(
        tbl,
        title=f"[bold]Queue[/bold] [dim]({n} file{'s' if n != 1 else ''})[/dim]",
        border_style="bright_black",
    )

    # ── progress panel ──
    job = jobs[idx] if idx < n else None

    if all_done:
        done_n    = sum(1 for j in jobs if j.status == Status.DONE)
        failed_n  = sum(1 for j in jobs if j.status == Status.FAILED)
        skipped_n = sum(1 for j in jobs if j.status == Status.SKIPPED)
        summary = Text()
        summary.append(f"{done_n} done", style="green bold")
        summary.append("  ·  ", style="dim")
        summary.append(f"{skipped_n} skipped", style="dim")
        if failed_n:
            summary.append("  ·  ", style="dim")
            summary.append(f"{failed_n} failed", style="red bold")
        prog_panel = Panel(
            Group(summary, Text(""), Text("")),  # 3 lines to match height
            title="[green bold]✓  All done[/green bold]",
            border_style="green",
        )

    elif job and job.status in (Status.ENCODING, Status.PAUSED):
        pct = min(1.0, prog.out_time_ms / prog.duration_ms) if prog.duration_ms > 0 else 0.0
        bar_w   = 36
        filled  = int(pct * bar_w)
        bar_col = "yellow" if paused else "cyan"
        bar = Text()
        bar.append("█" * filled,          style=bar_col)
        bar.append("░" * (bar_w - filled), style="bright_black")

        current_s = prog.out_time_ms / 1000
        total_s   = prog.duration_ms  / 1000
        pct_str   = f"{int(pct * 100):3d}%"

        try:
            speed_f = float(re.sub(r"x$", "", prog.speed or ""))
            eta = fmt_hms((prog.duration_ms - prog.out_time_ms) / 1000 / speed_f) if speed_f > 0 else "--:--:--"
        except (ValueError, TypeError):
            eta = "--:--:--"

        info = Table(box=None, padding=0, show_header=False, expand=True)
        info.add_column("name", ratio=1, no_wrap=True, overflow="ellipsis")
        info.add_column("meta", no_wrap=True)
        info.add_row(
            Text(job.input.name, style="bold"),
            Text(f"  {job.height}p · CRF {job.crf} · {job.preset}", style="dim"),
        )

        bar_line = Text()
        bar_line.append_text(bar)
        bar_line.append(f"  {pct_str}", style="bold")

        stats = Text()
        stats.append(f"{fmt_hms(current_s)} / {fmt_hms(total_s)}", style="bright_white")
        stats.append("  ·  ", style="dim")
        stats.append(f"{prog.fps} fps", style="dim")
        stats.append("  ·  ", style="dim")
        stats.append(prog.speed or "?x", style="dim")
        stats.append("  ·  ETA ", style="dim")
        stats.append(eta, style="yellow bold" if paused else "bright_white bold")

        title = "[yellow bold]⏸  Paused[/yellow bold]" if paused else "[cyan bold]▶  Now Encoding[/cyan bold]"
        prog_panel = Panel(Group(info, bar_line, stats), title=title, border_style="bright_black")

    elif job and job.status == Status.DETECTING:
        prog_panel = Panel(
            Group(
                Text(f"Running cropdetect on {job.input.name} …", style="yellow"),
                Text(""),
                Text(""),
            ),
            title="[yellow bold]⟳  Detecting crop[/yellow bold]",
            border_style="bright_black",
        )

    elif job and job.status == Status.FAILED:
        prog_panel = Panel(
            Group(
                Text(f"Failed: {job.input.name}", style="red bold"),
                Text(job.error[:120] if job.error else "", style="dim"),
                Text(""),
            ),
            title="[red bold]✗  Failed[/red bold]",
            border_style="bright_black",
        )

    else:
        prog_panel = Panel(
            Group(Text(""), Text(""), Text("")),
            title="[dim]Idle[/dim]",
            border_style="bright_black",
        )

    # ── keybinds footer ──
    keys = Text("  ")
    for k, label in (("[p]", " pause/resume  "), ("[s]", " skip  "), ("[q]", " quit")):
        keys.append(k, style="bold bright_white")
        keys.append(label, style="dim")

    return Group(queue_panel, prog_panel, keys)


# ── ffmpeg helpers ─────────────────────────────────────────────────────────────

def probe_file(path: Path):
    """Return (height, width, duration_s). Falls back to (0, 0, 0.0) on error."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height:format=duration",
             "-of", "default=noprint_wrappers=1:nokey=0", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        h = w = 0
        dur = 0.0
        for line in r.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                if k == "height":
                    try: h = int(v)
                    except ValueError: pass
                elif k == "width":
                    try: w = int(v)
                    except ValueError: pass
                elif k == "duration":
                    try: dur = float(v)
                    except ValueError: pass
        return h, w, dur
    except Exception:
        return 0, 0, 0.0


def detect_crop(path: Path) -> Optional[str]:
    """Return 'crop=W:H:X:Y' or None."""
    def _run(ss: Optional[str], duration: str) -> Optional[str]:
        cmd = ["ffmpeg"]
        if ss:
            cmd += ["-ss", ss]
        cmd += ["-i", str(path), "-t", duration,
                "-vf", "cropdetect=limit=32:round=2:reset=0",
                "-f", "null", "-"]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        lines = [l for l in r.stderr.splitlines() if "cropdetect" in l]
        if lines:
            m = re.search(r"crop=\d+:\d+:\d+:\d+", lines[-1])
            if m:
                return m.group(0)
        return None

    return _run("00:05:00", "00:01:00") or _run(None, "00:02:00")


def build_vf(args, crop: Optional[str]) -> Optional[str]:
    parts = []
    if args.ivtc:
        parts.append("fieldmatch,yadif=deint=interlaced,decimate" if args.y else "fieldmatch,decimate")
    elif args.deint:
        parts.append(f"{args.deint}=mode=1" if args.deint in ("yadif", "bwdif") else args.deint)
    if crop:
        parts.append(crop)
    return ",".join(parts) if parts else None


def build_cmd(job: Job, vf: Optional[str]) -> List[str]:
    cmd = [
        "ffmpeg", "-fflags", "+igndts",
        "-i", str(job.input),
        "-map", "0",
        "-loglevel", "error",
        "-progress", "pipe:1",
        "-nostats",
        "-c:v", "libx265",
        "-crf", str(job.crf),
        "-preset", job.preset,
    ]
    if job.height >= 2000:
        cmd += ["-x265-params", "hdr10=1:hdr10-opt=1:repeat-headers=1"]
    else:
        cmd += ["-x265-params", "psy-rd=1.0:psy-rdoq=0.5"]
    if vf:
        cmd += ["-vf", vf]
    cmd += ["-max_interleave_delta", "0", "-c:a", "copy", "-c:s", "copy", str(job.output), "-y"]
    return cmd


def read_progress(proc: subprocess.Popen, prog: Progress) -> None:
    for line in proc.stdout:
        k, _, v = line.strip().partition("=")
        if k == "out_time_ms":
            try:
                val = int(v)
                if val >= 0:
                    prog.out_time_ms = val // 1000  # ffmpeg reports µs despite the name
            except ValueError:
                pass
        elif k == "fps" and v and v != "0.00":
            prog.fps = v
        elif k == "speed" and v.strip():
            try:
                prog.speed = f"{float(v.strip().rstrip('x')):.1f}x"
            except ValueError:
                prog.speed = v.strip()


def drain_stderr(proc: subprocess.Popen, buf: list) -> None:
    for line in proc.stderr:
        buf.append(line)


# ── keyboard ──────────────────────────────────────────────────────────────────

def keyboard_reader(cmd_q: queue.Queue, stop_ev: threading.Event) -> None:
    if not sys.stdin.isatty():
        return
    fd  = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        while not stop_ev.is_set():
            if select.select([sys.stdin], [], [], 0.05)[0]:
                ch = sys.stdin.read(1)
                cmd_q.put(ch)
    except Exception:
        pass
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except Exception:
            pass


# ── arg parsing ───────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Encode video files with x265",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("inputs", nargs="+", metavar="INPUT")
    p.add_argument("-o", dest="output_dir", default=None, metavar="DIR",
                   help="Output directory for file inputs (default: ./converted)")
    p.add_argument("--crf", type=int, default=None,
                   help="x265 CRF (default: auto — 18 SD/HD, 20 4K)")
    p.add_argument("--preset", default="medium",
                   help="x265 preset (default: medium)")
    p.add_argument("--ivtc", action="store_true",
                   help="Inverse telecine (fieldmatch,decimate)")
    p.add_argument("-y", action="store_true",
                   help="IVTC + yadif for residual interlace")
    p.add_argument("--deint", metavar="FILTER",
                   help="Deinterlace filter (yadif, bwdif, estdif, w3fdif)")
    p.add_argument("--crop", nargs="?", const=True, default=None, metavar="VALUE",
                   help="Auto-detect crop bars, or supply manual crop=W:H:X:Y")
    args = p.parse_args()

    if args.y:
        args.ivtc = True

    if isinstance(args.crop, str) and not args.crop.startswith("crop="):
        p.error(f"--crop value must be 'crop=W:H:X:Y', got: {args.crop!r}")

    return args


def build_jobs(args) -> List[Job]:
    jobs = []
    for inp in args.inputs:
        path = Path(inp)
        if path.is_file():
            out_dir = Path(args.output_dir) if args.output_dir else Path("converted")
            out_dir.mkdir(parents=True, exist_ok=True)
            jobs.append(Job(input=path, output=out_dir / path.name))
        elif path.is_dir():
            out_dir = path / "converted"
            out_dir.mkdir(parents=True, exist_ok=True)
            for f in sorted(path.iterdir()):
                if f.suffix.lower() in VIDEO_EXTS:
                    jobs.append(Job(input=f, output=out_dir / f.name))
        else:
            print(f"warning: not found, skipping: {inp}", file=sys.stderr)
    return jobs


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    args  = parse_args()
    jobs  = build_jobs(args)

    if not jobs:
        print("No input files found.", file=sys.stderr)
        sys.exit(1)

    cmd_q   = queue.Queue()
    stop_ev = threading.Event()

    kb_thread = threading.Thread(target=keyboard_reader, args=(cmd_q, stop_ev), daemon=True)
    kb_thread.start()

    console = Console()
    paused  = False
    prog    = Progress()

    with Live(console=console, refresh_per_second=10, screen=False) as live:
        for idx, job in enumerate(jobs):
            if stop_ev.is_set():
                break

            prog = Progress()
            live.update(make_display(jobs, idx, prog, paused))

            # ── probe ──
            h, w, dur_s = probe_file(job.input)
            job.height = h
            job.width  = w
            job.crf    = args.crf if args.crf is not None else (20 if h >= 2000 else 18)
            job.preset = args.preset

            # ── crop detection ──
            crop = None
            skip_this = False

            if args.crop is not None:
                if isinstance(args.crop, str):
                    crop = args.crop  # manual value
                else:
                    job.status = Status.DETECTING
                    live.update(make_display(jobs, idx, prog, paused))
                    crop = detect_crop(job.input)
                    # if crop is a no-op, discard it
                    if crop:
                        m = re.match(r"crop=(\d+):(\d+):", crop)
                        if m and int(m.group(1)) == w and int(m.group(2)) == h:
                            crop = None
                    # drain keys accumulated during detection
                    while not cmd_q.empty():
                        try:
                            ch = cmd_q.get_nowait()
                            if ch == "s":
                                skip_this = True
                            elif ch in ("q", "\x03"):
                                stop_ev.set()
                        except queue.Empty:
                            break

            if skip_this or stop_ev.is_set():
                job.status = Status.SKIPPED
                live.update(make_display(jobs, idx, prog, paused))
                continue

            # ── start ffmpeg ──
            vf  = build_vf(args, crop)
            cmd = build_cmd(job, vf)

            prog       = Progress()
            prog.duration_ms = int(dur_s * 1000) if dur_s else 0

            stderr_buf: list = []
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

            prog_thread = threading.Thread(target=read_progress, args=(proc, prog), daemon=True)
            err_thread  = threading.Thread(target=drain_stderr,  args=(proc, stderr_buf), daemon=True)
            prog_thread.start()
            err_thread.start()

            job.status     = Status.ENCODING
            start_time     = time.monotonic()
            paused_secs    = 0.0
            paused_at: Optional[float] = None
            skip_requested = False

            # ── control loop ──
            while proc.poll() is None:
                while not cmd_q.empty():
                    try:
                        ch = cmd_q.get_nowait()
                    except queue.Empty:
                        break

                    if ch in ("p", " "):
                        if not paused:
                            os.kill(proc.pid, signal.SIGSTOP)
                            paused    = True
                            paused_at = time.monotonic()
                            job.status = Status.PAUSED
                        else:
                            os.kill(proc.pid, signal.SIGCONT)
                            if paused_at is not None:
                                paused_secs += time.monotonic() - paused_at
                                paused_at = None
                            paused    = False
                            job.status = Status.ENCODING

                    elif ch == "s":
                        skip_requested = True
                        if paused:
                            os.kill(proc.pid, signal.SIGCONT)
                            paused = False
                        proc.kill()

                    elif ch in ("q", "\x03"):
                        stop_ev.set()
                        if paused:
                            os.kill(proc.pid, signal.SIGCONT)
                            paused = False
                        proc.kill()

                now = time.monotonic()
                cur_pause = (now - paused_at) if paused_at else 0.0
                job.elapsed = now - start_time - paused_secs - cur_pause
                live.update(make_display(jobs, idx, prog, paused))
                time.sleep(0.05)

            prog_thread.join(timeout=2)
            err_thread.join(timeout=2)

            now = time.monotonic()
            cur_pause   = (now - paused_at) if paused_at else 0.0
            job.elapsed = now - start_time - paused_secs - cur_pause
            paused = False
            paused_at = None

            rc = proc.returncode
            if stop_ev.is_set() or skip_requested:
                job.status = Status.SKIPPED
                if job.output.exists():
                    job.output.unlink()
            elif rc == 0:
                job.status = Status.DONE
            else:
                job.status = Status.FAILED
                job.error  = "".join(stderr_buf).strip()

            live.update(make_display(jobs, idx, prog, paused))

            if stop_ev.is_set():
                # mark remaining as skipped for the summary
                for j in jobs[idx + 1:]:
                    j.status = Status.SKIPPED
                break

        # ── final display ──
        live.update(make_display(jobs, max(0, len(jobs) - 1), prog, False, all_done=True))

    stop_ev.set()

    # ── summary ──
    done_n    = sum(1 for j in jobs if j.status == Status.DONE)
    skipped_n = sum(1 for j in jobs if j.status == Status.SKIPPED)
    failed    = [j for j in jobs if j.status == Status.FAILED]

    print(f"\n{done_n} done, {skipped_n} skipped, {len(failed)} failed")
    for j in failed:
        print(f"  FAILED: {j.input.name}")
        if j.error:
            for line in j.error.splitlines()[:5]:
                print(f"    {line}")


if __name__ == "__main__":
    main()
