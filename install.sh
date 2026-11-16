#!/usr/bin/env bash
# Install kvm-switcher onto PATH. Uses this checkout when it has a built
# dist/kvm-switcher.pyz, otherwise the latest GitHub release asset.
#
# The program is a single-file zipapp, so nothing is unpacked and update and
# uninstall keep working on one file.
#
# Python is a real prerequisite, so it is checked before anything is written. A
# machine without it used to get a launcher that failed on every use.
set -euo pipefail

repo="algun/kvm-switcher"
program="kvm-switcher.pyz"
release_url="https://github.com/${repo}/releases/latest/download"
min_major=3
min_minor=9
dest="${HOME}/.local/share/kvm-switcher"
bindir="${HOME}/.local/bin"
here="$(cd "$(dirname "$0")" && pwd)"

guidance() {
  cat >&2 <<EOF

kvm-switcher needs Python ${min_major}.${min_minor} or newer.
Install it, then run this installer again:

  Debian/Ubuntu:  sudo apt install python3
  Fedora:         sudo dnf install python3
  macOS:          brew install python@3.12

EOF
}

# The reported "major.minor", from actually running the interpreter. A name in
# PATH proves nothing: on macOS a stub can exist without a usable interpreter.
probe_version() {
  "$@" -c 'import sys; sys.stdout.write("%d.%d" % sys.version_info[:2])' 2>/dev/null
}

python_cmd=""
too_old=""
# python3 first: on a system that still has Python 2, `python` is not it.
for candidate in python3 python; do
  command -v "$candidate" >/dev/null 2>&1 || continue
  version="$(probe_version "$candidate" || true)"
  [[ "$version" =~ ^[0-9]+\.[0-9]+$ ]] || continue
  major="${version%%.*}"
  minor="${version##*.}"
  if (( major > min_major || (major == min_major && minor >= min_minor) )); then
    python_cmd="$candidate"
    break
  fi
  too_old="${too_old}${too_old:+, }${candidate} (${version})"
done

if [[ -z "$python_cmd" ]]; then
  if [[ -n "$too_old" ]]; then
    echo "Found Python, but it is too old: ${too_old}" >&2
  fi
  guidance
  exit 1
fi

echo "Using $(command -v "$python_cmd") ($("$python_cmd" -c 'import sys; print(sys.version.split()[0])'))"

if [[ -f "${here}/${program}" ]]; then
  # An extracted release archive, where the program sits beside this script.
  src="${here}"
elif [[ -f "${here}/dist/${program}" ]]; then
  # A source checkout, where the build writes into dist/.
  src="${here}/dist"
else
  # Piped from a URL, so there is no checkout to copy from. Everything comes from
  # the newest release, so what you run is what was tagged and tested: no branch
  # is consulted, and a commit pushed to main cannot reach you before a release.
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  mkdir -p "${tmp}/kvm-switcher"
  curl -fsSL -L "${release_url}/${program}" \
    -o "${tmp}/kvm-switcher/${program}"
  # The seed config is optional: a missing one only means `configure` asks.
  curl -fsSL -L "${release_url}/config.json" \
    -o "${tmp}/kvm-switcher/config.json" || rm -f "${tmp}/kvm-switcher/config.json"
  src="${tmp}/kvm-switcher"
fi

if [[ ! -f "${src}/${program}" ]]; then
  echo "No ${program} to install; build it with: python build.py" >&2
  exit 1
fi

# Refuse to keep a payload that is not the program: a captive portal or a proxy
# error page would otherwise be installed as a working-looking file. Running it
# is the real check.
if ! reported="$("$python_cmd" "${src}/${program}" version 2>/dev/null)"; then
  echo "${program} does not run; nothing installed." >&2
  exit 1
fi
if [[ ! "$reported" =~ ^[0-9]+\.[0-9] ]]; then
  echo "${program} does not report a version; nothing installed." >&2
  exit 1
fi

mkdir -p "$dest" "$bindir"
cp "${src}/${program}" "$dest/${program}"
if [[ -f "${src}/config.json" ]]; then
  cp "${src}/config.json" "$dest/config.json"
fi
shopt -s nullglob
cp "${src}"/*.sh "${src}"/*.ps1 "$dest/" 2>/dev/null || true
chmod +x "${dest}/${program}" "${dest}"/*.sh 2>/dev/null || true

# A pre-package install left kvm-switcher.py here, and the old launcher pointed
# at it. Removing it is the migration: anything that still names the old file
# now fails loudly instead of running a stale copy this installer did not update.
migrated=0
if [[ -f "${dest}/kvm-switcher.py" ]]; then
  rm -f "${dest}/kvm-switcher.py"
  migrated=1
  echo "Removed the old single-file program (${dest}/kvm-switcher.py)"
fi

# The launcher names the interpreter the gate verified, by absolute path, so a
# later PATH change cannot point it at a different or missing Python. The name
# stays as a fallback in case that path is removed later (a dropped Homebrew
# version, for example). The .pyz promotes its own staged updates at start-up,
# so this script needs no .new handling.
python_path="$(command -v "$python_cmd")"
cat > "${bindir}/kvm-switcher" <<EOF
#!/bin/sh
# Written by install.sh. Uses the interpreter it verified, by path, and falls
# back to the name if that path has since gone.
interpreter='${python_path}'
[ -x "\$interpreter" ] || interpreter='${python_cmd}'
exec "\$interpreter" '${dest}/${program}' "\$@"
EOF
chmod +x "${bindir}/kvm-switcher"

echo "Installed ${bindir}/kvm-switcher (version ${reported})"
case ":${PATH}:" in
  *":${bindir}:"*) ;;
  *) echo "Add ${bindir} to PATH to run kvm-switcher." ;;
esac
echo "Then: kvm-switcher configure"
if (( migrated )); then
  # The old login entry names the .py that was just removed, so it is now
  # wrong. Registering a service is not this installer's job, so say what is
  # needed rather than doing it behind the user's back.
  echo "Your login watcher still points at the old program."
  echo "Re-register it now: kvm-switcher install"
else
  echo "Login watcher: kvm-switcher install"
fi
