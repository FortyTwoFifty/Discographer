from __future__ import annotations

import argparse
import sys
from pathlib import Path

from discographer.catalog import (
    Catalog,
    Drive,
    Job,
    load,
    locked,
    mark_failed,
    mark_pending,
    mark_ripped,
    mark_skipped,
    mark_unskipped,
    save_state,
    scan_job,
)
from discographer.cd import cddb_id, has_media, msf_stamp, query_toc, read_cdtext, rewrite_cue_header
from discographer.rip import apply_tags, copy_verified, eject, remove_work_dir, rip_disc
from discographer.tui import run_session


def _resolve_drive(cat: Catalog, spec: str | None) -> Drive:
    if spec is None:
        for d in cat.drives.values():
            if has_media(d):
                return d
        if cat.drives:
            return next(iter(cat.drives.values()))
        raise SystemExit("no drives in catalog")
    if spec in cat.drives:
        return cat.drives[spec]
    for d in cat.drives.values():
        if d.path == spec or d.path.endswith(spec) or Path(d.path).name == spec:
            return d
    raise SystemExit(f"unknown drive {spec}")


def cmd_status(cat: Catalog, args) -> int:
    print(f"library  {cat.library} {'OK' if cat.library.is_dir() else 'NOT MOUNTED'}")
    print(f"staging  {cat.staging}")
    print(f"current  {cat.current or '(none)'}")
    print()
    for d in cat.drives.values():
        media = "disc" if has_media(d) else "empty"
        print(f"drive    {d.id:4} {d.path}  offset {d.offset:+d}  {d.name}  [{media}]")
    if not cat.current:
        return 0
    job = cat.job()
    st = cat.state_for(job.id)
    remain = cat.remaining(job)
    print()
    print(f"job      {job.id}")
    print(f"album    {job.album_name()}")
    print(f"author   {job.author}")
    print(f"path     {job.dest_dir(cat.library)}")
    print(f"discs    {job.discs}  ripped {st.ripped or '[]'}  next {st.next_disc}")
    if st.pending:
        print(f"pending  {st.pending}")
    if st.skipped:
        print(f"skipped  {st.skipped}")
    if remain:
        print(f"todo     {remain}")
    elif st.skipped:
        print(f"todo     done ({len(st.skipped)} skipped)")
    else:
        print("todo     done")
    return 0


def cmd_jobs(cat: Catalog, args) -> int:
    for jid, job in cat.jobs.items():
        st = cat.state_for(jid)
        mark = "*" if jid == cat.current else " "
        remain = len(cat.remaining(job))
        skip = f"  skipped={st.skipped}" if st.skipped else ""
        print(
            f"{mark} {jid:16}  {job.album_name()}  "
            f"{len(st.ripped)}/{job.discs}  next={st.next_disc}  left={remain}{skip}"
        )
    return 0


def cmd_set(cat: Catalog, args) -> int:
    lock = locked(cat.state_path)
    try:
        cat = load(cat.path, cat.state_path)
        job = cat.job(args.job)
        cat.current = job.id
        cat.state_for(job.id)
        save_state(cat)
    finally:
        lock.close()
    print(f"current job {job.id}: {job.album_name()}")
    return 0


def cmd_scan(cat: Catalog, args) -> int:
    lock = locked(cat.state_path)
    try:
        cat = load(cat.path, cat.state_path)
        jobs = [cat.job(args.job)] if args.job else list(cat.jobs.values())
        for job in jobs:
            st = scan_job(cat, job)
            extra = f"  skipped {st.skipped}" if st.skipped else ""
            print(
                f"{job.id}: ripped {st.ripped or '[]'}  next {st.next_disc}  "
                f"{job.album_name()}{extra}"
            )
        save_state(cat)
    finally:
        lock.close()
    return 0


def cmd_devices(cat: Catalog, args) -> int:
    for d in cat.drives.values():
        print(f"{d.id}\t{d.path}\t{d.offset}\t{d.name}")
        if not has_media(d):
            print("  empty")
            continue
        toc = query_toc(d)
        print(f"  tracks {toc.track_count}  leadout {toc.leadout}  discid {cddb_id(toc)}")
        titles = read_cdtext(d)
        for tr in toc.tracks:
            title = titles.get(tr.number, "")
            print(f"  {tr.number:02d}  {msf_stamp(tr.start_lba)}  {title}")
    return 0


def cmd_toc(cat: Catalog, args) -> int:
    drive = _resolve_drive(cat, args.drive)
    toc = query_toc(drive)
    titles = read_cdtext(drive)
    print(f"{drive.name} ({drive.path})")
    print(f"discid {cddb_id(toc)}  tracks {toc.track_count}")
    for tr in toc.tracks:
        title = titles.get(tr.number, "")
        print(
            f"{tr.number:02d}  start {msf_stamp(tr.start_lba)}  "
            f"len {msf_stamp(tr.length)}  {title}"
        )
    return 0


