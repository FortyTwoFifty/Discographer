from __future__ import annotations

import hashlib
import os
import select
import shutil
import signal
import subprocess
import threading
import time
import wave
import zlib
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from discographer import __version__
from discographer.catalog import Catalog, Drive, Job
from discographer.cd import Toc, cddb_id, msf_stamp, query_toc, read_cdtext, write_cue

BYTES_PER_SECTOR = 2352


class RipCancelled(RuntimeError):
    pass


def signal_rip(proc: subprocess.Popen, sig: int = signal.SIGTERM) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.send_signal(sig)
        except OSError:
            pass


def terminate_rip(proc: subprocess.Popen) -> None:
    signal_rip(proc, signal.SIGTERM)
    try:
        proc.wait(timeout=3)
        return
    except subprocess.TimeoutExpired:
        pass
    signal_rip(proc, signal.SIGKILL)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


def throw_if_cancelled(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise RipCancelled("cancelled")


def work_dir(cat: Catalog, job: Job, disc: int, drive: Drive) -> Path:
    d = cat.staging / job.id / f"disc-{disc}-{drive.id}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def remove_work_dir(wd: Path) -> None:
    shutil.rmtree(wd)
    try:
        wd.parent.rmdir()
    except OSError:
        pass


def rip_wav(
    drive: Drive,
    wav: Path,
    summary: Path,
    on_line: Callable[[str], None] | None = None,
    cancel: threading.Event | None = None,
    on_proc: Callable[[subprocess.Popen], None] | None = None,
) -> None:
    wav.unlink(missing_ok=True)
    progress = summary.with_name("progress.log")
    cmd = [
        "stdbuf",
        "-eL",
        "cdparanoia",
        "-d",
        drive.path,
        "-O",
        str(drive.offset),
        "-w",
        "-e",
        "-z20",
        f"--log-summary={summary}",
        "1-",
        str(wav),
    ]
    proc = subprocess.Popen(
        cmd,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        start_new_session=True,
    )
    if on_proc:
        on_proc(proc)
    assert proc.stderr is not None
    tail: list[str] = []
    fd = proc.stderr
    try:
        throw_if_cancelled(cancel)
        with progress.open("w") as log:
            while True:
                throw_if_cancelled(cancel)
                ready, _, _ = select.select([fd], [], [], 0.25)
                if not ready:
                    if proc.poll() is not None:
                        leftover = fd.read()
                        if leftover:
                            log.write(leftover)
                            for line in leftover.splitlines(keepends=True):
                                tail.append(line)
                                if on_line:
                                    on_line(line)
                        break
                    continue
                line = fd.readline()
                if line == "":
                    break
                log.write(line)
                tail.append(line)
                if len(tail) > 40:
                    tail = tail[-20:]
                if on_line:
                    on_line(line)
        try:
            rc = proc.wait(timeout=3600)
        except subprocess.TimeoutExpired:
            terminate_rip(proc)
            raise RuntimeError(f"cdparanoia timed out on {drive.path}")
    except RipCancelled:
        terminate_rip(proc)
        raise
    throw_if_cancelled(cancel)
    if rc != 0 or not wav.is_file() or wav.stat().st_size < 1024:
        raise RuntimeError(
            f"cdparanoia failed on {drive.path} (exit {rc})\n{''.join(tail[-20:])}"
        )


def track_crc32(wav: Path, toc: Toc) -> tuple[str, list[str]]:
    with wave.open(str(wav), "rb") as w:
        if w.getnchannels() != 2 or w.getsampwidth() != 2 or w.getframerate() != 44100:
            raise RuntimeError("wav is not 16-bit stereo 44100")
        pcm = w.readframes(w.getnframes())
    image = f"{zlib.crc32(pcm) & 0xFFFFFFFF:08X}"
    tracks = []
    for tr in toc.tracks:
        rel = tr.start_lba - toc.tracks[0].start_lba
        start = rel * BYTES_PER_SECTOR
        end = min((rel + tr.length) * BYTES_PER_SECTOR, len(pcm))
        chunk = pcm[start:end]
        tracks.append(f"{zlib.crc32(chunk) & 0xFFFFFFFF:08X}")
    return image, tracks


def encode_flac(
    wav: Path,
    flac: Path,
    job: Job,
    disc: int,
    on_progress: Callable[[str, float | None], None] | None = None,
    cancel: threading.Event | None = None,
    on_proc: Callable[[subprocess.Popen], None] | None = None,
) -> None:
    flac.unlink(missing_ok=True)
    tags = [
        f"ALBUM={job.album_name()}",
        f"DISCNUMBER={disc}",
        f"DISCTOTAL={job.discs}",
        f"TOTALDISCS={job.discs}",
        f"ENCODER=discographer {__version__}",
    ]
    if job.author:
        tags.append(f"ALBUMARTIST={job.author}")
    if job.composer:
        tags.append(f"COMPOSER={job.composer}")
    if job.genre:
        tags.append(f"GENRE={job.genre}")
    if job.year is not None:
        tags.append(f"DATE={job.year}")
    cmd = ["flac", "-8", "--no-error-on-compression-fail", "-o", str(flac)]
    for t in tags:
        cmd.extend(["-T", t])
    cmd.append(str(wav))
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    if on_proc:
        on_proc(proc)
    err_box = [""]

    def drain():
        if proc.stderr is not None:
            err_box[0] = proc.stderr.read()

    th = threading.Thread(target=drain, daemon=True)
    th.start()
    deadline = time.monotonic() + 1800
    try:
        while True:
            throw_if_cancelled(cancel)
            try:
                rc = proc.wait(timeout=0.4)
                break
            except subprocess.TimeoutExpired:
                if time.monotonic() > deadline:
                    terminate_rip(proc)
                    raise RuntimeError("flac encode timed out")
                if on_progress:
                    on_progress("encoding", None)
        th.join(timeout=2)
        err = err_box[0]
    except RipCancelled:
        terminate_rip(proc)
        raise
    throw_if_cancelled(cancel)
    if rc != 0 or not flac.is_file():
        raise RuntimeError((err or "").strip() or "flac encode failed")
    throw_if_cancelled(cancel)
    t = subprocess.run(["flac", "-t", str(flac)], capture_output=True, text=True, timeout=300)
    if t.returncode != 0:
        raise RuntimeError(t.stderr.strip() or "flac test failed")


def write_log(
    path: Path,
    job: Job,
    disc: int,
    drive: Drive,
    toc: Toc,
    wav: Path,
    dest: Path,
    summary: Path,
    image_crc: str,
    track_crcs: list[str],
) -> None:
    now = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    lines = [
        f"discographer {__version__}",
        f"extraction logfile from {now}",
        "",
        f"{job.author} / {job.album_name()}",
        "",
        f"Used drive : {drive.name} ({drive.path})",
        "Ripper mode             : cdparanoia 10.2 secure",
        f"Read offset correction  : {drive.offset}",
        "Max retry count         : 20",
        "Gap status              : Appended (track 1 pregap dropped)",
        "",
        "TOC of the extracted CD",
        "     Track |   Start  |  Length  | Start sector | End sector ",
        "    ---------------------------------------------------------",
    ]
    for tr in toc.tracks:
        end = tr.start_lba + tr.length - 1
        length = msf_stamp(tr.length)
        start = msf_stamp(tr.start_lba)
        lines.append(
            f"       {tr.number:2d}  | {start} | {length} | {tr.start_lba:9d}    | {end:7d}   "
        )
    lines.extend(
        [
            "",
            f"CDDB disc id           : {cddb_id(toc)}",
            f"Filename               : {dest}",
            f"CRC32 hash             : {image_crc}",
            f"WAV size               : {wav.stat().st_size} bytes",
            "",
        ]
    )
    for tr, crc in zip(toc.tracks, track_crcs):
        lines.append(f"Track {tr.number:02d}  CRC32 {crc}")
    lines.append("")
    summary_text = summary.read_text(errors="replace") if summary.is_file() else ""
    if summary_text:
        lines.append("cdparanoia summary")
        lines.append(summary_text)
    low = summary_text.lower()
    if "error" in low or "skip" in low:
        lines.append("Check cdparanoia summary for reconstruction/skips")
    else:
        lines.append("No errors occurred")
    lines.append("")
    path.write_text("\n".join(lines) + "\n")


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def copy_verified(
    src: Path,
    dest: Path,
    on_progress: Callable[[str, float | None], None] | None = None,
    cancel: threading.Event | None = None,
) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    total = src.stat().st_size
    copied = 0
    with src.open("rb") as inf, dest.open("wb") as out:
        while True:
            throw_if_cancelled(cancel)
            chunk = inf.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)
            copied += len(chunk)
            if on_progress and total:
                on_progress("copying", copied / total)
    if dest.stat().st_size != total or sha256(src) != sha256(dest):
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"copy verify failed {src} -> {dest}")


