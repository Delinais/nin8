# NINEIGHT ↔ Furnace integration

Goal: compose adaptive music for a Godot game ([LG](https://github.com/Delinais/LG)) with Furnace,
using patches designed in NINEIGHT. Constraints: stock Furnace release (no fork), small additions
to nin8 only, commercial / closed-source game.

## The decision

Furnace already contains a YM2608 built on the same ymfm core as nin8, so nin8 is not an
"instrument inside Furnace" — it is the **patch lab**. Furnace is the tracker and renderer.

```
 nin8 (design / audition)          Furnace (sequence, transpose, render)          Godot (LG)
 ┌────────────────────┐  .fui   ┌──────────────────────────────────────┐  stems  ┌─────────────────────┐
 │ patches, keyboard  │ ───────▶│ instruments ▸ song ▸ headless render │ ───────▶│ AudioStream*        │
 └────────────────────┘ built ✔ └──────────────────────────────────────┘ designed└─────────────────────┘
        ▲   MIDI-out (keyboard capture)  designed, not built  ─────────▶ Furnace MIDI-in
```

| Piece | State |
| --- | --- |
| nin8 → Furnace patch export (`scripts/nin8fui.py`) | **built and verified** (below) |
| Native auto-export from nin8 (no script step for your own edits) | designed, not built |
| nin8 keyboard → Furnace pattern capture | designed, not built |
| Furnace pattern → nin8 as a sound module | works with caveats; **not recommended** |
| Furnace → stems/clips → Godot | designed (commands verified in source, pipeline script not written) |
| ymfm runtime inside the game (procedural music/SFX) | later; patches stay as data so nothing is redone |

## Patch export (built)

`scripts/nin8fui.py` — see the README for usage. Field mapping (nin8 APVTS id → Furnace):

| nin8 | Furnace `.fui` FM feature | note |
| --- | --- | --- |
| `alg`, `fb` | algorithm, feedback | |
| `lfo_ams`, `lfo_pms` | AMS, FMS | per instrument |
| `lfo_freq` | `ex3` macro (LFO speed) | LFO speed is not an instrument field; written when AMS/PMS ≠ 0 |
| `opN_ar dr rr sl tl mul am ssgeg` | AR DR RR SL TL MULT AM SSG-EG | raw chip values, copied |
| `opN_sr` | D2R | |
| `opN_ks` | RS | |
| `opN_dt` | DT | **chip code → Furnace `signed detune + 3`**: `(3,4,5,6,7,2,1,0)[code]` |
| operator order | op slots 1,3,2,4 | **nin8 op2 and op3 swap** |
| — | `kvs = 2`, `block = 0`, all four operators enabled | automatic TL volume scaling, automatic block |
| `pan`, `master_gain`, `poly` | not exported | not instrument data (nin8's pan is a software panner) |

Format version 228 (Furnace 0.6.8.x). Newer builds read it silently; writing 256 makes a stable
build warn "made with a more recent version".

### Verification

Three independent verifiers were told to distrust the script and derive expectations from Furnace's
source. All reported against Furnace **dev256** (the stable 0.6.8.x source was not available).

- **Furnace's real reader and writer** (`instrument.cpp`, `safeReader/Writer.cpp`, `macroInt.cpp`,
  compiled in a harness with stubs for `fmt`/sample code): the 48 bank files plus 10,000 fuzzed and
  442 extreme-value files decode field-for-field as intended, the LFO macro parses as a single
  value, and Furnace's own writer reproduces the script's bytes (only the version word differs).
- **Chip level**: 548,640 voice-register comparisons (nin8's real write trace vs. Furnace's
  `ym2608.cpp` logic vs. the real Furnace engine loading the file) — 0 mismatches — and 3,144
  one-second chip renders **bit-identical** between nin8's and Furnace's ymfm copies.
- **Adversarial QA** found the CLI problems that are now fixed (silent overwrite of same-named
  patches, tracebacks on malformed input, partial output on long names, an off-menu preset bank) and
  a weak test suite; the suite now kills 25/25 hand-made mutants.

**Not verified:** the Furnace GUI or a stable 0.6.8.x binary; a real session (key-on timing against the
chip's envelope/LFO counters is not bit-identical); audio after Furnace's resampler; Python 3.8–3.10
at runtime (syntax checked only); Windows/macOS filesystem behaviour.

### Sound parity checklist

The registers are identical; whether a session *sounds* identical depends on Furnace settings:

1. **YM2608 core = "ymfm only"** for playback and render. The default (Nuked-OPN2 for FM) is close but not
   identical: over the 48 presets the worst 1/3-octave band differed by a median of 0.13 dB, 4.6 dB at the
   90th percentile and 13.5 dB at worst (indicative; measured with a re-implementation of Furnace's Nuked write loop).
2. **8 MHz clock**, default prescaler.
3. **Full note volume**: `kvs = 2` scales carrier TL when volume < full; nin8 has no velocity.
4. Pitch: Furnace's note→F-number table differs from nin8's by ≤ 1 F-number step (≤ 2.65 cents) on about
   half the notes, and nin8 clamps above MIDI 107. Enter raw F-numbers if bit-exact pitch ever matters.
5. nin8 adds a software pan and master gain, and renders at a different internal rate (OPN fidelity MED vs
   Furnace's MIN); neither is part of an instrument.
6. The LFO is one chip-wide register. Instruments *without* AMS/PMS deliberately do not write it (they
   must not reset the speed another channel relies on).

## Getting your own edits out of nin8

Today the plugin cannot export: Ctrl+D downloads are unhandled by the embedded WebKit (untested, but
JUCE's Linux backend has no download handler), file drops are untested, and the engine only learns the UI
state on the first key press (the UI keeps its own patch object). Working inputs now: the bundled bank,
a share link from the web app, hand-written preset JSON.

**Planned (needs your OK, ~100–150 lines of nin8 C++ plus a compose bind mount):** a JUCE-free
`FuiExport.h` written from `papers/newIns.md`; a debounced writer (message-thread timer, never the audio
thread) that writes `current.fui` into a bind-mounted `./furnace-export` whenever a parameter changes;
point Furnace's file dialog at that folder and use *Open → replace*. Must be tested against the
first-key-press sync. Furnace has no folder watching, OSC or CLI import, so *replace* stays manual.

## Capture: nin8's keyboard → a Furnace pattern (designed, not built)

Furnace can only *subscribe* to an existing ALSA-sequencer port (it never creates one) and its RtMidi is
ALSA-only. nin8 must therefore create a readable port:

1. In `PluginProcessor` (Standalone only): `MidiOutput::createNewDevice("NINEIGHT Out")`; null-check it
   (absent when `/dev/snd/seq` is). ~25 lines.
2. Echo UI, QWERTY and hardware notes (fixed velocity) into a separate `MidiBuffer`; send from the audio
   thread after the render loop. UI/QWERTY notes use a private queue today, so they bypass `processBlock`'s
   MIDI buffer — the echo must hook the queue drain.
3. Run with `compose.midi.yaml` (already provides `/dev/snd/seq` and the audio gid; `docker/setup.sh` enables it).
   The sequencer is kernel-wide, so a host Furnace sees the container's port (inferred, untested). JUCE 8.0.14
   talks to ALSA through its UMP API, which needs kernel ≥ 6.5 and a UMP-capable alsa-lib in the Alpine image
   (not checked); if the port does not appear or carries no bytes, check `aconnect -l` / `aseqdump` first.
4. Furnace: Settings → MIDI → input device → *Re-scan* → pick `NINEIGHT:NINEIGHT Out` → OK. Edit mode on,
   cursor in a Note column, the exported instrument selected.

Limits of stock Furnace capture: timestamps are ignored and notes snap to the nearest row; note-offs are
never recorded (add `OFF` rows by hand); pitch bend/sustain/aftertouch are dropped; Furnace plays each
note with *its own* instrument, so mute nin8 or you hear both; there is no hotplug (restart nin8 ⇒
re-scan and apply). A hardware MIDI keyboard can feed Furnace directly with no nin8 code. Do not route
Furnace MIDI-out back into nin8 while capturing (echo loops).

## Furnace as sequencer for nin8 (not recommended)

Works with stock builds via an ALSA loopback (Midi Through) and nin8's MIDI input, but nin8 ignores
velocity, bend, CC and program change and plays one patch on one 6-voice pool; Furnace's slides, vibrato
and FM macros don't survive MIDI; timing is unscheduled (~2 buffers of jitter); Furnace's channel mute
also mutes MIDI; and Furnace's exports never contain nin8's audio. Since patches transfer losslessly into
Furnace's own YM2608 it buys only live audition.

## Game audio pipeline (LG: Godot 4.4.1, Rust GDExtension, desktop targets)

Godot 4.4.1 already has the building blocks (class docs read from the 4.4.1 source):

| Technique | Godot | Furnace output it wants |
| --- | --- | --- |
| Vertical mixing | `AudioStreamSynchronized` (per-layer volume) | loop-aligned **stems** of equal length |
| Horizontal re-sequencing | `AudioStreamInteractive` (clips + transition table, beat/bar-quantised, fades, filler clips) | one clip per section, with BPM/beat metadata |
| Stingers | one-shots, or a transition in the interactive stream | short renders with tail |
| Playlists / variation | `AudioStreamPlaylist`, `AudioStreamRandomizer` | |
| Procedural (arrangement level) | scripts choosing clips/layers | the above |
| Procedural (note level), parametric chip SFX | `AudioStreamGenerator` or a custom stream from your `gdext` crate | needs a runtime chip (later) |

`AudioStreamOggVorbis` carries `bpm`, `beat_count`, `bar_beats`, `loop`, `loop_offset` for the interactive stream.

Headless rendering needs no audio device (dev256 flags, read from `src/main.cpp`, **not run** — check
`furnace --help` on your stable). Long flags take `--output file` or `--output=file`; short flags only
work with `=` (`-o=file`).

```sh
furnace --output=song.ogg --outformat=vorbis song.fur            # u8/s16/f32 (WAV), flac, opus, vorbis, mp3
furnace --output=stem.wav --outmode=perchan song.fur             # one file per channel (…_cNN.wav, re-rendered with the others muted)
furnace --output=intro.wav --subsong=1 song.fur                  # one subsong (e.g. one section) per render
furnace --vgmout=song.vgm song.fur                               # register stream; --cmdout writes Furnace's command stream
```

Sample rate is fixed at 44.1 kHz, and there is no fade-out or channel-mask flag; **loop-safe stems still need
a small render-and-trim script** (render the loop twice, cut the second pass so reverb/release tails wrap)
— not written yet. Rendering uses the *render* core setting (`opnaCoreRender`); set it to ymfm.
The official Linux binaries are likely glibc builds; for CI run them in a Debian/Ubuntu container or build with
`-DBUILD_GUI=OFF` (both untested).

**Plan:** hybrid. Build the pre-rendered path first. When procedural note-level music or parametric SFX
are wanted, embed ymfm (BSD-3) in the Rust extension and drive it with the same patch data; nin8's
`src/engine` is JUCE-free (see `tools/render_test.cpp`) and is the natural starting point.

## Licensing and provenance (not legal advice)

- `nin8fui.py` is written from Furnace's public format notes; no Furnace source is copied (the only
  Furnace-derived datum is the 8-entry detune table). That keeps it usable for a closed-source game's
  tooling regardless of Furnace's GPL-2.0-or-later.
- nin8 is a *tool*; it is not shipped inside the game. nin8 has no LICENSE file and JUCE 8 is **AGPLv3 or a
  commercial licence** (the README's "GPLv3/commercial" is inaccurate). Decide before distributing nin8 itself.
- The 48 bundled presets and the 8 KiB rhythm ROM have no recorded origin or licence (the ROM generator
  reads from an `opna` repo outside this one; the bytes equal Furnace's rhythm ROM). Names like "Manbow
  bass" and "Breeze: …" suggest outside sources. Confirm rights before shipping any of them, or design
  your own. Rendered audio from your own patches is unaffected.

## Observations about nin8 itself (not acted on)

- The UI's detune slider labels mirror the hardware: it shows "−1" for chip code 7, which is really −3.
  Sound is identical; Furnace's editor shows the true value, so the same patch reads differently.
- nin8's bundled ymfm copy decodes a port-1 write to register `0x28` as FM key-on/off (Furnace's copy only
  decodes port 0). nin8's init writes port-1 `0x28`; harmless before the first note.
- `master_gain` defaults to 0.5 in `Params.h` but the UI pushes 0.8 on its first sync, overwriting a host/DAW value.
