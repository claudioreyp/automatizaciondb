#!/bin/sh
set -eu
qr_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
qr_node=${IMPULSA_NODE_BIN:-node}
if [ "${1:-}" = "--check-runtime" ]; then
  [ "$#" -eq 1 ] || { echo 'Unexpected runtime-check arguments' >&2; exit 2; }
  exec "$qr_node" "$qr_root/dist/qr-direct.js" --check-runtime
fi
[ "$#" -eq 0 ] || { echo 'Unexpected QR runner arguments' >&2; exit 2; }
qr_session=${IMPULSA_QR_SESSION_DIR:-/var/lib/impulsa/session}
case "$qr_session" in /*) ;; *) echo 'Session directory must be absolute' >&2; exit 2;; esac
set -- "$qr_root/dist/qr-direct.js" "$qr_session"
if [ -n "${IMPULSA_NATIVE_PROFILE:-}" ]; then
  case "$IMPULSA_NATIVE_PROFILE" in /*) ;; *) echo 'Profile file must be absolute' >&2; exit 2;; esac
  set -- "$@" "$IMPULSA_NATIVE_PROFILE"
fi
case "${IMPULSA_NATIVE_ACTIVE:-false}" in
  false) ;;
  true) [ -n "${IMPULSA_NATIVE_PROFILE:-}" ] || { echo 'Activation requires a profile file' >&2; exit 2; }; set -- "$@" --activate-native;;
  *) echo 'IMPULSA_NATIVE_ACTIVE must be true or false' >&2; exit 2;;
esac
exec "$qr_node" "$@"
