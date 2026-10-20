#!/bin/sh
# Reproducible build of the on-thermostat API daemon (ARMv7 musl, static).
# Usage: daemon/build.sh [--check]   (--check: fail if the bundled binary differs)
set -eu
cd "$(dirname "$0")"
mkdir -p build
zig cc -target arm-linux-musleabihf -static -Os -Wall -o build/thermostat_api thermostat_api.c
BUNDLED=../custom_components/aciq_thermostat/bin/thermostat_api
if [ "${1:-}" = "--check" ]; then
  a=$(sha256sum build/thermostat_api | cut -d' ' -f1); b=$(sha256sum "$BUNDLED" | cut -d' ' -f1)
  echo "built:   $a"; echo "bundled: $b"
  [ "$a" = "$b" ] || { echo "bundled daemon does not match source build" >&2; exit 1; }
else
  cp build/thermostat_api "$BUNDLED"
  (cd "$(dirname "$BUNDLED")" && sha256sum thermostat_api > thermostat_api.sha256)
  cat "$(dirname "$BUNDLED")/thermostat_api.sha256"
fi
