#!/data/data/com.termux/files/usr/bin/bash
# install_termux.sh - one-shot installer for opencode_py on Termux.
#
# Works on 64-bit (aarch64) and 32-bit (armv7 / armv8l) Android phones.
# It installs the system toolchain, builds curl_cffi (the TLS fingerprint
# runtime the anonymous OpenCode Zen free models require), installs the app
# and prints the one command needed to start.
#
# Usage, from the repo checkout:
#   bash install_termux.sh
#
# Or from anywhere (clones the repo first):
#   REPO_URL=https://github.com/<you>/opencode_py.git bash install_termux.sh
#
# Re-running is safe: an existing checkout is updated and the app reinstalled.

set -euo pipefail

step() { printf '\n==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"

# ---------------------------------------------------------------------------
# 1. Architecture detection.
#
# 32-bit Termux on a 64-bit kernel reports `armv8l`, which curl_cffi's build
# does not recognise. Force the armv7 target so it downloads the prebuilt
# linux-android armv7 libcurl-impersonate instead of aborting.
# ---------------------------------------------------------------------------
step "Detecting architecture"
ARCH="$(uname -m)"
case "$ARCH" in
  armv7l|armv8l|armv7*|armv8*)
    export CURL_CFFI_TARGET_MACHINE=armv7l
    export CURL_CFFI_TARGET_POINTER_SIZE=32
    ;;
  aarch64|arm64)
    export CURL_CFFI_TARGET_MACHINE=aarch64
    export CURL_CFFI_TARGET_POINTER_SIZE=64
    ;;
  *)
    warn "unknown architecture '$ARCH'; letting curl_cffi auto-detect"
    ;;
esac
# curl_cffi's build switches to its Android branch when TERMUX_VERSION is set.
export TERMUX_VERSION="${TERMUX_VERSION:-termux}"
printf '    arch=%s target=%s/%s\n' "$ARCH" \
  "${CURL_CFFI_TARGET_MACHINE:-auto}" "${CURL_CFFI_TARGET_POINTER_SIZE:-auto}"

# ---------------------------------------------------------------------------
# 2. System packages. clang + libc++ + libffi + pkg-config are what a source
#    build of curl_cffi/cffi needs on Termux.
# ---------------------------------------------------------------------------
if command -v pkg >/dev/null 2>&1; then
  step "Updating package lists"
  pkg update -y || warn "pkg update failed; continuing with the current lists"

  step "Installing system packages (this can take a few minutes)"
  pkg install -y python git ripgrep openssh \
                 clang make binutils libc++ libffi pkg-config
  # Android platform tools are only needed by the optional browser tool.
  pkg install -y android-tools \
    || warn "android-tools not installed; the optional browser tool will be unavailable"
else
  warn "'pkg' not found - not running in Termux? Skipping system packages."
fi

# ---------------------------------------------------------------------------
# 3. Locate or clone the source tree.
# ---------------------------------------------------------------------------
if [ -f "$SCRIPT_DIR/pyproject.toml" ] && [ -d "$SCRIPT_DIR/opencode_py" ]; then
  INSTALL_DIR="$SCRIPT_DIR"
  step "Using existing checkout: $INSTALL_DIR"
else
  REPO_URL="${REPO_URL:-https://github.com/anomalyco/opencode_py.git}"
  INSTALL_DIR="${INSTALL_DIR:-$HOME/opencode_py}"
  if [ -d "$INSTALL_DIR/.git" ]; then
    step "Updating existing checkout: $INSTALL_DIR"
    git -C "$INSTALL_DIR" pull --ff-only \
      || warn "git pull failed; using the checkout as-is"
  else
    step "Cloning $REPO_URL"
    git clone "$REPO_URL" "$INSTALL_DIR"
  fi
fi
cd "$INSTALL_DIR"