def cmd_eject(cat: Catalog, args) -> int:
    if args.drive in (None, "all"):
        drives = list(cat.drives.values())
    else:
        drives = [_resolve_drive(cat, args.drive)]
    for d in drives:
        eject(d)
        print(f"ejected {d.id} ({d.path})")
    return 0


def _rip_one(cat: Catalog, job: Job, disc: int, drive: Drive, args) -> tuple[int, str | None]:
    try:
        rip_disc(
            cat,
            job,
            disc,
            drive,
            keep=args.keep,
            no_eject=args.no_eject,
            dry_run=args.dry_run,
            force=args.force,
        )
        return disc, None
    except Exception as e:
        return disc, str(e)


def cmd_rip(cat: Catalog, args) -> int:
    lock = locked(cat.state_path)
    try:
        cat = load(cat.path, cat.state_path)
        job = cat.job(args.job)
        drive = _resolve_drive(cat, args.drive)
        remain = cat.remaining(job)
        st = cat.state_for(job.id)
        if args.disc is not None:
            disc = args.disc
        elif remain:
            disc = remain[0]
        else:
            if st.skipped:
                print(
                    f"{job.id} complete ({len(st.ripped)}/{job.discs} ripped, "
                    f"skipped {st.skipped})"
                )
            else:
                print(f"{job.id} complete ({job.discs} discs)")
            return 0
        if not args.dry_run:
            mark_pending(cat, job, [disc])
    finally:
        lock.close()
    try:
        disc, err = _rip_one(cat, job, disc, drive, args)
    except BaseException:
        if not args.dry_run:
            lock = locked(cat.state_path)
            try:
                cat = load(cat.path, cat.state_path)
                mark_failed(cat, cat.job(args.job), disc)
            finally:
                lock.close()
        raise
    lock = locked(cat.state_path)
    try:
        cat = load(cat.path, cat.state_path)
        job = cat.job(args.job)
        if err:
            if not args.dry_run:
                mark_failed(cat, job, disc)
            print(f"FAIL disc {disc}: {err}", file=sys.stderr)
            return 1
        if not args.dry_run:
            mark_ripped(cat, job, disc)
        return 0
    finally:
        lock.close()


def cmd_salvage(cat: Catalog, args) -> int:
    job = cat.job(args.job)
    root = cat.staging / job.id
    if not root.is_dir():
        print(f"no staging at {root}")
        return 1
    rc = 0
    found = False
    for d in sorted(root.iterdir()):
        if not d.is_dir() or not d.name.startswith("disc-"):
            continue
        try:
            disc = int(d.name.split("-")[1])
        except (IndexError, ValueError):
            continue
        flac = d / job.flac_name(disc)
        if not flac.is_file():
            continue
        found = True
        dest = job.disc_dir(cat.library, disc)
        print(f"salvage disc {disc}  {flac.name}")
        try:
            copy_verified(flac, dest / job.flac_name(disc))
            cue = d / job.cue_name(disc)
            log = d / job.log_name()
            if cue.is_file():
                copy_verified(cue, dest / job.cue_name(disc))
            if log.is_file():
                copy_verified(log, dest / job.log_name())
        except Exception as e:
            print(f"FAIL disc {disc}: {e}", file=sys.stderr)
            rc = 1
            continue
        lock = locked(cat.state_path)
        try:
            cat = load(cat.path, cat.state_path)
            mark_ripped(cat, cat.job(job.id), disc)
        finally:
            lock.close()
        remove_work_dir(d)
        print(f"ok disc {disc} -> {dest}")
    if not found:
        print("nothing to salvage")
        return 1
    return rc


def cmd_skip(cat: Catalog, args) -> int:
    lock = locked(cat.state_path)
    try:
        cat = load(cat.path, cat.state_path)
        job = cat.job(args.job)
        mark_skipped(cat, job, args.disc)
    finally:
        lock.close()
    st = cat.state_for(job.id)
    remain = cat.remaining(job)
    print(f"skipped disc {args.disc} of {job.id} ({job.album_name()})")
    print(f"  ripped {st.ripped or '[]'}  skipped {st.skipped}  left {remain or 'done'}")
    return 0


def cmd_unskip(cat: Catalog, args) -> int:
    lock = locked(cat.state_path)
    try:
        cat = load(cat.path, cat.state_path)
        job = cat.job(args.job)
        if args.disc not in cat.state_for(job.id).skipped:
            print(f"disc {args.disc} of {job.id} is not skipped")
            return 0
        mark_unskipped(cat, job, args.disc)
    finally:
        lock.close()
    print(f"unskipped disc {args.disc} of {job.id} — it is remaining again")
    return 0