def eject(drive: Drive) -> None:
    subprocess.run(["eject", drive.path], capture_output=True, timeout=30)


def apply_tags(flac: Path, job: Job, disc: int) -> None:
    fields = [
        "ALBUM",
        "ALBUMARTIST",
        "ARTIST",
        "COMPOSER",
        "GENRE",
        "DATE",
        "DISCNUMBER",
        "DISCTOTAL",
        "TOTALDISCS",
    ]
    cmd = ["metaflac"]
    for f in fields:
        cmd.append(f"--remove-tag={f}")
    cmd.append(str(flac))
    subprocess.run(cmd, check=True, timeout=60)
    tags = [
        f"ALBUM={job.album_name()}",
        f"DISCNUMBER={disc}",
        f"DISCTOTAL={job.discs}",
        f"TOTALDISCS={job.discs}",
    ]
    if job.author:
        tags.append(f"ALBUMARTIST={job.author}")
    if job.composer:
        tags.append(f"COMPOSER={job.composer}")
    if job.genre:
        tags.append(f"GENRE={job.genre}")
    if job.year is not None:
        tags.append(f"DATE={job.year}")
    cmd = ["metaflac"]
    for t in tags:
        cmd.append(f"--set-tag={t}")
    cmd.append(str(flac))
    subprocess.run(cmd, check=True, timeout=60)


