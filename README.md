# Discographer

Accurate CD ripper for one optical drive or a fleet of them. Each drive is an independent lane: a slow disc on `sr0` does not block `sr1`. Output is one **FLAC image + CUE + log per disc**, tagged from a YAML catalog instead of retyped at the tray.

It uses **cdparanoia** (secure read, sample offset) and **flac**. Track titles come from CD-Text when the disc has it.

## Install

```bash
git clone https://github.com/FortyTwoFifty/discographer.git
cd discographer
./install.sh
```

`install.sh` installs system packages (Arch/Debian/Fedora) and `pip install -e .`, which puts `discographer` on your PATH.

Manual, if you already have the tools:

```bash
# Arch
sudo pacman -S --needed python python-yaml python-pip cdparanoia flac libcdio eject

# Debian/Ubuntu
sudo apt install python3 python3-yaml python3-pip python3-venv cdparanoia flac libcdio-utils eject

pip install --user -e .
```

Required commands: `python3` (≥3.11), `cdparanoia`, `flac`/`metaflac`, `cd-info`, `eject`, `stdbuf`.

Copy the example catalog and edit it:

```bash
mkdir -p ~/.config/discographer
cp catalog.example.yaml ~/.config/discographer/catalog.yaml
```

If `catalog.yaml` sits in the repo directory, that is used instead. Override with `--catalog` or `DISCOGRAPHER_CATALOG`.

## Everyday

```bash
discographer scan          # sync ripped discs from the library folders
discographer               # interactive session (also: discographer session)
```

At start, each detected `/dev/sr*` (or each drive listed in the catalog) is a lane. You assign a **job** per drive — they can be the same album or two different ones. For each job you can turn on **auto-start** (default yes for multi-disc jobs): after a disc ejects, inserting the next one starts that lane without pressing Enter. `--auto` / `--no-auto` skip the question.

The TUI tells you which disc to put in which tray. Manual lanes **wait for Enter**; auto lanes start as soon as a disc is loaded (they will not immediately retry a failed or cancelled disc still in the tray — Enter retries, or eject and insert).

While a drive rips you see a progress bar, CD-relative speed (`8.2x`), and ETA. When it finishes it ejects and immediately arms **that drive’s** next disc. The other lanes keep going.

| Key | Action |
|---|---|
| Enter | start idle lanes that have a disc loaded |
| `a` / `a sr0` | toggle auto-start on an idle lane |
| `j` | change job on an idle lane |
| `x` / `x sr0` | cancel the in-flight rip on that drive; other lanes keep going |
| `s` / `s sr0` | skip this disc (unreadable); record the gap and arm the next one |
| `q` | finish in-flight rips, then quit (`x`/`s` still work while quitting) |

One-shot, no session:

```bash
discographer rip -d sr0
discographer toc -d sr0
discographer devices
```

## Catalog

`catalog.yaml` is what you edit. `state.yaml` (next to it) is written by the tool: `ripped`, `next_disc`, `pending`, `skipped`.

```yaml
library: ~/Music
staging: ~/.cache/discographer

defaults:
  genre: Rock

jobs:
  dark-side:
    album: The Dark Side of the Moon
    author: Pink Floyd
    year: 1973
    discs: 1

  mistborn-1:
    series: Mistborn
    author: Brandon Sanderson
    book: 1
    title: The Final Empire
    part: 1
    parts: 3
    discs: 6
    year: 2010
    genre: Spoken & Audio
    composer: GraphicAudio
```

| Field | Meaning |
|---|---|
| `album` | Exact album tag and folder name (overrides the derived string) |
| `author` / `artist` | Album artist |
| `title` | Used when `album` is omitted |
| `series`, `book`, `part`, `parts` | Derive `Series #N - Title (P of Q)` |
| `discs` | How many CDs in this job (default 1) |
| `year`, `genre`, `composer` | Tags (composer/genre omitted if empty) |
| `author_dir` | Put an artist folder under `series/` (or under `library/`) |

**One-disc** jobs write `library/Album/Album.flac`. **Multi-disc** jobs write `library/.../Album/Disc N/Album (Disc N).flac` plus matching `.cue` and `.log`.

Rips go to `staging` first, then a checksum copy into `library` (plain writes, so SMB/GVFS mounts work).

### Drives

