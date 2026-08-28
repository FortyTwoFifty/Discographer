from __future__ import annotations

import queue
import re
import select
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from discographer.catalog import Catalog, Drive, Job, load, locked, mark_failed, mark_pending, mark_ripped, save_state
from discographer.cd import has_media
from discographer.rip import rip_disc

WORDS_PER_SECTOR = 1176
CD_1X_WORDS = 44100 * 2
CB_RE = re.compile(r"^##:\s*(-?\d+)\s+\[([^\]]+)\]\s+@\s*(-?\d+)")
TO_SECTOR_RE = re.compile(r"to sector\s+(\d+)")

HIDE = "\033[?25l"
SHOW = "\033[?25h"
UP = "\033[{}A"
CL = "\033[2K"


def parse_cb(line: str) -> tuple[int, str, int] | None:
    m = CB_RE.match(line.strip())
    if not m:
        return None
    return int(m.group(1)), m.group(2), int(m.group(3))


def parse_to_sector(line: str) -> int | None:
    m = TO_SECTOR_RE.search(line)
    return int(m.group(1)) if m else None


def rip_frac(pos: int, leadout: int, kind: str) -> float:
    if kind == "finished":
        return 1.0
    total = leadout * WORDS_PER_SECTOR
    if total <= 0 or pos < 0:
        return 0.0
    return min(0.999, pos / total)


def fmt_time(seconds: float | None) -> str:
    if seconds is None or seconds < 0 or seconds > 36 * 3600:
        return "--:--"
    s = int(seconds)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def fmt_speed(x: float | None) -> str:
    if x is None or x < 0:
        return "--x"
    if x >= 10:
        return f"{x:.0f}x"
    return f"{x:.1f}x"


def speed_from_delta(dpos: int, dt: float) -> float | None:
    if dt <= 0 or dpos <= 0:
        return None
    return dpos / dt / CD_1X_WORDS


def speed_from_window(
    samples: list[tuple[float, int]], min_dt: float = 4.0
) -> float | None:
    if len(samples) < 2:
        return None
    t0, p0 = samples[0]
    t1, p1 = samples[-1]
    dt = t1 - t0
    if dt < min_dt or p1 <= p0:
        return None
    x = speed_from_delta(p1 - p0, dt)
    if x is None or x > 48 or x < 0.15:
        return None
    return x


def eta_from_speed(pos: int, frac: float | None, speed_x: float | None) -> float | None:
    if pos <= 0 or not frac or not speed_x or frac >= 0.999 or speed_x <= 0:
        return None
    remain = pos / frac - pos
    if remain <= 0:
        return 0.0
    return remain / (speed_x * CD_1X_WORDS)



def bar(frac: float | None, width: int = 28) -> str:
    if frac is None:
        n = int(time.monotonic() * 4) % (width + 1)
        left = max(0, n - 4)
        return "░" * left + "█" * min(4, n) + "░" * max(0, width - n)
    frac = min(1.0, max(0.0, frac))
    filled = int(round(frac * width))
    return "█" * filled + "░" * (width - filled)


def _wait_line(timeout: float) -> str | None:
    if not sys.stdin.isatty():
        time.sleep(timeout)
        return None
    r, _, _ = select.select([sys.stdin], [], [], timeout)
    if not r:
        return None
    return sys.stdin.readline()


@dataclass
class Row:
    drive_id: str
    name: str
    disc: int = 0
    discs: int = 0
    album: str = ""
    phase: str = "idle"
    frac: float | None = None
    t0: float = 0.0
    eta: float | None = None
    err: str | None = None
    speed_x: float | None = None
    last_pos: int | None = None
    last_t: float = 0.0
    ready: bool = False
    samples: list[tuple[float, int]] = field(default_factory=list)


