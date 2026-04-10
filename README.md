# encoder-thing

x265/AV1 batch encoder with a live TUI — queue display, progress bar, fps/speed/ETA,
and keyboard controls for pause, skip, and quit.

## Setup

```
./install.fish
```

Creates `.venv/`, installs `rich`, and optionally symlinks `encoder-thing` into
`~/.local/bin` so you can run it from anywhere.

## Usage

```
encoder-thing [flags] <input_file|dir> [...]
```

When passed a directory, output goes to `<dir>/converted/`. Multiple directories
can be passed at once, each gets its own `converted/` subfolder:

```
encoder-thing --ivtc /shows/rocko/s01 /shows/rocko/s02 /shows/rocko/s03
```

## Keys

| Key | Action |
|---|---|
| `p` / `space` | Pause / resume |
| `s` | Skip current file (deletes partial output) |
| `q` / `^C` | Quit |

## Flags

| Flag | Description |
|---|---|
| `-o <dir>` | Output directory for file inputs (default: `./converted`) |
| `--codec <codec>` | Video codec: `x265` (default) or `av1` (libsvtav1) |
| `--crf <n>` | CRF value (default: auto — x265: 18 SD/HD / 20 4K, AV1: 30 SD/HD / 35 4K) |
| `--preset <p>` | Encoder preset (default: `medium` for x265, `5` for AV1) |
| `--grain <n>` | AV1 film grain synthesis level 0–50 (AV1 only) |
| `--ivtc` | Inverse telecine (`fieldmatch,decimate`) for 24fps film in 480i |
| `-y` | IVTC + yadif for irregular pulldown or residual interlace artifacts |
| `--deint <filter>` | Deinterlace only — `yadif`, `bwdif`, `estdif`, `w3fdif` |
| `--crop [value]` | Auto-detect letterbox/pillarbox bars, or supply `crop=W:H:X:Y` |
| `-f` / `--force` | Re-encode even if output already exists |
| `--clean` | Delete all existing output files before encoding |
| `--ffmpeg <path>` | Path to ffmpeg binary (default: `ffmpeg`) |
| `--ffprobe <path>` | Path to ffprobe binary (default: `ffprobe`) |

Flags can be freely combined — e.g. `--ivtc --crop`, `--codec av1 --crf 28 --grain 8`.

## Examples

```
# Plain progressive encode (x265)
encoder-thing movie.mkv

# AV1 encode
encoder-thing --codec av1 movie.mkv

# AV1 with film grain synthesis (good for grainy source material)
encoder-thing --codec av1 --grain 8 movie.mkv

# AV1 with custom CRF and preset
encoder-thing --codec av1 --crf 28 --preset 4 movie.mkv

# IVTC for telecined 480i, multiple seasons
encoder-thing --ivtc /shows/rocko/s01 /shows/rocko/s02

# IVTC + yadif for tricky pulldown
encoder-thing -y --crf 16 --preset slow episode.mkv

# Deinterlace only
encoder-thing --deint bwdif episode.mkv

# Auto-detect crop bars
encoder-thing --crop movie.mkv

# Manual crop value
encoder-thing --crop crop=536:480:92:0 /path/to/season/
```

For 480i deinterlaced with `--deint bwdif`, `--crf 16 --preset slow` produces
the best results.

### AV1 notes

- Uses **libsvtav1**. Requires ffmpeg built with SVT-AV1 support.
- SVT-AV1 presets run 0 (slowest/best) to 13 (fastest). Default is `5`.
- `--grain` enables film grain synthesis — the encoder strips grain from the
  source, stores it as metadata, and the decoder re-applies it. Effective range
  is roughly 0–50; 4–10 suits most live-action content.
- 4K HDR sources automatically get `enable-hdr=1`.
