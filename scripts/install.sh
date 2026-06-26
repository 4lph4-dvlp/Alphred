#!/usr/bin/env bash
# Alphred installer for macOS / Linux.
#
#   - Verifies Python 3.11+.
#   - Installs Alphred (editable) into the user site.
#   - Adds the user bin dir to PATH (in your shell rc) so the `alphred` command works.
#
# Hermes Agent must already be installed (Alphred is a wrapper over it).
#
# Usage:
#   bash scripts/install.sh [--no-path] [--python <exe>]
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY=""
NO_PATH=0
while [ $# -gt 0 ]; do
  case "$1" in
    --no-path)  NO_PATH=1 ;;
    --python)   PY="${2:-}"; shift ;;
    -h|--help)  grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

c_info() { printf '\033[36m%s\033[0m\n' "$1"; }
c_ok()   { printf '\033[32m  %s\033[0m\n' "$1"; }
c_warn() { printf '\033[33m  %s\033[0m\n' "$1"; }

c_info "Alphred installer (macOS / Linux)"
echo "  repo: $REPO"

# ---- 1) Resolve a Python 3.11+ interpreter ----
PYBIN=""
for c in "$PY" python3 python; do
  [ -z "$c" ] && continue
  command -v "$c" >/dev/null 2>&1 || continue
  v="$("$c" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null)" || continue
  major="${v%.*}"; minor="${v#*.}"
  if [ "$major" -eq 3 ] && [ "$minor" -ge 11 ]; then PYBIN="$c"; break; fi
done
[ -z "$PYBIN" ] && { echo "Python 3.11+ not found. Install it and retry." >&2; exit 1; }
c_ok "Python: $PYBIN ($("$PYBIN" -c 'import sys;print(sys.version.split()[0])'))"

# ---- 2) Install Alphred (editable) ----
c_info "Installing Alphred (pip install -e .) ..."
( cd "$REPO" && "$PYBIN" -m pip install -e . )
c_ok "installed"

# ---- 3) Ensure the console-script dir is on PATH ----
BIN_DIR="$("$PYBIN" - <<'PY'
import sysconfig, os
names = ['alphred']
dirs = []
for args in ((), ('posix_user',)):
    try: dirs.append(sysconfig.get_path('scripts', *args))
    except Exception: pass
for d in dirs:
    if any(os.path.exists(os.path.join(d, n)) for n in names):
        print(d); break
else:
    print(dirs[-1] if dirs else '')
PY
)"
if [ -n "$BIN_DIR" ] && [ -d "$BIN_DIR" ]; then
  case ":$PATH:" in
    *":$BIN_DIR:"*) c_ok "PATH already contains $BIN_DIR" ;;
    *)
      if [ "$NO_PATH" -eq 1 ]; then
        c_warn "PATH not modified (--no-path). Use 'python -m alphred.cli' or add: $BIN_DIR"
      else
        rc="$HOME/.profile"
        case "${SHELL:-}" in
          *zsh)  rc="$HOME/.zshrc" ;;
          *bash) rc="$HOME/.bashrc" ;;
        esac
        if grep -q "alphred installer" "$rc" 2>/dev/null; then
          c_ok "PATH entry already present in $rc"
        else
          printf '\n# added by alphred installer\nexport PATH="$PATH:%s"\n' "$BIN_DIR" >> "$rc"
          c_ok "added to PATH in $rc (open a NEW terminal, or: source $rc)"
        fi
        export PATH="$PATH:$BIN_DIR"  # current session
      fi
      ;;
  esac
else
  c_warn "could not locate the alphred bin dir; use 'python -m alphred.cli'"
fi

# ---- 4) Hermes presence check (informational) ----
HERMES_BIN="$("$PYBIN" -c 'from alphred.config import resolve_hermes_bin; print(resolve_hermes_bin() or "")' 2>/dev/null || true)"
if [ -z "$HERMES_BIN" ]; then
  c_warn "Hermes not found — install Hermes Agent, then run 'alphred setup'. (Alphred wraps Hermes.)"
fi

echo
c_info "Done. Next steps:"
echo "  alphred setup     # configure LLM provider (Hermes onboarding), if not done yet"
echo "  alphred           # start the queue-aware TUI"
echo "  alphred serve     # or run the gateway + web dashboard (http://localhost:8643/)"
echo "  (if 'alphred' is still not found, open a new terminal, or use 'python -m alphred.cli')"
