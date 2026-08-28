import tempfile
import unittest
from pathlib import Path

from discographer.catalog import Job, load, scan_job
from discographer.cd import (
    cddb_id,
    msf_stamp,
    parse_paranoia_toc,
    rewrite_cue_header,
    write_cue,
)

Q = """
cdparanoia III release 10.2 (September 11, 2008)
Table of contents (audio tracks only):
track        length               begin        copy pre ch
===========================================================
  1.    22294 [04:57.19]        0 [00:00.00]    no   no  2
  2.    22510 [05:00.10]    22294 [04:57.19]    no   no  2
  3.    22448 [04:59.23]    44804 [09:57.29]    no   no  2
  4.    23038 [05:07.13]    67252 [14:56.52]    no   no  2
  5.    22257 [04:56.57]    90290 [20:03.65]    no   no  2
  6.    22252 [04:56.52]   112547 [25:00.47]    no   no  2
  7.    22713 [05:02.63]   134799 [29:57.24]    no   no  2
  8.    22337 [04:57.62]   157512 [35:00.12]    no   no  2
  9.    22936 [05:05.61]   179849 [39:57.74]    no   no  2
 10.    22387 [04:58.37]   202785 [45:03.60]    no   no  2
 11.     2495 [00:33.20]   225172 [50:02.22]    no   no  2
TOTAL  227667 [50:35.42]    (audio only)
"""

INDEXES = [
    "00:00:00",
    "04:57:19",
    "09:57:29",
    "14:56:52",
    "20:03:65",
    "25:00:47",
    "29:57:24",
    "35:00:12",
    "39:57:74",
    "45:03:60",
    "50:02:22",
]

TITLES = {i: f"WoK Part 2 - 4{chr(64 + i)}" for i in range(1, 12)}


def wok_job(**kw) -> Job:
    base = dict(
        id="wok-1-2",
        series="The Stormlight Archive",
        author="Brandon Sanderson",
        author_dir=True,
        book=1,
        title="The Way of Kings",
        part=2,
        parts=5,
        discs=7,
        year=2010,
        genre="Spoken & Audio",
        composer="GraphicAudio",
    )
    base.update(kw)
    return Job(**base)


class TestToc(unittest.TestCase):
    def test_parse_and_discid(self):
        toc = parse_paranoia_toc(Q)
        self.assertEqual(toc.track_count, 11)
        self.assertEqual(toc.leadout, 227667)
        self.assertEqual(toc.tracks[0].start_lba, 0)
        self.assertEqual(toc.tracks[10].start_lba, 225172)
        self.assertEqual(cddb_id(toc), "A10BDB0B")

    def test_msf_matches_xld_cue(self):
        toc = parse_paranoia_toc(Q)
        for tr, stamp in zip(toc.tracks, INDEXES):
            self.assertEqual(msf_stamp(tr.start_lba), stamp)


class TestCue(unittest.TestCase):
    def test_write_cue_matches_xld(self):
        toc = parse_paranoia_toc(Q)
        job = wok_job()
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "disc.cue"
            text = write_cue(
                path,
                job,
                4,
                toc,
                TITLES,
                "The Stormlight Archive #1 - The Way of Kings (2 of 5) (Disc 4).flac",
            )
        self.assertIn('TITLE "The Stormlight Archive #1 - The Way of Kings (2 of 5)"', text)
        self.assertIn('PERFORMER "Brandon Sanderson"', text)
        self.assertIn('REM GENRE "Spoken & Audio"', text)
        self.assertIn('REM DATE "2010"', text)
        self.assertIn("REM DISCNUMBER 4", text)
        self.assertIn("REM TOTALDISCS 7", text)
        self.assertIn("REM DISCID A10BDB0B", text)
        self.assertIn('TITLE "WoK Part 2 - 4A"', text)
        self.assertIn('TITLE "WoK Part 2 - 4K"', text)
        self.assertIn("INDEX 01 04:57:19", text)
        self.assertIn("INDEX 01 50:02:22", text)
        self.assertIn('SONGWRITER "GraphicAudio"', text)

    def test_rewrite_header_keeps_tracks(self):
        toc = parse_paranoia_toc(Q)
        job = wok_job()
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "disc.cue"
            write_cue(path, job, 4, toc, TITLES, "old.flac")
            job2 = wok_job(author="Retagged Author")
            rewrite_cue_header(path, job2, 4, "new.flac")
            text = path.read_text()
        self.assertIn('PERFORMER "Retagged Author"', text)
        self.assertIn('FILE "new.flac" WAVE', text)
        self.assertIn("REM DISCID A10BDB0B", text)
        self.assertIn('TITLE "WoK Part 2 - 4A"', text)
        self.assertIn("INDEX 01 50:02:22", text)


