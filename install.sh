#!/usr/bin/env bash
# Install kvm-switcher onto PATH. Uses this checkout when the script is next
# to kvm-switcher.py, otherwise the latest GitHub release.
set -euo pipefail

repo="algun/kvm-switcher"
ref="main"
dest="${HOME}/.local/share/kvm-switcher"
bindir="${HOME}/.local/bin"
here="$(cd "$(dirname "$0")" && pwd)"

if [[ -f "${here}/kvm-switcher.py" ]]; then
  src="$here"
else
  # Piped from a URL, so there is no checkout to copy from. Keep $ref in step
  # with install.ps1 so both one-liners install the same code.
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  mkdir -p "${tmp}/kvm-switcher"
  for name in kvm-switcher.py config.json; do
    curl -fsSL -L "https://raw.githubusercontent.com/${repo}/${ref}/${name}" \
      -o "${tmp}/kvm-switcher/${name}"
  done
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