If `drives:` is omitted, every `/dev/sr*` is used with read offset **+6**. Catalog entries overlay auto-detect, so a third USB drive (`sr2`) still appears.

```yaml
drives:
  sr0:
    path: /dev/sr0
    name: HL-DT-ST DVDRAM SP80NB60
    offset: 6
  sr1:
    path: /dev/sr1
    name: ASUS SDRW-08V1M-U
    offset: 6
```

**Sample offset.** Each drive model starts reading a fixed number of samples early or late relative to the audio on the disc. `offset` is that correction, in samples, passed to `cdparanoia -O`. With the right value, the FLAC is the same PCM as any other correctly-offset rip of that pressing. With the wrong value, the whole image is shifted: samples fall off one end of the disc and junk (or silence) appears at the other. Track CRC32s in the log will not match a known-good rip, and AccurateRip will fail even on a clean read.

The shift is tiny in time (6 samples is about 0.14 ms at 44.1 kHz) so it is not something you "hear as a delay." It is still the wrong bits, and it will not match rips from a drive that is set correctly.

The built-in **+6** is a fallback for unlisted `/dev/sr*` devices — it matches some slim USB burners in the example, not necessarily yours. Look the model up (`discographer devices` prints `name`) at <http://www.accuraterip.com/driveoffsets.htm> and set `offset` per drive. Table values are often small (`6`, `48`) but can be large (`667`) or negative (`-582`). Mixed models in a fleet need a different offset on each lane.

Change it when you add a drive whose table value is not 6, or when two drives ripping the same disc write different CRC32s.

## Commands

| Command | What |
|---|---|
| *(none)* / `session` | interactive multi-drive session |
| `status` | current job, drives, next disc |
| `jobs` | all catalog jobs |
| `set JOB` | default job id |
| `scan [JOB]` | set `ripped` / `next_disc` from library folders (keeps `skipped`) |
| `devices` | drives + TOC/CD-Text if a disc is loaded |
| `toc [-d sr0]` | TOC and CD-Text |
| `rip [-d sr0] [--disc N]` | one disc, no session |
| `skip N [JOB]` | mark disc N unreadable and continue the job without it |
| `unskip N [JOB]` | put a skipped disc back on the remaining list |
| `salvage [JOB]` | copy finished staging files into the library |
| `retag [JOB]` | rewrite tags/CUE from the catalog, no re-rip |
| `retag --rename-from "Old Album"` | move files, then retag |
| `eject [sr0\|all]` | eject |

`--dry-run`, `--force`, `--keep` (leave staging WAV/FLAC), `--no-eject` apply to `rip` and `session`. `--auto` / `--no-auto` apply to `session`.

## Damaged discs

discographer does not give up on a slow disc by itself. `cdparanoia -z20` retries each unreadable sector up to 20 times, then skips that sector and continues. A scratched GraphicAudio disc can sit on one sector for a long time; the lane will keep trying until the read finishes, the process fails, or you cancel it. There is no overall time limit on a rip. The other drives are independent and are not blocked.

A rip that *does* finish after retries is still marked ripped. The `.log` will say to check the cdparanoia summary if it contains reconstruction or skips — that disc may have silent or interpolated samples.

When a disc will not come back:

- **`x`** (or `x sr0`) kills cdparanoia on that drive only, ejects, and offers the **same** disc again. Use this to stop a hopeless attempt without recording a gap — retry later, try the other drive, or skip next.
- **`s`** (or `s sr0`) is the collection-level decision: abort if it is ripping, mark that disc **skipped** in `state.yaml`, and arm the next remaining disc. The job can complete with a hole. `status` / `jobs` show `skipped: [2]`.
- **`discographer skip 2`** / **`unskip 2`** do the same outside a session (current job, or pass the job id).

Skipped discs are not offered in a session and do not block the rest of a set. `scan` keeps them (it only drops a skip if that disc’s FLAC later appears in the library). To retry a replacement disc: `unskip N` then session, or `discographer rip --disc N`. A successful rip clears the skip. A failed retry of a still-skipped disc stays skipped.

`q` still waits for in-flight rips. If one lane is stuck, `x` or `s` that drive, then `q`.

## Accuracy

This is not AccurateRip-as-a-workflow. Many discs (private CD-Rs, small-run sets) are not in that database. discographer uses cdparanoia secure reads, a configured sample offset (see **Drives**), and `--never-skip=20`. Logs include per-track CRC32.
