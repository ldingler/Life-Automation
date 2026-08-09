#!/usr/bin/env bash
#
# One command from a fresh clone to a working demo, on macOS.
#
#   ./scripts/bootstrap-mac.sh
#
# Design rule: DETECT, don't install. The only thing this will offer to install
# is `uv` (small, self-contained, and it asks first). It will never install
# Homebrew or a Python runtime on your behalf — those are large changes to make
# to someone's machine unasked. When something is missing it prints the exact
# command to run and stops.
#
# Written for bash 3.2, which is what macOS still ships. No associative arrays,
# no `mapfile`, no `${var,,}`.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [ -t 1 ]; then
  BOLD=$'\033[1m'; GREEN=$'\033[32m'; RED=$'\033[31m'
  YELLOW=$'\033[33m'; DIM=$'\033[2m'; RESET=$'\033[0m'
else
  BOLD=''; GREEN=''; RED=''; YELLOW=''; DIM=''; RESET=''
fi

step() { printf '\n%s==>%s %s%s\n' "$BOLD" "$RESET" "$1" "$RESET"; }
ok()   { printf '  %s✓%s %s\n' "$GREEN" "$RESET" "$1"; }
warn() { printf '  %s!%s %s\n' "$YELLOW" "$RESET" "$1"; }
die()  {
  printf '\n  %s✗ %s%s\n' "$RED" "$1" "$RESET"
  if [ $# -gt 1 ]; then printf '\n    %sFix:%s %s\n\n' "$BOLD" "$RESET" "$2"; fi
  exit 1
}

printf '%sNellis Deal Engine — macOS bootstrap%s\n' "$BOLD" "$RESET"
printf '%s%s%s\n' "$DIM" "$REPO_ROOT" "$RESET"

# ---------------------------------------------------------------- platform ---
step "Checking platform"
UNAME_S="$(uname -s)"
if [ "$UNAME_S" != "Darwin" ]; then
  warn "This script targets macOS; you're on $UNAME_S. Continuing anyway."
else
  ok "macOS $(sw_vers -productVersion 2>/dev/null || echo '?') ($(uname -m))"
fi

# ------------------------------------------------------------------ python ---
step "Looking for Python 3.11+"

# Checked in order. Homebrew lives in /opt/homebrew on Apple Silicon and
# /usr/local on Intel, and pyenv shims land in ~/.pyenv.
PYTHON_CANDIDATES="
python3.13
python3.12
python3.11
/opt/homebrew/bin/python3.13
/opt/homebrew/bin/python3.12
/opt/homebrew/bin/python3.11
/usr/local/bin/python3.13
/usr/local/bin/python3.12
/usr/local/bin/python3.11
$HOME/.pyenv/shims/python3
python3
"

PYTHON_BIN=""
FOUND_TOO_OLD=""
for candidate in $PYTHON_CANDIDATES; do
  if command -v "$candidate" >/dev/null 2>&1; then
    resolved="$(command -v "$candidate")"
    if "$resolved" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
      PYTHON_BIN="$resolved"
      break
    else
      version="$("$resolved" -c 'import sys; print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo '?')"
      FOUND_TOO_OLD="$resolved ($version)"
    fi
  fi
done

if [ -z "$PYTHON_BIN" ]; then
  detail="No Python 3.11 or newer found."
  if [ -n "$FOUND_TOO_OLD" ]; then
    detail="Only found $FOUND_TOO_OLD — macOS ships 3.9, which is too old."
  fi
  if command -v brew >/dev/null 2>&1; then
    die "$detail" "brew install python@3.11   then re-run this script"
  fi
  die "$detail" \
    "Install Homebrew from https://brew.sh then: brew install python@3.11
         (or install Python 3.11+ from https://www.python.org/downloads/macos/)"
fi
ok "$($PYTHON_BIN -V) at $PYTHON_BIN"

# --------------------------------------------------------------------- uv ----
step "Looking for uv"
if command -v uv >/dev/null 2>&1; then
  ok "uv $(uv --version 2>/dev/null | awk '{print $2}')"
else
  warn "uv is not installed. It's a fast Python package manager (~30MB, self-contained)."
  printf '    Install it now? %s[y/N]%s ' "$BOLD" "$RESET"
  read -r reply </dev/tty || reply="n"
  case "$reply" in
    [yY]*)
      curl -LsSf https://astral.sh/uv/install.sh | sh
      # The installer drops uv in ~/.local/bin, which may not be on PATH yet.
      export PATH="$HOME/.local/bin:$PATH"
      command -v uv >/dev/null 2>&1 || die "uv install did not put uv on PATH" \
        'export PATH="$HOME/.local/bin:$PATH"   then re-run this script'
      ok "uv installed"
      ;;
    *)
      die "uv is required." \
        'curl -LsSf https://astral.sh/uv/install.sh | sh
         (or use plain venv: python3.11 -m venv .venv && .venv/bin/pip install -e ".[dev]")'
      ;;
  esac
fi

# ---------------------------------------------------------------- install ----
step "Creating the virtualenv and installing"
uv venv --python "$PYTHON_BIN" >/dev/null
ok ".venv created"

# arm64 wheels exist for every dependency, so this needs no compiler. If it
# starts building from source, something is off — say so rather than hanging.
if ! uv pip install -e ".[dev]" --quiet; then
  die "Install failed." \
    'Re-run without --quiet to see why:  uv pip install -e ".[dev]"'
fi
ok "dependencies installed"

VENV_BIN="$REPO_ROOT/.venv/bin"

# --------------------------------------------------------------------- env ---
step "Configuration"
if [ -f .env ]; then
  ok ".env already exists — leaving it alone"
else
  cp .env.example .env
  ok ".env created from .env.example"
  warn "Email and eBay are unconfigured. The demo works without them."
fi

# ------------------------------------------------------------------- check ---
step "Environment check"
"$VENV_BIN/nellis" check || die "Environment check found a blocking problem (see above)."

# ------------------------------------------------------------------- tests ---
step "Running the test suite"
if "$VENV_BIN/python" -m pytest -q 2>&1 | tail -3; then
  ok "tests passed"
else
  die "Tests failed on this machine." \
    "Paste the output above — a failure here is a genuine portability bug worth fixing."
fi

# -------------------------------------------------------------------- demo ---
step "Seeding the offline demo"
"$VENV_BIN/nellis" demo --reset

# -------------------------------------------------------------------- done ---
PORT="$("$VENV_BIN/python" -c 'from nellis.config import get_settings; print(get_settings().web_port)')"

cat <<EOF

${GREEN}${BOLD}Ready.${RESET}

  ${BOLD}Start the dashboard${RESET}
    .venv/bin/nellis serve
    open http://127.0.0.1:${PORT}

  ${BOLD}Other things to try${RESET}
    .venv/bin/nellis queue            what the engine says to bid on
    .venv/bin/nellis digest           render the alert email
    .venv/bin/nellis check            re-run this environment check

  ${BOLD}When you're ready to go live against the real site${RESET}
    .venv/bin/nellis doctor           can we read Nellis? which fields resolve?
    .venv/bin/nellis record           capture real pages so the parsers can be fixed

  ${DIM}Tip: add the venv to your PATH for this shell so you can drop the prefix:
    export PATH="${REPO_ROOT}/.venv/bin:\$PATH"${RESET}

EOF
