##
## NINEIGHT standalone, containerised for a Linux desktop (X11 / XWayland + PulseAudio/PipeWire).
##
##   docker compose up --build -d
##
## Three stages: JUCE source -> build -> runtime. Only the Standalone target is
## built (a VST3 inside a container is no use to a host DAW).
##

ARG ALPINE_VERSION=3.24

# ---------------------------------------------------------------------------
# JUCE, fetched at the commit the third_party/JUCE submodule is pinned to
# (`git ls-tree HEAD third_party/JUCE`), so the build works from a clone that
# never ran `git submodule update --init`. Keep JUCE_COMMIT in sync if the
# submodule is bumped.
# ---------------------------------------------------------------------------
FROM alpine:${ALPINE_VERSION} AS juce
ARG JUCE_COMMIT=2cdfca8feb300fb424002ba2c2751569e5bacb64
RUN apk add --no-cache git \
 && git init -q /juce \
 && git -C /juce remote add origin https://github.com/juce-framework/JUCE.git \
 && git -C /juce fetch -q --depth 1 origin "${JUCE_COMMIT}" \
 && git -C /juce checkout -q FETCH_HEAD

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
FROM alpine:${ALPINE_VERSION} AS build

# -dev packages cover every system header JUCE includes on Linux: X11 (+ Xcursor,
# Xext/XShm, Xcomposite, Xinerama, Xrandr, Xrender), ALSA, freetype, fontconfig
# and gtk3 / libsoup3 / webkit2gtk-4.1 for the WebBrowserComponent. `git` is
# needed because CMakeLists.txt applies the JUCE webview patch with `git apply`.
# (libcurl is not needed: JUCE_USE_CURL=0.)
RUN apk add --no-cache \
      build-base cmake samurai git pkgconf linux-headers \
      alsa-lib-dev fontconfig-dev freetype-dev \
      libx11-dev libxcomposite-dev libxcursor-dev libxext-dev \
      libxinerama-dev libxrandr-dev libxrender-dev \
      gtk+3.0-dev libsoup3-dev webkit2gtk-4.1-dev

WORKDIR /src

# Least-changing inputs first so the (slow) JUCE configure/compile layers cache well.
COPY --from=juce /juce third_party/JUCE
COPY CMakeLists.txt ./
COPY patches patches
COPY third_party/ymfm third_party/ymfm
COPY assets assets
COPY ui ui
COPY src src

# 0 = use every core. Lower it (e.g. NIN8_BUILD_JOBS=4) if the LTO build runs out of RAM.
ARG BUILD_JOBS=0
RUN jobs="${BUILD_JOBS}"; [ "${jobs}" -gt 0 ] 2>/dev/null || jobs="$(nproc)"; \
    cmake -B build -G Ninja -DCMAKE_BUILD_TYPE=Release \
 && cmake --build build --target Nineight_Standalone --parallel "${jobs}" \
 && strip --strip-unneeded build/Nineight_artefacts/Release/Standalone/NINEIGHT

# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
FROM alpine:${ALPINE_VERSION} AS runtime

# libstdc++/libgcc/libatomic : C++ runtime the binary links against
# alsa-lib + alsa-plugins-pulse : JUCE's only audio backend is ALSA; the pulse
#     plugin (which also installs the "default -> pulse" ALSA config) routes it
#     to the host's PipeWire/PulseAudio socket. No /dev/snd needed for audio.
# libx* : JUCE dlopen()s these by soname at runtime (they are not NEEDED entries)
# gtk+3.0 webkit2gtk-4.1 libsoup3 : JUCE dlopen()s these for the embedded UI
RUN apk add --no-cache \
      tini \
      libstdc++ libgcc libatomic \
      alsa-lib alsa-plugins-pulse \
      fontconfig freetype font-dejavu \
      libx11 libxext libxcursor libxinerama libxrandr libxrender \
      gtk+3.0 libsoup3 webkit2gtk-4.1

# JUCE dlopen()s the *unversioned* names (libwebkit2gtk-4.1.so, libgtk-3.so, ...),
# which Alpine only ships in the huge -dev packages. Add the symlinks instead.
RUN set -e; \
    for lib in webkit2gtk-4.1 javascriptcoregtk-4.1 soup-3.0 gtk-3 glib-2.0; do \
      [ -e "/usr/lib/lib${lib}.so.0" ] || { echo "missing /usr/lib/lib${lib}.so.0" >&2; exit 1; }; \
      ln -sf "lib${lib}.so.0" "/usr/lib/lib${lib}.so"; \
    done; \
    [ -e /etc/alsa/conf.d/99-pulseaudio-default.conf ] \
      || { echo "alsa-plugins-pulse did not install its default-device config" >&2; exit 1; }

# Unprivileged user. Compose runs the container as the *host* UID/GID (needed for
# PulseAudio socket auth and X cookie access), which may differ from 1000, so
# HOME is world-writable (sticky) rather than owned by one uid.
RUN adduser -D -u 1000 -h /home/nin8 -s /sbin/nologin nin8 \
 && install -d -m 1777 /home/nin8 /home/nin8/.config /home/nin8/.cache /home/nin8/.local /home/nin8/.local/share

COPY --from=build /src/build/Nineight_artefacts/Release/Standalone/NINEIGHT /usr/local/bin/nin8
COPY docker/entrypoint.sh /usr/local/bin/nin8-entrypoint

ENV HOME=/home/nin8 \
    XDG_RUNTIME_DIR=/tmp/xdg-runtime \
    # JUCE forces the GTK/WebKit child onto X11 (XEmbed); native Wayland can't be embedded.
    GDK_BACKEND=x11 \
    # WebKitGTK sandboxes its web process with bubblewrap, which needs user
    # namespaces that Docker's default seccomp profile denies. The container is
    # the sandbox; without this the editor stays blank.
    WEBKIT_DISABLE_SANDBOX_THIS_IS_DANGEROUS=1 \
    # No GPU / render node in the container. (JUCE already pins WebKit to software
    # rendering; these just stop WebKit probing for GBM/DMABUF.)
    WEBKIT_DISABLE_COMPOSITING_MODE=1 \
    WEBKIT_DISABLE_DMABUF_RENDERER=1 \
    # No session bus / dconf in here; silence the warnings.
    NO_AT_BRIDGE=1 \
    GSETTINGS_BACKEND=memory

USER nin8
WORKDIR /home/nin8

LABEL org.opencontainers.image.title="NINEIGHT" \
      org.opencontainers.image.description="NINEIGHT OPNA (YM2608) FM synth - standalone, for a Linux desktop"

ENTRYPOINT ["/sbin/tini", "--", "/usr/local/bin/nin8-entrypoint"]
CMD ["/usr/local/bin/nin8"]
