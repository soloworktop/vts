#!/usr/bin/env bash
# VTS source build, derived from the pinned recipe recorded in binaries.json.
# Sources MUST have been verified by build_desktop.py before this is called.
set -euo pipefail
SRC=$(cd "$1" && pwd)
OUT=$2
TARGET=$3
mkdir -p "$OUT"
OUT=$(cd "$OUT" && pwd)
WORK=$OUT/work
mkdir -p "$WORK/prefix"
PREFIX=$WORK/prefix
JOBS=${VTS_BUILD_JOBS:-6}
unset CPATH C_INCLUDE_PATH CPLUS_INCLUDE_PATH LIBRARY_PATH CFLAGS CPPFLAGS LDFLAGS PKG_CONFIG_PATH || true
export PKG_CONFIG_LIBDIR="$PREFIX/lib/pkgconfig"
FF_FLAGS=()
ARCH_FLAGS=""
LINK_FLAGS=""
case "$TARGET" in
  macos-arm64)
    test "$(uname -m)" = arm64
    SDK=$(xcrun --show-sdk-path)
    export MACOSX_DEPLOYMENT_TARGET=14.0
    export SDKROOT="$SDK"
    CC="$(xcrun --find clang)"
    ARCH_FLAGS="-arch arm64 -isysroot $SDK"
    LINK_FLAGS="-Wl,-syslibroot,$SDK"
    HOST=aarch64-apple-darwin
    FF_FLAGS=(--enable-securetransport --cc="$CC" --host-cflags="$ARCH_FLAGS" --host-ldflags="$ARCH_FLAGS $LINK_FLAGS")
    EXE=""
    ;;
  windows-x64)
    CC=gcc
    HOST=x86_64-w64-mingw32
    LINK_FLAGS="-static"
    FF_FLAGS=(--target-os=mingw32 --arch=x86_64 --enable-schannel --cc=gcc)
    EXE=.exe
    ;;
  *) echo "unsupported desktop target" >&2; exit 1 ;;
esac
export CC
# Build zlib statically even on macOS, so developer package-manager paths cannot leak.
tar -xzf "$SRC/zlib-1.3.2.tar.gz" -C "$WORK"
(
 cd "$WORK/zlib-1.3.2"
 if [ "$TARGET" = windows-x64 ]; then
   make -f win32/Makefile.gcc -j"$JOBS" libz.a
   mkdir -p "$PREFIX/lib" "$PREFIX/include"
   cp libz.a "$PREFIX/lib/"
   cp zlib.h zconf.h "$PREFIX/include/"
 else
   CFLAGS="-O2 $ARCH_FLAGS" ./configure --static --prefix="$PREFIX"
   make -j"$JOBS" install
 fi
)
tar -xzf "$SRC/lame-3.100.tar.gz" -C "$WORK"
(
 cd "$WORK/lame-3.100"
 CFLAGS="-O2 $ARCH_FLAGS -Wno-implicit-function-declaration" ac_cv_prog_cc_c23=no \
 ./configure --prefix="$PREFIX" --host="$HOST" --build="$HOST" --disable-shared --enable-static \
 --disable-frontend --disable-decoder --disable-gtktest --disable-dependency-tracking
 make -j"$JOBS" install
)
tar -xJf "$SRC/ffmpeg-9.0.2.tar.xz" -C "$WORK"
(
 cd "$WORK/ffmpeg-9.0.2"
 ./configure "${FF_FLAGS[@]}" --prefix="$PREFIX" \
   --extra-cflags="-I$PREFIX/include $ARCH_FLAGS" --extra-ldflags="-L$PREFIX/lib $ARCH_FLAGS $LINK_FLAGS" \
   --pkg-config-flags=--static --extra-version=vts --disable-autodetect --disable-shared --enable-static \
   --disable-doc --disable-ffplay --disable-debug --enable-zlib --enable-libmp3lame
 make -j"$JOBS"
 cp "ffmpeg$EXE" "ffprobe$EXE" "$OUT/"
 {
  "$CC" --version | head -1
  echo "FFmpeg 9.0.2; LAME 3.100; zlib 1.3.2; target=$TARGET"
  sed -n 's/^#define FFMPEG_CONFIGURATION "\(.*\)"$/\1/p' config.h
 } > "$OUT/buildinfo.txt"
)