def cmd_retag(cat: Catalog, args) -> int:
    job = cat.job(args.job)
    discs = [args.disc] if args.disc else scan_job(cat, job).ripped
    if not discs:
        print("no ripped discs found")
        return 1
    rc = 0
    for disc in discs:
        ddir = job.disc_dir(cat.library, disc)
        flac = ddir / job.flac_name(disc)
        cue = ddir / job.cue_name(disc)
        if args.rename_from:
            old_album = args.rename_from
            if "/" in old_album or "\0" in old_album or old_album in (".", ".."):
                raise SystemExit("invalid --rename-from")
            old_root = Path(cat.library) / job.series
            if job.author_dir:
                old_root = old_root / job.author
            old_dir = old_root / old_album / f"Disc {disc}"
            old_flac = old_dir / f"{old_album} (Disc {disc}).flac"
            old_cue = old_dir / f"{old_album} (Disc {disc}).cue"
            old_log = old_dir / f"{old_album}.log"
            ddir.mkdir(parents=True, exist_ok=True)
            for src, dest in (
                (old_flac, flac),
                (old_cue, cue),
                (old_log, ddir / job.log_name()),
            ):
                if src.is_file() and src.resolve() != dest.resolve():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    src.replace(dest)
        if not flac.is_file():
            print(f"missing {flac}", file=sys.stderr)
            rc = 1
            continue
        apply_tags(flac, job, disc)
        if cue.is_file():
            rewrite_cue_header(cue, job, disc, job.flac_name(disc))
        print(f"retagged disc {disc}  {flac}")
    save_state(cat)
    return rc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="discographer",
        description="Accurate multi-drive CD ripper (FLAC image + CUE + log)",
    )
    p.add_argument("--catalog", type=Path, default=None)
    p.add_argument("--state", type=Path, default=None)
    p.set_defaults(
        func=run_session,
        keep=False,
        no_eject=False,
        force=False,
        dry_run=False,
        disc=None,
        job=None,
        drive=None,
    )
    sub = p.add_subparsers(dest="cmd", required=False)

    sp = sub.add_parser("status", help="show current job and drives")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("jobs", help="list catalog jobs")
    sp.set_defaults(func=cmd_jobs)

    sp = sub.add_parser("set", help="set current job")
    sp.add_argument("job")
    sp.set_defaults(func=cmd_set)

    sp = sub.add_parser("scan", help="sync ripped discs from the library folder")
    sp.add_argument("job", nargs="?")
    sp.set_defaults(func=cmd_scan)

    sp = sub.add_parser("devices", help="list drives and any loaded disc TOC")
    sp.set_defaults(func=cmd_devices)

    sp = sub.add_parser("toc", help="print TOC and CD-Text for a drive")
    sp.add_argument("-d", "--drive", default=None)
    sp.set_defaults(func=cmd_toc)

    sp = sub.add_parser("eject", help="eject one or all drives")
    sp.add_argument("drive", nargs="?", default="all")
    sp.set_defaults(func=cmd_eject)

    rip_opts = argparse.ArgumentParser(add_help=False)
    rip_opts.add_argument("-d", "--drive", default=None)
    rip_opts.add_argument("--disc", type=int, default=None)
    rip_opts.add_argument("--job", default=None)
    rip_opts.add_argument("--keep", action="store_true")
    rip_opts.add_argument("--no-eject", action="store_true")
    rip_opts.add_argument("--force", action="store_true")
    rip_opts.add_argument("--dry-run", action="store_true")

    sp = sub.add_parser("rip", parents=[rip_opts], help="rip next disc (or --disc) on one drive")
    sp.set_defaults(func=cmd_rip)

    sp = sub.add_parser(
        "session",
        parents=[rip_opts],
        help="interactive session: one lane per drive, independent rips",
    )
    sp.set_defaults(func=run_session)
    sub.add_parser("both", parents=[rip_opts], help="alias for session").set_defaults(func=run_session)

    sp = sub.add_parser(
        "skip",
        help="mark a disc unreadable and continue the job without it",
    )
    sp.add_argument("disc", type=int, help="disc number")
    sp.add_argument("job", nargs="?", help="job id (default: current)")
    sp.set_defaults(func=cmd_skip)

    sp = sub.add_parser("unskip", help="return a skipped disc to the remaining list")
    sp.add_argument("disc", type=int, help="disc number")
    sp.add_argument("job", nargs="?", help="job id (default: current)")
    sp.set_defaults(func=cmd_unskip)

    sp = sub.add_parser("salvage", help="copy finished staging rips to the library (after a copy failure)")
    sp.add_argument("job", nargs="?")
    sp.set_defaults(func=cmd_salvage)

    sp = sub.add_parser("retag", help="rewrite tags/CUE from catalog without re-ripping")
    sp.add_argument("job", nargs="?")
    sp.add_argument("--disc", type=int, default=None)
    sp.add_argument("--rename-from", default=None, help="old album folder name to move from")
    sp.set_defaults(func=cmd_retag)
    return p


def main(argv: list[str] | None = None) -> int:
    p = build_parser()
    args = p.parse_args(argv)
    cat = load(args.catalog, args.state)
    try:
        return args.func(cat, args)
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except (RuntimeError, ValueError, FileNotFoundError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
