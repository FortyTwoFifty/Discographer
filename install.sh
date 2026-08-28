#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"

need_cmds=(python3)
sys_pkgs_arch=(python python-yaml python-pip cdparanoia flac libcdio eject coreutils)
sys_pkgs_deb=(python3 python3-yaml python3-pip python3-venv cdparanoia flac libcdio-utils eject coreutils)
sys_pkgs_rpm=(python3 python3-pyyaml python3-pip cdparanoia flac libcdio eject coreutils)

have() { command -v "$1" >/dev/null 2>&1; }

install_sys() {
  if have pacman; then
    sudo pacman -S --needed --noconfirm "${sys_pkgs_arch[@]}"
  elif have apt-get; then
    sudo apt-get update -y
    sudo apt-get install -y "${sys_pkgs_deb[@]}"
  elif have dnf; then
    sudo dnf install -y "${sys_pkgs_rpm[@]}"
  else
    echo "Install these yourself: python3, PyYAML, cdparanoia, flac, cd-info (libcdio), eject, stdbuf (coreutils)" >&2
  fi
}

install_sys

for c in python3 cdparanoia flac metaflac cd-info eject stdbuf; do
  if ! have "$c"; then
    echo "missing command: $c" >&2
    exit 1
  fi
done

if ! python3 -m pip install --user -e "$ROOT"; then
  python3 -m pip install --user --break-system-packages -e "$ROOT" || echo "pip skipped; using $ROOT/scripts/discographer"
fi

CFG="${XDG_CONFIG_HOME:-$HOME/.config}/discographer"
mkdir -p "$CFG" "$HOME/.cache/discographer" "$HOME/.local/bin"
if [[ ! -f "$CFG/catalog.yaml" && ! -f "$ROOT/catalog.yaml" ]]; then
  cp "$ROOT/catalog.example.yaml" "$CFG/catalog.yaml"
  echo "wrote $CFG/catalog.yaml  — edit library/ and jobs:"
fi

chmod +x "$ROOT/scripts/discographer" "$ROOT/install.sh"
ln -sfn "$ROOT/scripts/discographer" "$HOME/.local/bin/discographer"
if have discographer; then
  echo "installed: $(command -v discographer)"
else
  echo "add ~/.local/bin to PATH"
fi
PYTHONPATH="$ROOT" python3 -m discographer --help >/dev/null
echo "ok  discographer is ready"