# ---------------------------------------------------------------------------
# 4. Python packaging tools, then curl_cffi.
#
# On Termux there is no installable curl_cffi wheel (PyPI's armv7 wheel is
# glibc, Termux is Bionic), so pip builds from source and fetches curl_cffi's
# prebuilt libcurl-impersonate for linux-android armv7/arm64. The retry without
# build isolation covers pip environments where the isolated build can't see
# the Termux toolchain.
# ---------------------------------------------------------------------------
step "Upgrading pip and build tooling"
python -m pip install --upgrade pip setuptools wheel \
  || warn "could not upgrade pip/setuptools/wheel; continuing with the current ones"

step "Installing cffi"
python -m pip install --upgrade "cffi>=2.1.1" \
  || python -m pip install --upgrade --no-build-isolation "cffi>=2.1.1"

step "Building curl_cffi (TLS fingerprint runtime; downloads ~15 MB)"
if ! python -m pip install --upgrade "curl_cffi>=0.7,<0.18"; then
  warn "standard build failed; retrying without build isolation"
  python -m pip install --upgrade --no-build-isolation "curl_cffi>=0.7,<0.18"
fi

# ---------------------------------------------------------------------------
# 5. Install the app itself.
# ---------------------------------------------------------------------------
step "Installing Python requirements"
python -m pip install -r requirements.txt

step "Installing opencode-py"
# Deps are already satisfied above; --force-reinstall makes re-runs pick up
# updated source code instead of silently keeping the old copy.
python -m pip install --force-reinstall --no-deps .

# ---------------------------------------------------------------------------
# 6. Verify: executable present, package importable, TLS runtime can take the
#    official JA3. (No network call here, so it never burns free quota.)
# ---------------------------------------------------------------------------
step "Verifying the installation"
command -v opencode-py >/dev/null 2>&1 \
  || { warn "opencode-py is not on PATH; check \$PATH"; exit 1; }

# Run from a neutral directory so this checks the installed copy in
# site-packages, not the source tree we are standing in.
(
  cd "${TMPDIR:-/tmp}"
  python - <<'PY'
import curl_cffi
from curl_cffi import requests as cr

import opencode_py
from opencode_py.providers.zen import BUN_JA3

session = cr.Session(ja3=BUN_JA3)
session.close()
print(f"    opencode_py: {opencode_py.__file__}")
print(f"    curl_cffi {curl_cffi.__version__}: official JA3 accepted")
PY
)

# Optional real request: proves key-less free-model access end to end.
# Skip with NO_LIVE_CHECK=1 (e.g. when the daily free quota is used up).
if [ "${NO_LIVE_CHECK:-0}" != "1" ]; then
  step "Live check against OpenCode Zen (free model)"
  if command -v timeout >/dev/null 2>&1; then
    timeout 120 opencode-py --check || warn "live check failed; see the message above"
  else
    opencode-py --check || warn "live check failed; see the message above"
  fi
fi

# ---------------------------------------------------------------------------
# 7. Done.
# ---------------------------------------------------------------------------
printf '\n'
printf '====================================================================\n'
printf ' Installation complete - ready to use.\n'
printf '====================================================================\n'
cat <<EOF

 Start it:
   opencode-py

 Free OpenCode Zen models work anonymously: no API key, no /connect.
 Default model: opencode/deepseek-v4.1-flash-free
   List free models:  opencode-py --models
   Headless one-shot: opencode-py --no-tui -m "hello"

 All free *chat* models accept tools. (jev-1.13-free is a decision-only
 model and is not usable for chat.)

 Free quota is per client on a rolling window. If you see
 "Free usage limit reached", switch to another free model or wait. The
 official opencode client gets exactly the same 429 at that point.

 Optional, for extra quota - keyed providers need no TLS fingerprint:
   /connect  ->  Groq / Cerebras / Google / OpenRouter / NVIDIA / Mistral / GitHub

 Low-RAM tips:
   - Prefer *-free / flash models
   - /clear or /new to trim a long session
   - If output looks stuck:  PYTHONUNBUFFERED=1 opencode-py

 Updating later:
   cd "$INSTALL_DIR" && git pull && bash install_termux.sh

 Troubleshooting: see the README's troubleshooting section.
EOF