class TestCatalog(unittest.TestCase):
    def test_album_and_paths(self):
        job = wok_job()
        self.assertEqual(
            job.album_name(),
            "The Stormlight Archive #1 - The Way of Kings (2 of 5)",
        )
        root = Path("/mnt/graphicaudio")
        self.assertEqual(
            job.dest_dir(root),
            root
            / "The Stormlight Archive"
            / "Brandon Sanderson"
            / "The Stormlight Archive #1 - The Way of Kings (2 of 5)",
        )
        self.assertEqual(
            job.flac_name(4),
            "The Stormlight Archive #1 - The Way of Kings (2 of 5) (Disc 4).flac",
        )
        self.assertEqual(
            job.log_name(),
            "The Stormlight Archive #1 - The Way of Kings (2 of 5).log",
        )

    def test_album_override(self):
        job = wok_job(album="Dante Valentine 01 - Working for the Devil (1 of 2)")
        self.assertEqual(job.album_name(), "Dante Valentine 01 - Working for the Devil (1 of 2)")

    def test_no_author_dir(self):
        job = wok_job(author_dir=False, series="Mistborn", title="The Final Empire", part=1, parts=3)
        p = job.dest_dir(Path("/mnt/graphicaudio"))
        self.assertEqual(
            p,
            Path("/mnt/graphicaudio")
            / "Mistborn"
            / "Mistborn #1 - The Final Empire (1 of 3)",
        )

    def test_load_real_catalog(self):
        cat = load()
        job = cat.jobs["wok-1-2"]
        self.assertEqual(cat.current, "wok-1-2")
        self.assertEqual(job.discs, 7)
        self.assertEqual(cat.drives["sr0"].offset, 6)
        self.assertGreaterEqual(cat.state_for("wok-1-2").next_disc, 5)

    def test_scan(self):
        cat = load()
        job = cat.jobs["wok-1-2"]
        st = scan_job(cat, job)
        self.assertIn(1, st.ripped)
        self.assertTrue(st.ripped)
        self.assertGreaterEqual(st.next_disc, 1)

    def test_single_disc_paths(self):
        job = Job(id="dsm", album="The Dark Side of the Moon", author="Pink Floyd", discs=1)
        self.assertEqual(job.album_name(), "The Dark Side of the Moon")
        root = Path("/tmp/music")
        self.assertEqual(job.dest_dir(root), root / "The Dark Side of the Moon")
        self.assertEqual(job.disc_dir(root, 1), root / "The Dark Side of the Moon")
        self.assertEqual(job.flac_name(1), "The Dark Side of the Moon.flac")