class Board:
    def __init__(self, rows: list[Row]):
        self.note = "Enter=start loaded idle drives   j=change idle job   q=quit"
        self.rows = {r.drive_id: r for r in rows}
        self.order = [r.drive_id for r in rows]
        self.lock = threading.Lock()
        self.drawn = 0
        self.last_draw = 0.0
        self.tty = sys.stdout.isatty()
        self.paused = False

    def _lines(self) -> list[str]:
        lines = ["discographer", self.note]
        for did in self.order:
            r = self.rows[did]
            lines.append(f"{r.drive_id:4}  {r.name}")
            album = r.album or "(no job)"
            lines.append(f"  {album}")
            if r.err:
                lines.append(f"  Disc {r.disc}/{r.discs}  FAIL  {r.err[:50]}")
            elif r.phase == "wait_load":
                flag = "ready" if r.ready else "empty"
                lines.append(
                    f"  put Disc {r.disc} of {r.discs}  then Enter  [{flag}]"
                )
            elif r.phase == "idle":
                lines.append("  idle")
            elif r.phase == "done":
                elapsed = fmt_time(time.monotonic() - r.t0) if r.t0 else ""
                lines.append(f"  Disc {r.disc}/{r.discs}  done  {elapsed}")
            elif r.phase == "ripping" and r.frac is not None:
                pct = f"{int(r.frac * 100):3d}%"
                eta = f"{fmt_time(r.eta)} left" if r.eta is not None else "ETA --:--"
                lines.append(
                    f"  Disc {r.disc}/{r.discs}  {bar(r.frac)} {pct}  {fmt_speed(r.speed_x)}  {eta}"
                )
            elif r.phase in ("encoding", "hashing", "copying", "waiting"):
                elapsed = fmt_time(time.monotonic() - r.t0) if r.t0 else ""
                lines.append(
                    f"  Disc {r.disc}/{r.discs}  {bar(r.frac)}  {r.phase}  {elapsed}"
                )
            else:
                lines.append(f"  Disc {r.disc}/{r.discs}  {r.phase}")
            lines.append("")
        return lines

    def _paint(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self.last_draw < 0.12:
            return
        self.last_draw = now
        if not self.tty or self.paused:
            return
        lines = self._lines()
        out = sys.stdout
        if self.drawn:
            out.write(UP.format(self.drawn))
        for line in lines:
            out.write(f"{CL}\r{line}\n")
        self.drawn = len(lines)
        out.flush()

    def start(self) -> None:
        with self.lock:
            self.paused = False
            self.drawn = 0
        if self.tty:
            sys.stdout.write(HIDE)
            sys.stdout.flush()
        self._paint(force=True)

    def close(self) -> None:
        with self.lock:
            self.paused = False
            self._paint(force=True)
            self.paused = True
            self.drawn = 0
        if self.tty:
            sys.stdout.write(SHOW)
            sys.stdout.flush()

    def update(
        self,
        drive_id: str,
        phase: str,
        frac: float | None = None,
        err: str | None = None,
        pos: int | None = None,
        kind: str | None = None,
    ) -> None:
        with self.lock:
            r = self.rows[drive_id]
            now = time.monotonic()
            if r.t0 == 0.0:
                r.t0 = now
            if phase != r.phase and phase in ("ripping", "encoding", "hashing", "copying"):
                if r.phase in ("waiting", "wait_load", "idle"):
                    r.t0 = now
            r.phase = phase
            r.err = err
            use = kind in (None, "read", "finished") or phase != "ripping"
            if phase == "ripping" and frac is not None and use:
                if r.frac is None or frac >= r.frac:
                    r.frac = frac
            elif phase != "ripping" and frac is not None:
                r.frac = frac
            if phase == "ripping" and pos is not None and kind == "read":
                if r.last_pos is None or pos >= r.last_pos:
                    r.samples.append((now, pos))
                    cutoff = now - 12.0
                    r.samples = [(t, p) for t, p in r.samples if t >= cutoff]
                    win = speed_from_window(r.samples)
                    if win is not None:
                        r.speed_x = win
                    r.last_pos = pos
                    r.last_t = now
                    eta = eta_from_speed(pos, r.frac, r.speed_x)
                    if eta is not None:
                        if r.eta is None or abs(eta - r.eta) >= 1.0:
                            r.eta = eta
            elif phase == "done":
                r.frac = 1.0
                r.eta = 0.0
            self._paint(force=phase in ("done", "wait_load", "idle") or err is not None)

    def set_lane(
        self,
        drive_id: str,
        *,
        album: str,
        disc: int,
        discs: int,
        phase: str,
        ready: bool = False,
        err: str | None = None,
    ) -> None:
        with self.lock:
            r = self.rows[drive_id]
            r.album = album
            r.disc = disc
            r.discs = discs
            r.phase = phase
            r.ready = ready
            r.err = err
            r.frac = None
            r.eta = None
            r.speed_x = None
            r.last_pos = None
            r.last_t = 0.0
            r.t0 = 0.0
            r.samples = []
            self._paint(force=True)

    def set_ready(self, drive_id: str, ready: bool) -> None:
        with self.lock:
            r = self.rows[drive_id]
            if r.ready == ready:
                return
            r.ready = ready
            self._paint(force=True)

    def set_note(self, note: str) -> None:
        with self.lock:
            if self.note == note:
                return
            self.note = note
            self._paint(force=True)


def pick_job(cat: Catalog, reason: str) -> Job | None:
    print()
    print(reason)
    options = []
    for jid, job in cat.jobs.items():
        left = cat.remaining(job)
        if left:
            options.append((jid, job, left))
    if not options:
        print("No jobs with remaining discs. Add one in catalog.yaml.")
        return None
    print()
    for i, (jid, job, left) in enumerate(options, 1):
        star = "*" if jid == cat.current else " "
        print(
            f"  {i}){star} {jid:12}  {job.album_name()}"
            f"  next Disc {left[0]}/{job.discs}  ({len(left)} left)"
        )
    print("  j)  type a job id")
    print("  q)  quit")
    while True:
        try:
            raw = input("job> ").strip()
        except EOFError:
            return None
        if raw in ("q", "quit"):
            return None
        if raw == "":
            continue
        if raw in ("j", "id"):
            jid = input("id> ").strip()
            if jid in cat.jobs:
                return cat.jobs[jid]
            print("unknown job")
            continue
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return options[int(raw) - 1][1]
        if raw in cat.jobs:
            return cat.jobs[raw]
        print("?")


def _set_current(cat: Catalog, job: Job) -> None:
    lock = locked(cat.state_path)
    try:
        cat = load(cat.path, cat.state_path)
        cat.current = job.id
        cat.state_for(job.id)
        save_state(cat)
    finally:
        lock.close()


def next_free_disc(cat: Catalog, job: Job, taken: set[tuple[str, int]]) -> int | None:
    for n in cat.remaining(job):
        if (job.id, n) not in taken:
            return n
    return None


def _print_jobs(cat: Catalog) -> None:
    print("jobs:")
    for jid, job in cat.jobs.items():
        left = cat.remaining(job)
        if left:
            extra = f"next Disc {left[0]}/{job.discs}  ({len(left)} left)"
        else:
            extra = "done"
        print(f"  {jid:12}  {job.album_name()}  {extra}")


def _resolve_job(cat: Catalog, raw: str, default_id: str | None) -> Job | None:
    raw = raw.strip()
    if raw in ("q", "quit"):
        return None
    if raw == "":
        raw = default_id or ""
    if raw.isdigit():
        options = [j for j in cat.jobs.values() if cat.remaining(j)]
        i = int(raw)
        if 1 <= i <= len(options):
            return options[i - 1]
    if raw in cat.jobs:
        return cat.jobs[raw]
    raise ValueError(f"unknown job {raw}")


def _ask_lane_job(cat: Catalog, drive: Drive, default_id: str | None) -> Job | None:
    hint = default_id or ""
    try:
        raw = input(f"{drive.id}  {drive.name}\n  job [{hint}]: ")
    except EOFError:
        return None
    try:
        return _resolve_job(cat, raw, default_id)
    except ValueError as e:
        print(e)
        return _ask_lane_job(cat, drive, default_id)


@dataclass
class Lane:
    drive: Drive
    job: Job | None = None
    disc: int | None = None
    busy: bool = False
    wait_load: bool = False


def _taken(lanes: list[Lane]) -> set[tuple[str, int]]:
    out: set[tuple[str, int]] = set()
    for ln in lanes:
        if ln.job and ln.disc is not None and (ln.busy or ln.wait_load):
            out.add((ln.job.id, ln.disc))
    return out


def _arm_lane(cat: Catalog, board: Board, lanes: list[Lane], ln: Lane) -> bool:
    cat = load(cat.path, cat.state_path)
    if ln.job is None:
        board.set_lane(ln.drive.id, album="", disc=0, discs=0, phase="idle")
        ln.wait_load = False
        ln.disc = None
        return False
    job = cat.jobs[ln.job.id]
    ln.job = job
    n = next_free_disc(cat, job, _taken(lanes))
    if n is None:
        ln.disc = None
        ln.wait_load = False
        board.set_lane(
            ln.drive.id,
            album=job.album_name(),
            disc=0,
            discs=job.discs,
            phase="idle",
        )
        return False
    ln.disc = n
    ln.wait_load = True
    board.set_lane(
        ln.drive.id,
        album=job.album_name(),
        disc=n,
        discs=job.discs,
        phase="wait_load",
        ready=has_media(ln.drive),
    )
    return True


def run_session(cat: Catalog, args) -> int:
    cat = load(cat.path, cat.state_path)
    default_id = getattr(args, "job", None) or cat.current
    _print_jobs(cat)
    print()
    lanes: list[Lane] = []
    for drive in cat.drives.values():
        job = _ask_lane_job(cat, drive, default_id)
        if job is None:
            return 0
        default_id = job.id
        _set_current(cat, job)
        lanes.append(Lane(drive=drive, job=job))
        cat = load(cat.path, cat.state_path)
    if args.dry_run:
        taken: set[tuple[str, int]] = set()
        for ln in lanes:
            assert ln.job is not None
            n = next_free_disc(cat, ln.job, taken)
            if n is None:
                print(f"dry-run {ln.drive.id} idle  {ln.job.album_name()}")
                continue
            taken.add((ln.job.id, n))
            print(
                f"dry-run {ln.drive.id} disc {n}/{ln.job.discs}  {ln.job.album_name()}"
            )
        return 0

    done_q: queue.Queue = queue.Queue()
    rows = [Row(drive_id=ln.drive.id, name=ln.drive.name) for ln in lanes]
    board = Board(rows)
    HELP = "Enter=start loaded idle drives   j=change idle job   q=quit"
    board.note = HELP
    board.start()
    last_poll = 0.0
    stop = False

    def work(drive: Drive, job: Job, disc: int):
        leadout_box = [1]

        def on_leadout(n: int):
            leadout_box[0] = max(leadout_box[0], n)

        def on_line(line: str):
            ts = parse_to_sector(line)
            if ts is not None:
                leadout_box[0] = max(leadout_box[0], ts + 1)
            parsed = parse_cb(line)
            if not parsed:
                return
            _func, kind, pos = parsed
            frac = rip_frac(pos, leadout_box[0], kind)
            board.update(
                drive.id,
                "done" if kind == "finished" else "ripping",
                frac,
                pos=pos,
                kind=kind,
            )

        def on_progress(phase: str, frac: float | None = None):
            if phase == "ripping":
                board.update(drive.id, "ripping", frac if frac else 0.0)
                return
            board.update(drive.id, phase, frac)

        err = None
        try:
            rip_disc(
                cat,
                job,
                disc,
                drive,
                keep=args.keep,
                no_eject=args.no_eject,
                dry_run=False,
                force=args.force,
                on_progress=on_progress,
                on_line=on_line,
                on_leadout=on_leadout,
            )
            board.update(drive.id, "done", 1.0)
        except Exception as e:
            err = str(e)
            board.update(drive.id, "fail", err=err.splitlines()[0])
        done_q.put((drive.id, job.id, disc, err))

    def start_ready(force_check: bool) -> None:
        empty = []
        for ln in lanes:
            if not ln.wait_load or ln.busy or ln.job is None or ln.disc is None:
                continue
            if not has_media(ln.drive):
                empty.append(f"{ln.drive.id} (Disc {ln.disc})")
                board.set_ready(ln.drive.id, False)
                continue
            lock = locked(cat.state_path)
            try:
                fresh = load(cat.path, cat.state_path)
                job = fresh.jobs[ln.job.id]
                mark_pending(fresh, job, [ln.disc])
            finally:
                lock.close()
            ln.busy = True
            ln.wait_load = False
            board.set_lane(
                ln.drive.id,
                album=ln.job.album_name(),
                disc=ln.disc,
                discs=ln.job.discs,
                phase="ripping",
            )
            ex.submit(work, ln.drive, ln.job, ln.disc)
        if force_check and empty:
            board.set_note("empty: " + ", ".join(empty) + "  — load, then Enter")
        elif force_check:
            board.set_note(HELP)

    def handle_done(drive_id: str, job_id: str, disc: int, err: str | None) -> None:
        lock = locked(cat.state_path)
        try:
            fresh = load(cat.path, cat.state_path)
            job = fresh.jobs[job_id]
            if err:
                mark_failed(fresh, job, disc)
            else:
                mark_ripped(fresh, job, disc)
        finally:
            lock.close()
        ln = next(x for x in lanes if x.drive.id == drive_id)
        ln.busy = False
        ln.wait_load = False
        ln.disc = None
        cat2 = load(cat.path, cat.state_path)
        if ln.job is not None:
            ln.job = cat2.jobs[ln.job.id]
        if not _arm_lane(cat2, board, lanes, ln):
            board.close()
            nxt = pick_job(
                cat2,
                f"{ln.drive.id}  {job_id} has no discs left. Pick a job for this drive.",
            )
            board.start()
            if nxt is None:
                board.set_lane(ln.drive.id, album="", disc=0, discs=0, phase="idle")
                return
            _set_current(cat2, nxt)
            ln.job = nxt
            _arm_lane(load(cat.path, cat.state_path), board, lanes, ln)
        board.set_note(HELP)

    def change_idle_job() -> None:
        idle = [ln for ln in lanes if ln.wait_load or (not ln.busy and ln.job is None)]
        waiting = [ln for ln in lanes if ln.wait_load]
        target = waiting[0] if len(waiting) == 1 else None
        if target is None and len(idle) == 1:
            target = idle[0]
        board.close()
        if target is None and (waiting or idle):
            print("which drive?")
            for ln in waiting or idle:
                print(f"  {ln.drive.id}")
            try:
                did = input("drive> ").strip()
            except EOFError:
                board.start()
                return
            target = next((x for x in lanes if x.drive.id == did), None)
        cat2 = load(cat.path, cat.state_path)
        if target is None:
            board.start()
            return
        nxt = pick_job(cat2, f"{target.drive.id}  currently {target.job.id if target.job else 'none'}")
        board.start()
        if nxt is None:
            return
        _set_current(cat2, nxt)
        target.job = nxt
        target.busy = False
        _arm_lane(load(cat.path, cat.state_path), board, lanes, target)

    try:
        with ThreadPoolExecutor(max_workers=max(1, len(lanes))) as ex:
            for ln in lanes:
                _arm_lane(cat, board, lanes, ln)
            if not sys.stdin.isatty():
                start_ready(True)
            while True:
                while True:
                    try:
                        item = done_q.get_nowait()
                    except queue.Empty:
                        break
                    handle_done(*item)
                now = time.monotonic()
                if now - last_poll >= 1.0:
                    last_poll = now
                    for ln in lanes:
                        if ln.wait_load:
                            board.set_ready(ln.drive.id, has_media(ln.drive))
                busy = any(ln.busy for ln in lanes)
                waiting = any(ln.wait_load for ln in lanes)
                if stop and not busy:
                    break
                if not busy and not waiting and not stop:
                    board.close()
                    nxt = pick_job(load(cat.path, cat.state_path), "All drives idle. Pick a job, or q.")
                    if nxt is None:
                        break
                    _set_current(cat, nxt)
                    for ln in lanes:
                        if not ln.busy:
                            ln.job = nxt
                            _arm_lane(load(cat.path, cat.state_path), board, lanes, ln)
                    board.start()
                    continue
                key = _wait_line(0.25)
                if key is None:
                    continue
                key = key.strip()
                if key.lower() in ("q", "quit"):
                    if busy:
                        board.set_note("waiting for in-flight rips to finish, then quit")
                        stop = True
                        for ln in lanes:
                            ln.wait_load = False
                        continue
                    break
                if stop:
                    continue
                if key.lower() in ("j", "job"):
                    change_idle_job()
                    continue
                if key in cat.jobs or (key.isdigit() and int(key) >= 1):
                    waiting_lanes = [ln for ln in lanes if ln.wait_load]
                    if len(waiting_lanes) == 1:
                        try:
                            job = _resolve_job(
                                load(cat.path, cat.state_path), key, waiting_lanes[0].job.id if waiting_lanes[0].job else None
                            )
                        except ValueError:
                            board.set_note(HELP)
                            continue
                        if job:
                            waiting_lanes[0].job = job
                            _set_current(cat, job)
                            _arm_lane(load(cat.path, cat.state_path), board, lanes, waiting_lanes[0])
                    continue
                start_ready(True)
    except BaseException:
        lock = locked(cat.state_path)
        try:
            fresh = load(cat.path, cat.state_path)
            for ln in lanes:
                if ln.busy and ln.job and ln.disc is not None:
                    if ln.disc in fresh.state_for(ln.job.id).pending:
                        mark_failed(fresh, fresh.jobs[ln.job.id], ln.disc)
        finally:
            lock.close()
        board.close()
        raise
    board.close()
    return 0
