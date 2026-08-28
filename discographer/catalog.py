from __future__ import annotations

import fcntl
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "discographer"


def default_paths() -> tuple[Path, Path]:
    env = os.environ.get("DISCOGRAPHER_CATALOG")
    if env:
        c = Path(env).expanduser()
        s = Path(os.environ["DISCOGRAPHER_STATE"]).expanduser() if os.environ.get("DISCOGRAPHER_STATE") else c.parent / "state.yaml"
        return c, s
    xdg_c = config_dir() / "catalog.yaml"
    if xdg_c.is_file():
        return xdg_c, config_dir() / "state.yaml"
    local = ROOT / "catalog.yaml"
    if local.is_file():
        return local, ROOT / "state.yaml"
    return xdg_c, config_dir() / "state.yaml"


DEFAULT_CATALOG, DEFAULT_STATE = default_paths()


def _sysfs(sr: str, rel: str) -> str:
    try:
        return (Path("/sys/block") / sr / rel).read_text().strip()
    except OSError:
        return ""


def detect_drives() -> dict[str, Drive]:
    found: dict[str, Drive] = {}
    for p in sorted(Path("/dev").glob("sr[0-9]*")):
        if not p.name[2:].isdigit():
            continue
        vendor = _sysfs(p.name, "device/vendor")
        model = _sysfs(p.name, "device/model")
        name = " ".join(x for x in (vendor, model) if x) or p.name
        found[p.name] = Drive(id=p.name, path=str(p), name=name, offset=6)
    return found


@dataclass
class Drive:
    id: str
    path: str
    name: str
    offset: int = 6


@dataclass
class Job:
    id: str
    title: str = ""
    author: str = ""
    series: str = ""
    book: int | None = None
    discs: int = 1
    author_dir: bool = False
    year: int | None = None
    part: int | None = None
    parts: int | None = None
    album: str | None = None
    genre: str = ""
    composer: str = ""

    def album_name(self) -> str:
        if self.album:
            name = self.album
        elif self.series and self.book is not None and self.title:
            name = f"{self.series} #{self.book} - {self.title}"
            if self.part is not None and self.parts is not None:
                name = f"{name} ({self.part} of {self.parts})"
        elif self.title:
            name = self.title
        else:
            name = self.id
        return name.replace("/", "-").replace("\0", "")

    def dest_dir(self, library: Path) -> Path:
        root = Path(library)
        p = root
        if self.series:
            p = p / self.series.replace("/", "-").replace("\0", "")
        if self.author_dir and self.author:
            p = p / self.author.replace("/", "-").replace("\0", "")
        p = p / self.album_name()
        try:
            p.resolve().relative_to(root.resolve())
        except ValueError as e:
            raise ValueError("job path escapes library") from e
        return p

    def disc_dir(self, library: Path, disc: int) -> Path:
        if self.discs == 1:
            return self.dest_dir(library)
        return self.dest_dir(library) / f"Disc {disc}"

    def stem(self, disc: int) -> str:
        if self.discs == 1:
            return self.album_name()
        return f"{self.album_name()} (Disc {disc})"

    def flac_name(self, disc: int) -> str:
        return f"{self.stem(disc)}.flac"

    def cue_name(self, disc: int) -> str:
        return f"{self.stem(disc)}.cue"

    def log_name(self) -> str:
        return f"{self.album_name()}.log"


@dataclass
class JobState:
    next_disc: int = 1
    ripped: list[int] = field(default_factory=list)
    pending: list[int] = field(default_factory=list)


@dataclass
class Catalog:
    library: Path
    staging: Path
    defaults: dict
    drives: dict[str, Drive]
    jobs: dict[str, Job]
    path: Path
    state_path: Path
    current: str | None = None
    job_state: dict[str, JobState] = field(default_factory=dict)

    def job(self, job_id: str | None = None) -> Job:
        jid = job_id or self.current
        if not jid:
            raise SystemExit("no current job; discographer set <job>")
        if jid not in self.jobs:
            raise SystemExit(f"unknown job {jid}")
        return self.jobs[jid]

    def state_for(self, job_id: str) -> JobState:
        st = self.job_state.get(job_id)
        if st is None:
            st = JobState()
            self.job_state[job_id] = st
        return st

    def remaining(self, job: Job) -> list[int]:
        st = self.state_for(job.id)
        taken = set(st.ripped) | set(st.pending)
        return [n for n in range(1, job.discs + 1) if n not in taken]


def _load_yaml(path: Path) -> dict:
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text())
    return data if isinstance(data, dict) else {}


