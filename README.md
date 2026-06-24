# easrecorder
This program can record EAS alerts and save them as audio files. It can decode from any program that can output PCM signed 16 bit audio, so it is very versatile.

## Install

```bash
pip install .
```

The package installs these Python dependencies automatically:

- `soxr` for streaming sample-rate conversion
- `lameenc` for optional MP3 encoding

It requires one external tool at runtime:

- `multimon-ng` for decoding SAME headers

On Debian based distros, you can install these dependencies by running:
```bash
sudo apt install multimon-ng
```

You may have to compile `multimon-ng` from source if you're using some other distro.

## Usage

After installation, run:

```bash
easrecorder --help
```

## Python API

You can also run the recorder from another Python program without spawning the
`easrecorder` command:

```python
from easrecorder import EASRecorder, RecorderSettings

settings = RecorderSettings(
    rate=22050,
    outdir="/tmp/easrecorder",
    pre_seconds=2.0,
    post_seconds=5.0,
    max_seconds=120,
    save_format="wav",
    index_path="index.json",
)

recorder = EASRecorder(settings)
recorder.run()
```

`run()` reads raw PCM bytes from `input_stream` until EOF. If you want to feed
audio samples from your own code, use `start()`, `write()`, and `stop()`:

```python
from easrecorder import EASRecorder, RecorderSettings

settings = RecorderSettings(rate=22050, outdir="/tmp/easrecorder")
recorder = EASRecorder(settings)

recorder.start()
try:
    recorder.write(pcm_bytes)
    recorder.write(more_pcm_bytes)
finally:
    recorder.stop()
```

The bytes passed to `write()` must be raw signed 16-bit little-endian mono PCM
at `settings.rate`. `rate` and `detect_rate` configure the decoder pipeline
when `start()` runs, so changing those requires restarting the recorder.

All command-line options are available through `RecorderSettings`:

| CLI option | API setting |
| --- | --- |
| `--rate` | `rate` |
| `--detect-rate` | `detect_rate` |
| `--outdir` | `outdir` |
| `--max-seconds` | `max_seconds` |
| `--prefix` | `prefix` |
| `--mp3` | `save_format="mp3"` |
| `--local-time` | `local_time=True` |
| `--reconstruct-same` | `reconstruct_same=True` |
| `--tone` | `tone` |
| `--tone-duration` | `tone_duration` |
| `--year` | `year` |
| `--pre-seconds` | `pre_seconds` |
| `--post-seconds` | `post_seconds` |
| `--index [PATH]` | `index_path` |
| `--stdout` | `copy_stdout=True` |

The settings object is mutable. Alert-scoped values such as `pre_seconds`,
`post_seconds`, `max_seconds`, `outdir`, `prefix`, `save_format`, `local_time`,
`reconstruct_same`, `tone`, `tone_duration`, `year`, and `index_path` are
snapshotted when a new SAME header starts. If you change one of those values
during an active alert, the change applies to the next alert. `save_format` can
be `"wav"` or `"mp3"`. Set `index_path` to a path to enable indexing or to
`None` to disable it live. Relative index paths are resolved inside `outdir`.

## Alert index

Pass `--index` to maintain `index.json` in the output directory, or pass an
explicit path with `--index PATH`. Existing indexes are loaded and updated on
subsequent runs. Alerts are sorted newest-first and updates use atomic file
replacement.

The index is a versioned JSON object:

```json
{
  "version": 1,
  "alerts": [
    {
      "raw_same_header": "ZCZC-WXR-TOR-039173-039051+0030-1591829-KCLE/NWS-",
      "event_type": "TOR",
      "originator": "WXR",
      "fips_codes": ["039173", "039051"],
      "start_time_utc": "2026-06-08T18:29:00Z",
      "duration_code": "0030",
      "duration_seconds": 1800,
      "expires_at_utc": "2026-06-08T18:59:00Z",
      "sender_id": "KCLE/NWS",
      "file_path": "/absolute/path/to/TOR-06-08-2026-1829UTC.wav"
    }
  ]
}
```

