#!/usr/bin/env bash
# Install kvm-switcher onto PATH. Uses this checkout when the script is next
# to kvm-switcher.py, otherwise the latest GitHub release.
set -euo pipefail

repo="algun/kvm-switcher"
dest="${HOME}/.local/share/kvm-switcher"
bindir="${HOME}/.local/bin"
here="$(cd "$(dirname "$0")" && pwd)"

if [[ -f "${here}/kvm-switcher.py" ]]; then
  src="$here"
else
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  curl -fsSL -L "https://github.com/${repo}/releases/latest/download/kvm-switcher.tar.gz" \
    -o "${tmp}/kvm-switcher.tar.gz"
  tar -C "$tmp" -xzf "${tmp}/kvm-switcher.tar.gz"
  src="${tmp}/kvm-switcher"
fi

mkdir -p "$dest" "$bindir"
cp "${src}/kvm-switcher.py" "${src}/config.json" "$dest/"
shopt -s nullglob
cp "${src}"/*.sh "${src}"/*.ps1 "$dest/"
chmod +x "${dest}/kvm-switcher.py" "${dest}"/*.sh
ln -sfn "${dest}/kvm-switcher.py" "${bindir}/kvm-switcher"

echo "Installed ${bindir}/kvm-switcher"
case ":${PATH}:" in
  *":${bindir}:"*) ;;
  *) echo "Add ${bindir} to PATH to run kvm-switcher." ;;
esac
echo "Then: kvm-switcher configure"
echo "Login watcher: kvm-switcher install"