def rip_disc(
    cat: Catalog,
    job: Job,
    disc: int,
    drive: Drive,
    *,
    keep: bool = False,
    no_eject: bool = False,
    dry_run: bool = False,
    force: bool = False,
    on_progress: Callable[[str, float | None], None] | None = None,
    on_line: Callable[[str], None] | None = None,
    on_leadout: Callable[[int], None] | None = None,
    cancel: threading.Event | None = None,
    on_proc: Callable[[subprocess.Popen], None] | None = None,
) -> Path:
    if disc < 1 or disc > job.discs:
        raise RuntimeError(f"disc {disc} out of range 1..{job.discs}")
    dest_dir = job.disc_dir(cat.library, disc)
    dest_flac = dest_dir / job.flac_name(disc)
    dest_cue = dest_dir / job.cue_name(disc)
    dest_log = dest_dir / job.log_name()
    if dest_flac.exists() and not dry_run and not force:
        raise RuntimeError(f"already exists: {dest_flac} (use --force)")
    if dry_run:
        print(f"dry-run {drive.id} disc {disc}/{job.discs}")
        print(f"  album  {job.album_name()}")
        print(f"  dest   {dest_flac}")
        return dest_flac
    if not cat.library.is_dir():
        raise RuntimeError(f"library not mounted: {cat.library}")
    toc = query_toc(drive)
    if on_leadout:
        on_leadout(toc.leadout)
    titles = {}
    try:
        titles = read_cdtext(drive)
    except (subprocess.TimeoutExpired, OSError):
        titles = {}
    throw_if_cancelled(cancel)
    wd = work_dir(cat, job, disc, drive)
    wav = wd / "image.wav"
    summary = wd / "paranoia.log"
    cue_tmp = wd / job.cue_name(disc)
    flac_tmp = wd / job.flac_name(disc)
    log_tmp = wd / job.log_name()
    if on_progress:
        on_progress("ripping", 0.0)
    else:
        print(f"[{drive.id}] ripping disc {disc}/{job.discs}  {job.album_name()}", flush=True)
    try:
        rip_wav(drive, wav, summary, on_line=on_line, cancel=cancel, on_proc=on_proc)
        throw_if_cancelled(cancel)
        if on_progress:
            on_progress("hashing", None)
        else:
            print(f"[{drive.id}] encoding flac", flush=True)
        write_cue(cue_tmp, job, disc, toc, titles, job.flac_name(disc))
        image_crc, track_crcs = track_crc32(wav, toc)
        throw_if_cancelled(cancel)
        encode_flac(
            wav, flac_tmp, job, disc, on_progress=on_progress, cancel=cancel, on_proc=on_proc
        )
        if on_progress:
            on_progress("copying", 0.0)
        else:
            print(f"[{drive.id}] copying to {dest_dir}", flush=True)
        write_log(
            log_tmp,
            job,
            disc,
            drive,
            toc,
            wav,
            dest_flac,
            summary,
            image_crc,
            track_crcs,
        )
        try:
            copy_verified(flac_tmp, dest_flac, on_progress=on_progress, cancel=cancel)
            copy_verified(cue_tmp, dest_cue, cancel=cancel)
            copy_verified(log_tmp, dest_log, cancel=cancel)
        except Exception:
            dest_flac.unlink(missing_ok=True)
            dest_cue.unlink(missing_ok=True)
            dest_log.unlink(missing_ok=True)
            raise
    except RipCancelled:
        if not keep:
            remove_work_dir(wd)
        if not no_eject:
            eject(drive)
        raise
    if not keep:
        remove_work_dir(wd)
    if not no_eject:
        eject(drive)
    if on_progress:
        on_progress("done", 1.0)
    else:
        print(f"[{drive.id}] ok disc {disc}/{job.discs} -> {dest_flac}", flush=True)
    return dest_flac
