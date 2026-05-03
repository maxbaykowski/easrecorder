# easrecorder
This program can record EAS alerts and save them as audio files. It can decode from any program that can output PCM signed 16 bit audio, so it is very versatile.

## Install

```bash
pip install .
```

This package has no Python dependencies, but it requires these external tools at runtime:

- `ffmpeg`
- `multimon-ng`

On Debian based distros, you can install these dependencies by running:
```bash
sudo apt install ffmpeg multimon-ng
```

You may have to compile `multimon-ng` from source if you're using some other distro.

## Usage

After installation, run:

```bash
easrecorder --help
```

## Usage examples

At minimum, you'll need `sox` to run most of these examples. While ffmpeg works too, most people prefer `sox` better as its syntax is easier to remember. You might also want to grab `rtl-sdr` while you're at it, as this readme does provide examples for decoding EAS alerts with an RTL SDR dongle.

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