def load(catalog_path: Path | None = None, state_path: Path | None = None) -> Catalog:
    if catalog_path:
        cpath = Path(catalog_path)
        spath = Path(state_path) if state_path else cpath.parent / "state.yaml"
    else:
        cpath, spath = default_paths()
        if state_path:
            spath = Path(state_path)
    raw = _load_yaml(cpath)
    if not raw:
        raise SystemExit(
            f"missing catalog {cpath}\nCopy catalog.example.yaml to that path or run ./install.sh"
        )
    defaults = raw.get("defaults") or {}
    genre = str(defaults.get("genre") or "")
    composer = str(defaults.get("composer") or "")
    configured: dict[str, Drive] = {}
    for did, d in (raw.get("drives") or {}).items():
        path = str(d["path"])
        if not path.startswith("/dev/sr"):
            raise SystemExit(f"refusing drive path {path}")
        configured[did] = Drive(
            id=did,
            path=path,
            name=str(d.get("name") or did),
            offset=int(d.get("offset", 6)),
        )
    drives = detect_drives()
    drives.update(configured)
    if not drives:
        raise SystemExit("no optical drives found (no /dev/sr*)")
    jobs = {}
    for jid, j in (raw.get("jobs") or {}).items():
        title = str(j.get("title") or "")
        album = str(j["album"]) if j.get("album") else None
        if not title and not album:
            raise SystemExit(f"job {jid} needs title or album")
        jobs[jid] = Job(
            id=jid,
            series=str(j.get("series") or ""),
            author=str(j.get("author") or j.get("artist") or ""),
            author_dir=bool(j.get("author_dir") or j.get("artist_dir") or False),
            book=int(j["book"]) if j.get("book") is not None else None,
            title=title,
            discs=int(j.get("discs") or 1),
            year=int(j["year"]) if j.get("year") is not None else None,
            part=int(j["part"]) if j.get("part") is not None else None,
            parts=int(j["parts"]) if j.get("parts") is not None else None,
            album=album,
            genre=str(j.get("genre") or genre),
            composer=str(j.get("composer") or composer),
        )
    st = _load_yaml(spath)
    job_state = {}
    for jid, s in (st.get("jobs") or {}).items():
        ripped = [int(n) for n in (s.get("ripped") or [])]
        pending = [int(n) for n in (s.get("pending") or [])]
        nxt = int(s.get("next_disc") or 1)
        job_state[jid] = JobState(
            next_disc=nxt,
            ripped=sorted(set(ripped)),
            pending=sorted(set(pending)),
        )
    library = Path(os.path.expanduser(str(raw.get("library") or "~/Music")))
    staging = Path(os.path.expanduser(str(raw.get("staging") or "~/.cache/discographer")))
    return Catalog(
        library=library,
        staging=staging,
        defaults=defaults,
        drives=drives,
        jobs=jobs,
        path=cpath,
        state_path=spath,
        current=st.get("current") or raw.get("current"),
        job_state=job_state,
    )


class _Dumper(yaml.SafeDumper):
    pass


def _list_flow(dumper, data):
    return dumper.represent_sequence("tag:yaml.org,2002:seq", data, flow_style=True)


_Dumper.add_representer(list, _list_flow)


def save_state(cat: Catalog) -> None:
    cat.state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "current": cat.current,
        "jobs": {
            jid: {
                "next_disc": st.next_disc,
                "ripped": sorted(set(st.ripped)),
                "pending": sorted(set(st.pending)),
            }
            for jid, st in cat.job_state.items()
        },
    }
    text = yaml.dump(payload, Dumper=_Dumper, sort_keys=False, allow_unicode=True)
    tmp = cat.state_path.with_suffix(".yaml.tmp")
    tmp.write_text(text)
    tmp.replace(cat.state_path)


def locked(state_path: Path):
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(str(state_path) + ".lock")
    fh = open(lock_path, "a+b")
    fcntl.flock(fh, fcntl.LOCK_EX)
    return fh


def mark_pending(cat: Catalog, job: Job, discs: list[int]) -> None:
    st = cat.state_for(job.id)
    st.pending = sorted(set(st.pending) | set(discs))
    save_state(cat)


def mark_ripped(cat: Catalog, job: Job, disc: int) -> None:
    st = cat.state_for(job.id)
    st.pending = [n for n in st.pending if n != disc]
    if disc not in st.ripped:
        st.ripped.append(disc)
        st.ripped = sorted(set(st.ripped))
    remain = [n for n in range(1, job.discs + 1) if n not in set(st.ripped) | set(st.pending)]
    st.next_disc = remain[0] if remain else job.discs + 1
    save_state(cat)


def mark_failed(cat: Catalog, job: Job, disc: int) -> None:
    st = cat.state_for(job.id)
    st.pending = [n for n in st.pending if n != disc]
    remain = [n for n in range(1, job.discs + 1) if n not in set(st.ripped) | set(st.pending)]
    st.next_disc = remain[0] if remain else job.discs + 1
    save_state(cat)


def scan_job(cat: Catalog, job: Job) -> JobState:
    st = cat.state_for(job.id)
    found = []
    root = job.dest_dir(cat.library)
    if job.discs == 1:
        if root.is_dir() and list(root.glob("*.flac")):
            found = [1]
    elif root.is_dir():
        for p in root.iterdir():
            if not p.is_dir() or not p.name.startswith("Disc "):
                continue
            try:
                n = int(p.name.split()[1])
            except (IndexError, ValueError):
                continue
            if list(p.glob("*.flac")):
                found.append(n)
    st.ripped = sorted(set(found))
    st.pending = []
    remain = [n for n in range(1, job.discs + 1) if n not in st.ripped]
    st.next_disc = remain[0] if remain else job.discs + 1
    return st