class TestProgress(unittest.TestCase):
    def test_parse_cb(self):
        from discographer.tui import (
            parse_cb,
            parse_to_sector,
            rip_frac,
            fmt_time,
            fmt_speed,
            speed_from_delta,
            speed_from_window,
            eta_from_speed,
            CD_1X_WORDS,
        )

        self.assertEqual(parse_cb("##: 0 [read] @ 24696"), (0, "read", 24696))
        self.assertEqual(
            parse_cb("##: -1 [finished] @ 332476367"),
            (-1, "finished", 332476367),
        )
        self.assertEqual(parse_to_sector("  to sector  303021 (track 14 [2:22.09])"), 303021)
        leadout = 303022
        self.assertLess(rip_frac(24696, leadout, "read"), 0.01)
        self.assertEqual(rip_frac(0, leadout, "finished"), 1.0)
        self.assertAlmostEqual(rip_frac(leadout * 1176 // 2, leadout, "read"), 0.5, places=2)
        self.assertEqual(fmt_time(75), "1:15")
        self.assertEqual(fmt_time(5), "0:05")
        self.assertEqual(fmt_time(None), "--:--")
        self.assertEqual(fmt_speed(None), "--x")
        self.assertEqual(fmt_speed(2.4), "2.4x")
        self.assertEqual(fmt_speed(10.2), "10x")
        self.assertAlmostEqual(speed_from_delta(CD_1X_WORDS * 8, 1.0), 8.0)
        win = [(0.0, 0), (0.5, CD_1X_WORDS * 40), (8.0, CD_1X_WORDS * 64)]
        self.assertAlmostEqual(speed_from_window(win), 8.0, places=2)
        self.assertIsNone(speed_from_window([(0.0, 0), (0.2, CD_1X_WORDS * 20)]))
        eta = eta_from_speed(CD_1X_WORDS * 40, 0.5, 8.0)
        self.assertAlmostEqual(eta, 5.0, places=2)

    def test_next_free_disc(self):
        from discographer.tui import next_free_disc

        cat = load()
        job = cat.jobs["wok-1-3"]
        left = cat.remaining(job)
        self.assertTrue(left)
        self.assertEqual(next_free_disc(cat, job, set()), left[0])
        taken = {(job.id, left[0])}
        self.assertEqual(next_free_disc(cat, job, taken), left[1] if len(left) > 1 else None)


class TestSalvageCleanup(unittest.TestCase):
    def _cat(self, td: Path):
        cat_path = td / "catalog.yaml"
        state_path = td / "state.yaml"
        cat_path.write_text(
            f"""
library: {td / "library"}
staging: {td / "staging"}
drives:
  sr0:
    path: /dev/sr0
jobs:
  test-job:
    album: Test Album
    author: Test Author
    discs: 1
"""
        )
        state_path.write_text("current: test-job\n")
        return load(cat_path, state_path)

    def test_successful_salvage_removes_staging_dir(self):
        from argparse import Namespace

        from discographer.cli import cmd_salvage

        with tempfile.TemporaryDirectory() as td:
            cat = self._cat(Path(td))
            job = cat.job("test-job")
            d = cat.staging / job.id / "disc-1-sr0"
            d.mkdir(parents=True)
            (d / job.flac_name(1)).write_bytes(b"flac")
            (d / job.cue_name(1)).write_bytes(b"cue")
            (d / job.log_name()).write_bytes(b"log")
            rc = cmd_salvage(cat, Namespace(job="test-job"))
            self.assertEqual(rc, 0)
            self.assertFalse(d.exists())
            dest = job.disc_dir(cat.library, 1)
            self.assertTrue((dest / job.flac_name(1)).is_file())
            self.assertTrue((dest / job.cue_name(1)).is_file())
            self.assertTrue((dest / job.log_name()).is_file())

    def test_failed_salvage_leaves_staging_dir(self):
        from argparse import Namespace

        from discographer.cli import cmd_salvage

        with tempfile.TemporaryDirectory() as td:
            cat = self._cat(Path(td))
            job = cat.job("test-job")
            d = cat.staging / job.id / "disc-1-sr0"
            d.mkdir(parents=True)
            (d / job.flac_name(1)).write_bytes(b"flac")
            blocked = job.disc_dir(cat.library, 1) / job.flac_name(1)
            blocked.mkdir(parents=True)
            rc = cmd_salvage(cat, Namespace(job="test-job"))
            self.assertEqual(rc, 1)
            self.assertTrue(d.exists())


if __name__ == "__main__":
    unittest.main()
