from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

from discographer.catalog import Drive, Job

TRACK_RE = re.compile(
    r"^\s*(\d+)\.\s+(\d+)\s+\[(\d+):(\d+)\.(\d+)\]\s+(\d+)\s+\[(\d+):(\d+)\.(\d+)\]"
)
CDTEXT_TRACK_RE = re.compile(r"^CD-TEXT for Track\s+(\d+)\s*:", re.I)
CDTEXT_TITLE_RE = re.compile(r"^\s+TITLE:\s*(.*\S)\s*$", re.I)


@dataclass
class Track:
    number: int
    start_lba: int
    length: int


@dataclass
class Toc:
    tracks: list[Track]
    leadout: int
    device: str = ""

    @property
    def track_count(self) -> int:
        return len(self.tracks)


def lba_to_msf(lba: int) -> tuple[int, int, int]:
    frames = lba
    mm = frames // (75 * 60)
    ss = (frames // 75) % 60
    ff = frames % 75
    return mm, ss, ff


def msf_stamp(lba: int) -> str:
    mm, ss, ff = lba_to_msf(lba)
    return f"{mm:02d}:{ss:02d}:{ff:02d}"


def cddb_sum(n: int) -> int:
    total = 0
    while n > 0:
        total += n % 10
        n //= 10
    return total


def cddb_id(toc: Toc) -> str:
    n = 0
    for tr in toc.tracks:
        n += cddb_sum((tr.start_lba + 150) // 75)
    total = (toc.leadout + 150) // 75 - (toc.tracks[0].start_lba + 150) // 75
    value = ((n % 0xFF) << 24) | (total << 8) | toc.track_count
    return f"{value:08X}"


def parse_paranoia_toc(text: str) -> Toc:
    tracks: list[Track] = []
    for line in text.splitlines():
        m = TRACK_RE.match(line)
        if m:
            num = int(m.group(1))
            length = int(m.group(2))
            start = int(m.group(6))
            tracks.append(Track(number=num, start_lba=start, length=length))
    if not tracks:
        raise ValueError("no audio tracks in TOC")
    last = tracks[-1]
    return Toc(tracks=tracks, leadout=last.start_lba + last.length)


def query_toc(drive: Drive) -> Toc:
    r = subprocess.run(
        ["cdparanoia", "-d", drive.path, "-Q"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    text = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        raise RuntimeError(f"{drive.path}: no audio CD ({_one_line(text)})")
    toc = parse_paranoia_toc(text)
    toc.device = drive.path
    return toc


def _one_line(text: str) -> str:
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line
    return "unknown error"


def read_cdtext(drive: Drive) -> dict[int, str]:
    r = subprocess.run(
        [
            "cd-info",
            "-C",
            drive.path,
            "--no-device-info",
            "--no-analyze",
            "--no-header",
            "--no-disc-mode",
            "-q",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    titles: dict[int, str] = {}
    current = None
    for line in (r.stdout or "").splitlines():
        m = CDTEXT_TRACK_RE.match(line.strip())
        if m:
            current = int(m.group(1))
            continue
        if current is None:
            continue
        tm = CDTEXT_TITLE_RE.match(line)
        if tm:
            titles[current] = tm.group(1).strip()
            current = None
    return titles


def _cue_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("\r", "")


def write_cue(
    path,
    job: Job,
    disc: int,
    toc: Toc,
    titles: dict[int, str],
    file_name: str,
) -> str:
    album = _cue_escape(job.album_name())
    lines = [f'TITLE "{album}"']
    if job.author:
        lines.append(f'PERFORMER "{_cue_escape(job.author)}"')
    if job.genre:
        lines.append(f'REM GENRE "{_cue_escape(job.genre)}"')
    if job.year is not None:
        lines.append(f'REM DATE "{job.year}"')
    lines.append(f"REM DISCNUMBER {disc}")
    lines.append(f"REM TOTALDISCS {job.discs}")
    lines.append(f"REM DISCID {cddb_id(toc)}")
    lines.append(f'FILE "{_cue_escape(file_name)}" WAVE')
    for tr in toc.tracks:
        title = titles.get(tr.number) or f"Track {tr.number:02d}"
        lines.append(f"  TRACK {tr.number:02d} AUDIO")
        lines.append(f'    TITLE "{_cue_escape(title)}"')
        if job.composer:
            lines.append(f'    SONGWRITER "{_cue_escape(job.composer)}"')
        rel = tr.start_lba - toc.tracks[0].start_lba
        lines.append(f"    INDEX 01 {msf_stamp(rel)}")
    text = "\n".join(lines) + "\n"
    path.write_text(text)
    return text


def rewrite_cue_header(path, job: Job, disc: int, file_name: str) -> None:
    raw = path.read_text()
    idx = raw.find("  TRACK ")
    if idx < 0:
        raise ValueError(f"no tracks in {path}")
    rest = raw[idx:]
    discid = None
    for line in raw.splitlines():
        if line.startswith("REM DISCID "):
            discid = line.split(None, 2)[2].strip()
            break
    album = _cue_escape(job.album_name())
    lines = [f'TITLE "{album}"']
    if job.author:
        lines.append(f'PERFORMER "{_cue_escape(job.author)}"')
    if job.genre:
        lines.append(f'REM GENRE "{_cue_escape(job.genre)}"')
    if job.year is not None:
        lines.append(f'REM DATE "{job.year}"')
    lines.append(f"REM DISCNUMBER {disc}")
    lines.append(f"REM TOTALDISCS {job.discs}")
    if discid:
        lines.append(f"REM DISCID {discid}")
    lines.append(f'FILE "{_cue_escape(file_name)}" WAVE')
    path.write_text("\n".join(lines) + "\n" + rest.lstrip("\n"))


def has_media(drive: Drive) -> bool:
    try:
        query_toc(drive)
        return True
    except (RuntimeError, ValueError, subprocess.TimeoutExpired):
        return False
