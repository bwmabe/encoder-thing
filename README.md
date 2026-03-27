# encoder-thing

x265 batch encoder with a live TUI — queue display, progress bar, fps/speed/ETA,
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
| `--crf <n>` | x265 CRF (default: auto — 18 for SD/HD, 20 for 4K) |
| `--preset <p>` | x265 preset (default: `medium`) |
| `--ivtc` | Inverse telecine (`fieldmatch,decimate`) for 24fps film in 480i |
| `-y` | IVTC + yadif for irregular pulldown or residual interlace artifacts |
| `--deint <filter>` | Deinterlace only — `yadif`, `bwdif`, `estdif`, `w3fdif` |
| `--crop [value]` | Auto-detect letterbox/pillarbox bars, or supply `crop=W:H:X:Y` |

Flags can be freely combined — e.g. `--ivtc --crop`, `-y --crf 16 --preset slow`.

## Examples

```
# Plain progressive encode
encoder-thing movie.mkv

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
