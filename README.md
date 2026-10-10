# NINEIGHT - OPNA (YM2608) FM Synth VST v0.1.0(Beta)

![NINEIGHT plugin UI](nin8-vst.png)

C++ port of the [NINEIGHT](https://delacannon.itch.io/nineigth) web app "NINEIGHT" as **VST3 / AU / Standalone** (JUCE 8).

- **Audio**: native `ymfm::ym2608` — the _same_ emulator the web app compiles to WASM. Register packing, voice allocation, resampler and rhythm-ROM init are line-for-line ports of `@opna/core`, so the plugin sounds identical.
- **UI**: the _actual_ Phaser 3 web UI (PC-98 CRT aesthetic) embedded via JUCE 8 `WebBrowserComponent`, talking to the native engine over a JS↔C++ bridge (`window.__JUCE__`).
- 52 host-automatable parameters (algorithm, feedback, LFO, gain, pan, poly, 4 ops × 11); state = APVTS XML.

## Build (Linux)

```sh
cmake -B build -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_C_COMPILER=clang-12 -DCMAKE_CXX_COMPILER=clang++-12   # gcc 9 breaks on JUCE 8 harfbuzz
cmake --build build --parallel
```

Outputs land in `build/Nineight_artefacts/Release/{VST3,Standalone}` and are copied to `~/.vst3`.

Deps: `libwebkit2gtk-4.0-dev` + usual JUCE X11/ALSA packages.

## Run in Docker (Linux desktop)

Runs the **Standalone** synth from a lean Alpine image: the window opens on your
desktop and the sound goes out through your host's PipeWire/PulseAudio, in the
spirit of `run-mtgo`. Nothing to install besides Docker; JUCE is fetched at the
pinned commit during the build, so a plain `git clone` (no submodules) is enough.
(VST3/AU plugins are not part of this — build natively for a DAW.)

```sh
docker compose up --build -d      # build the image and open NINEIGHT on your desktop
docker compose logs -f            # startup problems show up here
docker compose down               # stop (closing the window also stops it)
```

Run it as your normal user from a terminal inside your desktop session — **not
with sudo**: the session's `DISPLAY` / `XAUTHORITY` / `XDG_RUNTIME_DIR` are what get
wired into the container. It assumes uid/gid 1000 (the first user on Arch/Garuda);
for anything else run `./docker/setup.sh` once. It checks Docker, X11, audio and
MIDI on the host and writes `.env` with your uid/gid.

**Arch / Garuda host setup** (Dr460nized/KDE Plasma on Wayland or X11 both work):

```sh
sudo pacman -S docker docker-compose docker-buildx
sudo systemctl enable --now docker.socket
sudo usermod -aG docker "$USER"   # log out and back in afterwards
```

How it reaches the desktop (all in [`compose.yaml`](compose.yaml)):

| Piece | Mechanism |
| --- | --- |
| Window | Host X11 socket + your `XAUTHORITY` cookie, container runs as your uid and shares the host UTS namespace so the cookie matches. On a Wayland session this goes through XWayland — JUCE embeds the WebKit UI over X11, so native Wayland isn't possible. |
| Audio | `$XDG_RUNTIME_DIR/pulse` (PipeWire's `pipewire-pulse`, or PulseAudio) mounted in; JUCE's ALSA backend reaches it through `alsa-plugins-pulse`. No `/dev/snd` needed. Pick "Default"/"Pulseaudio" in the app's audio settings. |
| MIDI (optional) | `/dev/snd/seq` via [`compose.midi.yaml`](compose.midi.yaml), enabled automatically by `docker/setup.sh` when the device exists. |
| Settings | Named volume `nin8_nin8-home` (`~/.config/NINEIGHT`: audio device, etc.). |
| GPU | Not used — JUCE pins WebKit to software rendering, so no `/dev/dri`. |

Troubleshooting:

- `bind source path does not exist: …` — compose names the missing host path:
  `…/pulse` means PipeWire-pulse isn't running; an `Xauthority` path means `XAUTHORITY`
  isn't exported in the shell you ran compose from.
- Container exits at once — `docker compose logs` (the entrypoint says what's missing).
- `Authorization required` / can't open display — the cookie isn't readable: run as
  the desktop user, and check `NIN8_UID`/`NIN8_GID` in `.env` match `id`.
- Image build killed (out of memory during LTO) — `NIN8_BUILD_JOBS=4` in `.env`.
- Bumping the JUCE submodule? Update `JUCE_COMMIT` in the [`Dockerfile`](Dockerfile) to match.

## Furnace integration (patch export)

[Furnace](https://github.com/tildearrow/furnace) has its own YM2608 built on the same
ymfm core, so the useful link is patch transfer: design in NINEIGHT, then sequence and
render in Furnace. `scripts/nin8fui.py` (Python 3, no dependencies) writes NINEIGHT
patches as Furnace instrument files (`.fui`) that a **stock Furnace** loads. Design notes,
verification status and the game-audio pipeline are in
[docs/furnace-integration.md](docs/furnace-integration.md).

```sh
python3 scripts/nin8fui.py bank ~/furnace-nin8     # nin8's 31 menu presets -> 01_Lead 1.fui, 02_bb.fui, ...
                                                   #   (--all adds the 17 bundled presets nin8's menu never offers)
python3 scripts/nin8fui.py convert 'https://…/#patch=…' -o wobble.fui --name Wobble   # a web-app share link
python3 scripts/nin8fui.py convert preset.json -o out.fui    # preset JSON (the UI's Ctrl+D / drop format)
python3 scripts/nin8fui.py inspect wobble.fui                # decode a .fui in NINEIGHT terms
python3 scripts/test_nin8fui.py                              # tests
```

Quote share links (`#` and `&` mean something to a shell). File numbers match nin8's preset
menu, so `14_Manbow bass.fui` is the 14th preset (Up/Down keys). In Furnace use
**Instruments → Open** (multi-select; clicking a file previews it); right-click → *Open →
replace* refreshes an instrument in place and keeps the slot used by your patterns.

To sound like nin8, set Furnace up like this:

- **Settings → Emulation → Playback and Render core(s) → YM2608: "ymfm only".** Furnace's
  default is a Nuked-OPN2 + ymfm hybrid that is close but measurably different on some patches.
- Chip clock **8 MHz** (the plain "Yamaha YM2608" system; the PC-98 presets use 7.987 MHz, about 3 cents flat).
- Play notes at **full volume**: the instrument uses Furnace's automatic TL volume scaling, so
  quieter notes attenuate the carriers (nin8 has no velocity).

All 48 voice fields travel (algorithm, feedback, AMS, PMS, 4 × 11 operator fields), plus the LFO
speed as an instrument macro when the patch uses the LFO. Pan, master gain and polyphony are not
instrument data in Furnace. Furnace has a single chip-wide LFO, so instruments with different LFO
speeds cannot play together.

Getting *your own* edits out of the plugin is not possible yet (the embedded WebKit has no download
handler, and nin8's engine only picks up the UI state on the first key press). Today: the bundled
presets, a share link from the web app, or hand-written preset JSON. A native auto-export folder
is the planned next step.

The bundled presets carry no origin or licence information. Check where a preset came from
before shipping it in a game.

## Build (macOS — VST3 + AU + Standalone)

Requires Xcode (or Command Line Tools) and CMake ≥ 3.22. The `FORMATS` line i
CMakeLists adds AU automatically on Darwin. The UI uses the system WKWebView —
no extra dependencies.

```sh
# clone with submodules (JUCE)
git clone --recurse-submodules <repo-url> && cd nin8

# universal binary (Apple Silicon + Intel)
cmake -B build -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_OSX_ARCHITECTURES="arm64;x86_64"
cmake --build build --parallel
```

Outputs: `build/Nineight_artefacts/Release/{VST3,AU,Standalone}`; JUCE copies
them to `~/Library/Audio/Plug-Ins/VST3` and `~/Library/Audio/Plug-Ins/Components`.
The AU only shows up in Logic/GarageBand after a plugin rescan; validate with:

```sh
auval -v aumu Opna Nin8
```

Note: unsigned builds may need Gatekeeper quarantine removed on other machines
(`xattr -dr com.apple.quarantine <plugin>`), or proper codesign/notarization
for distribution.

## Build (Windows — VST3 + Standalone)

Requires Visual Studio 2022 (Desktop C++ workload), CMake, and git (the
configure step applies the JUCE webview patch with `git apply`).

```bat
git clone --recurse-submodules <repo-url>
cd nin8
cmake -B build -G "Visual Studio 17 2022" -A x64
cmake --build build --config Release --parallel
```

Outputs: `build/Nineight_artefacts/Release/{VST3,Standalone}`; the VST3 is
copied to `%COMMONPROGRAMFILES%\VST3` (may need an elevated shell, or set
`COPY_PLUGIN_AFTER_BUILD FALSE` and copy manually).

The UI needs the **WebView2 Evergreen Runtime** — preinstalled on Windows 11
and most updated Windows 10 machines; otherwise install it from the
[WebView2 download page](https://developer.microsoft.com/microsoft-edge/webview2/).
Without it the editor comes up blank (audio still works).

Note: the Linux-only pieces (webview patch behavior, `HostEnvSanitizer`) are
no-ops on macOS/Windows; the same `ui/webui.zip` is embedded on all platforms.

## Offline audio check

```sh
clang++-12 -O2 -std=c++17 -I third_party/ymfm/src -o build/render_test \
  tools/render_test.cpp third_party/ymfm/src/ymfm_{opn,adpcm,ssg}.cpp
build/render_test assets/ym2608_rhythm_rom.bin /tmp/c4.wav   # renders piano C4, fails if silent
```

## Bridge protocol

| JS → C++                               | Purpose                                                                                                                                                      |
| -------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `synthReady()` (native fn, round-trip) | returns `{sampleRate, nativeRate, patch}` on UI boot                                                                                                         |
| `uiEvent` (one-way `emitEvent`)        | all hot-path traffic: `{cmd: noteOn/noteOff/allNotesOff/setParam/setOpEnvelope/post, …}` — no response roundtrip, so knob drags can't flood the message loop |

Events (C++ → JS, 30 Hz while editor visible): `paramChanged`, `noteOn`/`noteOff`
(host MIDI → key highlight), `waveform` (2048-byte AnalyserNode-format scope data).

## Status / TODO

- [x] Native engine, MIDI, params, state, WebView UI (verified in Standalone on Linux)
- [ ] Add SSG scene / rhythm pads
- [ ] Export instruments presets from PC-98 games
- [ ] Scene adopts DAW-restored patch on editor open (currently boots with default preset)
- [ ] Mono-mode portamento glide for host MIDI

Licenses: ymfm BSD-3 (Aaron Giles), JUCE 8 (GPLv3/commercial), UI assets from the opna repo.