Unknown event or originator codes are retained as received. `start_time_utc`
comes from the SAME Julian day/time field and uses the configured `year`, or
the current system year when no year is configured. The public
`parse_same_header()` function exposes the same parser to Python applications.

## Usage examples

At minimum, you'll need `sox` to run most of these examples. You might also want to grab `rtl-sdr` while you're at it, as this readme does provide examples for decoding EAS alerts with an RTL SDR dongle.

### Decode from the default input device

```bash
rec -ts16 -r22050 -c1 - | easrecorder --rate 22050 --outdir ~/easrecorder
```

With this, you should be able to play EAS tones on another device and your PC's microphone should pick them up. You can also simply plug in a scanner and decode alerts that way.

### Decode from an audio file

```bash
sox file.mp3 -t s16 -c 1 -r 22050 -c 1 - | easrecorder --rate 22050 --outdir ~/easrecorder
```

Decode and record EAS alerts from a crusty old audio file you have saved on your drive. Useful if you recorded NOAA weather radio for several hours during a severe weather event, but don't feel like manually going through and picking out all the alerts. Note that if you don't know the sample rate of the file, it's best to resample it with `sox` so you know what the rate is, as `easrecorder` requires that you specify the rate of the audio being passed to it.

### Decode from an internet radio stream

```bash
sox "https://example.com/stream.mp3" -t s16 -r 22050 -c 1 - | easrecorder --rate 22050 --outdir ~/easrecorder
```

With the above example, you can decode and record EAS alerts sent over an internet radio station. This is useful, for instance, if you want to decode alerts from online streams of NOAA Weather Radio, such as those found on the [Ice Cast page](https://wxr.gwes-cdn.net/) of [GWES Weather Radio](https://weatherradio.org/). Note that if the mountpoint for the stream does not include the encoding format, or if it is wrong, you will have to specify the format of the stream in `sox` with the `-t` flag. For example, if the stream URL ends in `/mountpoint.mp3`, you do not need to specify the encoding format. However, if the stream URL ends in `/mountpoint` with no file extension, you must specify the stream format As such, the command would look something like this:

```bash
sox -t mp3 "https://example.com/stream" -t s16 -r 22050 -c 1 - | easrecorder --rate 22050 --outdir ~/easrecorder
```

### Decode from an RTL SDR dongle

#### From NOAA Weather Radio

```bash
rtl_fm -E deemp -F 9 -g 20 -M fm -f 162.475M -s 16000 - | easrecorder --rate 16000 --outdir ~/easrecorder
```

Decode and record from NOAA weather radio 162.475 MHz.

#### Decode from NOAA Weather radio and listen to the output

```bash
rtl_fm -E deemp -F 9 -g 20 -M fm -f 162.475M -s 16000 - \
  | easrecorder --rate 16000 --outdir ~/easrecorder --stdout \
  | sox -ts16 -c1 -r 16000 -
```

Decode and record from NOAA weather radio 162.475 MHz while playing the incoming audio through the default soundcard.

#### From broadcast FM

```bash
rtl_fm -E deemp -F 9 -g 20 -M fm -f 88.1M -s 240000 - \
  | sox -t s16 -r 240000 -c 1 - -t s16 -r 32000 -c 1 - \
  | easrecorder --rate 32000 --outdir ~/easrecorder
```

Decode and record from 88.1 FM. The built-in resampler in `rtl_fm` isn't the greatest, so we resample from 240 KHz to 32 KHz using `sox` instead.

#### Decode from broadcast FM and listen to the output

```bash
rtl_fm -E deemp -F 9 -g 20 -M fm -f 88.1M -s 240000 - \
  | sox -t s16 -r 240000 -c 1 - -t s16 -r 32000 -c 1 - \
  | easrecorder --rate 32000 --outdir ~/easrecorder --stdout \
  | play -t s16 -c 1 -r 32000 -
```

Decode and record from 88.1 FM while playing the incoming audio through the default soundcard. The built-in resampler in `rtl_fm` isn't the greatest, so we resample from 240 KHz to 32 KHz using `sox` instead.
