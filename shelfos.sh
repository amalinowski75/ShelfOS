#!/usr/bin/env bash
#
# ShelfOS: one entry point for running it and for installing it.
#
#   ./shelfos.sh devel          a local instance, reloading, in this clone
#   sudo ./shelfos.sh deploy    a real install: user, service, TLS, the lot
#   sudo ./shelfos.sh update    pull, reinstall deps, restart — backup first
#   ./shelfos.sh status         what is installed and whether it is healthy
#   ./shelfos.sh backup         wrap scripts/backup.py with the right paths
#
# Add --dry-run to any of them to see what would happen and change nothing.
#
# bash rather than POSIX sh, which is what run.sh was: this prompts for a
# password without echo, and doing that in sh means `stty -echo` plus a trap
# that, when missed, leaves the terminal unable to show what is typed. That is a
# hostile way to fail in a tool already asking for sudo. Every deployment target
# is Debian or Ubuntu, where bash is a certainty.

# -E (errtrace) is not decoration: without it an ERR trap set in a function is
# not in effect inside the functions it calls, so deploy's failure handler —
# the one that names the step and says nothing was rolled back — would never
# run, and a failed apt-get would exit silently after a step heading.
set -Eeuo pipefail

readonly SHELFOS_VERSION="1.0.0"

# Where a deploy puts things. Not configurable: they are baked into
# deploy/shelfos.service (WorkingDirectory, ReadWritePaths, EnvironmentFile) and
# a path that disagreed with the unit would fail in a way nobody could read.
readonly INSTALL_DIR="/opt/shelfos"
readonly DATA_DIR="/var/lib/shelfos"
readonly ETC_DIR="/etc/shelfos"
readonly ENV_FILE_SYSTEM="$ETC_DIR/env"
readonly SERVICE_NAME="shelfos.service"
readonly SERVICE_PATH="/etc/systemd/system/$SERVICE_NAME"
readonly SERVICE_USER="shelfos"
readonly CADDYFILE="/etc/caddy/Caddyfile"
readonly UDEV_RULE="/etc/udev/rules.d/99-brother-ql.rules"
readonly DEFAULT_PORT=9000

# Brother's USB vendor id, for spotting a label printer without needing lsusb
# (which a minimal server does not have, and which is not worth installing to
# ask one question).
readonly BROTHER_VENDOR="04f9"

DRY_RUN=0
ASSUME_YES=0
QUIET=0
REPO_ROOT=""

# ---------------------------------------------------------------------------
# Output. Everything the user reads goes to stderr except the answers `status`
# is asked for, so `./shelfos.sh status | grep …` stays usable while progress
# chatter still reaches a terminal.
# ---------------------------------------------------------------------------

if [ -t 2 ]; then
    C_DIM=$'\033[2m'; C_RED=$'\033[31m'; C_GREEN=$'\033[32m'
    C_YELLOW=$'\033[33m'; C_BOLD=$'\033[1m'; C_OFF=$'\033[0m'
else
    C_DIM=""; C_RED=""; C_GREEN=""; C_YELLOW=""; C_BOLD=""; C_OFF=""
fi

info() { [ "$QUIET" = 1 ] || printf '%s\n' "$*" >&2; }
note() { [ "$QUIET" = 1 ] || printf '%s%s%s\n' "$C_DIM" "$*" "$C_OFF" >&2; }
warn() { printf '%swarning:%s %s\n' "$C_YELLOW" "$C_OFF" "$*" >&2; }

# die STATUS MESSAGE…  — say what is wrong and stop. Status 2 is reserved for
# usage errors, so a caller can tell "you typed it wrong" from "it did not work".
die() {
    local status=$1; shift
    printf '%serror:%s %s\n' "$C_RED" "$C_OFF" "$*" >&2
    exit "$status"
}

STEP_TOTAL=0
STEP_INDEX=0
STEP_NAME=""

# A step's heading, then its commands indented under it, then its outcome. The
# heading ends in a newline rather than waiting for the result on the same line:
# a step that runs anything prints between the two, and a result appended after
# that lands under the wrong thing.
step() {
    STEP_INDEX=$((STEP_INDEX + 1))
    STEP_NAME=$1
    [ "$QUIET" = 1 ] || printf '%s[%2d/%2d]%s %s\n' \
        "$C_BOLD" "$STEP_INDEX" "$STEP_TOTAL" "$C_OFF" "$1" >&2
}

step_ok()      { [ "$QUIET" = 1 ] || printf '        %s%s%s\n' "$C_GREEN" "${1:-ok}" "$C_OFF" >&2; }
step_skipped() { [ "$QUIET" = 1 ] || printf '        %sskipped (%s)%s\n' "$C_DIM" "$1" "$C_OFF" >&2; }

# ---------------------------------------------------------------------------
# Dry run. Every command that changes anything goes through run() or sudo_run(),
# and every file that is written goes through write_file(). Nothing else in this
# script may touch the system — that single rule is what makes --dry-run
# trustworthy, and what makes deploy testable without root.
# ---------------------------------------------------------------------------

run() {
    if [ "$DRY_RUN" = 1 ]; then
        printf '        %s+ %s%s\n' "$C_DIM" "$*" "$C_OFF" >&2
        return 0
    fi
    "$@"
}

# Privileged commands. In a dry run this prints and returns without invoking
# sudo at all — deliberately, so the preview can be run by anyone, asks for no
# password, and can be asserted in a test that fails if sudo is ever reached.
sudo_run() {
    if [ "$DRY_RUN" = 1 ]; then
        printf '        %s+ sudo %s%s\n' "$C_DIM" "$*" "$C_OFF" >&2
        return 0
    fi
    if [ "$(id -u)" = 0 ]; then
        "$@"
    else
        sudo "$@"
    fi
}

# Anything that looks like a secret, blanked before it reaches a terminal or a
# log. A dry run prints file bodies, and /etc/shelfos/env is mostly secrets;
# even a placeholder must not teach anyone that secrets belong in this output.
redact() {
    sed -E 's/^([A-Za-z_]*(SECRET|PASSWORD|TOKEN|API_KEY)[A-Za-z_]*=).*/\1***/'
}

# write_file PATH MODE OWNER:GROUP  — body on stdin.
#
# Written to a temporary file first and moved into place with install(1), never
# with a redirect into the destination: a redirect creates the file with the
# umask's permissions and only then narrows them, and for a file holding the
# signing secret that window is a real one.
write_file() {
    local dest=$1 mode=$2 owner=$3 tmp
    if [ "$DRY_RUN" = 1 ]; then
        printf '        %s+ write %s (%s, %s)%s\n' "$C_DIM" "$dest" "$owner" "$mode" "$C_OFF" >&2
        redact | sed "s/^/        $C_DIM| /;s/\$/$C_OFF/" >&2
        return 0
    fi
    tmp=$(umask 077 && mktemp "${TMPDIR:-/tmp}/shelfos.XXXXXX")
    cat > "$tmp"
    sudo_run install -m "$mode" -o "${owner%%:*}" -g "${owner##*:}" "$tmp" "$dest"
    rm -f "$tmp"
}

# ---------------------------------------------------------------------------
# Asking. Reads come from /dev/tty, not stdin: deploy may be piped a password,
# or run from a script, and reading an answer out of that stream would be both
# wrong and dangerous.
# ---------------------------------------------------------------------------

have_tty() { [ -r /dev/tty ] && { [ -t 1 ] || [ -t 2 ]; }; }

# ask_yes_no PROMPT DEFAULT   DEFAULT is y or n.
#
# --yes answers yes, as it does everywhere else; a dry run answers yes too, so
# the preview walks the whole path rather than stopping at the first careful
# question. With no terminal and no --yes the default stands, which is what
# keeps an unattended run from talking itself into anything.
ask_yes_no() {
    local prompt=$1 default=$2 reply hint
    if [ "$default" = y ]; then hint="[Y/n]"; else hint="[y/N]"; fi
    if [ "$ASSUME_YES" = 1 ] || [ "$DRY_RUN" = 1 ]; then
        [ "$QUIET" = 1 ] || printf '%s %s %s\n' "$prompt" "$hint" "yes" >&2
        return 0
    fi
    if ! have_tty; then
        [ "$default" = y ]
        return
    fi
    while :; do
        printf '%s %s ' "$prompt" "$hint" >&2
        read -r reply < /dev/tty || reply=""
        case $(printf '%s' "$reply" | tr '[:upper:]' '[:lower:]') in
            y|yes) return 0 ;;
            n|no)  return 1 ;;
            "")    [ "$default" = y ]; return ;;
        esac
    done
}

# ask_value VARNAME PROMPT DEFAULT
ask_value() {
    local __var=$1 __prompt=$2 __default=$3 __reply
    if [ "$DRY_RUN" = 1 ] || ! have_tty; then
        printf -v "$__var" '%s' "$__default"
        return 0
    fi
    if [ -n "$__default" ]; then
        printf '%s [%s]: ' "$__prompt" "$__default" >&2
    else
        printf '%s: ' "$__prompt" >&2
    fi
    read -r __reply < /dev/tty || __reply=""
    printf -v "$__var" '%s' "${__reply:-$__default}"
}

# ask_secret VARNAME PROMPT  — twice, hidden, and held to the app's own floor so
# the answer cannot be one the app will later refuse.
ask_secret() {
    local __var=$1 __prompt=$2 __first __second
    while :; do
        printf '%s: ' "$__prompt" >&2
        read -rs __first < /dev/tty || __first=""
        printf '\n' >&2
        printf '%s (again): ' "$__prompt" >&2
        read -rs __second < /dev/tty || __second=""
        printf '\n' >&2
        if [ "$__first" != "$__second" ]; then
            warn "They do not match."
            continue
        fi
        if [ ${#__first} -lt 8 ]; then
            warn "At least 8 characters — the app enforces the same floor."
            continue
        fi
        break
    done
    printf -v "$__var" '%s' "$__first"
}

# ---------------------------------------------------------------------------
# The environment file.
#
# Parsed, not sourced. run.sh sourced it, which had two consequences worth
# ending: the file's values overrode the caller's environment (so
# `PORT=8080 ./run.sh` silently did nothing, against what the README promised),
# and the file ran as shell — which deploy would be doing as root, against a
# file in /etc. Parsing fixes both and makes this file mean the same thing here
# as it does to systemd's EnvironmentFile, which cannot run shell either.
#
# The caller's environment wins; the file fills what is unset.
# ---------------------------------------------------------------------------

load_env_file() {
    local file=$1 line key value
    [ -f "$file" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        case $line in ''|'#'*) continue ;; esac
        line=${line#export }                       # tolerated, though systemd cannot
        case $line in *=*) ;; *) continue ;; esac
        key=${line%%=*}
        value=${line#*=}
        key=${key%"${key##*[![:space:]]}"}         # trim trailing space
        # The whole key, not just its first character: `${!key}` below is an
        # indirect expansion, and bash makes an invalid name there a fatal
        # error, which under `set -e` ends the script on somebody's typo with a
        # message that names neither the file nor the line.
        case $key in
            ''|[0-9]*|*[!A-Za-z0-9_]*)
                warn "$file: ignoring '$key', which is not a variable name"
                continue ;;
        esac
        # One layer of matching quotes, the way systemd strips them, so a file
        # written either way keeps working in both places.
        case $value in
            \"*\") value=${value#\"}; value=${value%\"} ;;
            \'*\') value=${value#\'}; value=${value%\'} ;;
        esac
        # shellcheck disable=SC2016  # matching the literal characters, not expanding them
        case $value in
            *'$('*|*'`'*)
                warn "$file: $key contains a command substitution, which is no longer run" ;;
        esac
        # Only if unset. `${!key+x}` is empty for an unset variable and "x" for
        # one set to anything, including the empty string — an explicitly empty
        # SHELFOS_LABEL_DEVICE means "no printer" and must not be overwritten.
        if [ -z "${!key+x}" ]; then
            export "$key=$value"
        fi
    done < "$file"
}

# env_file_value FILE KEY — read one value without exporting anything, for
# `status` and `backup`, which need to know where the data is without adopting
# the whole production environment into their own process.
env_file_value() {
    local file=$1 want=$2 line key value
    [ -f "$file" ] || return 0
    while IFS= read -r line || [ -n "$line" ]; do
        case $line in ''|'#'*) continue ;; esac
        line=${line#export }
        key=${line%%=*}
        [ "$key" = "$want" ] || continue
        value=${line#*=}
        case $value in
            \"*\") value=${value#\"}; value=${value%\"} ;;
            \'*\') value=${value#\'}; value=${value%\'} ;;
        esac
        printf '%s' "$value"
        return 0
    done < "$file"
}

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

resolve_repo_root() {
    local src=$0
    # Follow symlinks, so `ln -s …/shelfos.sh ~/bin/shelfos` finds the checkout.
    while [ -L "$src" ]; do
        local dir; dir=$(cd -P "$(dirname "$src")" && pwd)
        src=$(readlink "$src")
        case $src in /*) ;; *) src="$dir/$src" ;; esac
    done
    REPO_ROOT=$(cd -P "$(dirname "$src")" && pwd)
    readonly REPO_ROOT
}

require_checkout() {
    local missing=""
    local f
    for f in app/main.py pyproject.toml scripts/seed_demo.py deploy/shelfos.service; do
        [ -e "$REPO_ROOT/$f" ] || missing="$missing $f"
    done
    [ -z "$missing" ] || die 1 "this does not look like a ShelfOS checkout; missing:$missing"
}

valid_port() {
    case $1 in
        ''|*[!0-9]*) return 1 ;;
    esac
    [ "$1" -ge 1 ] && [ "$1" -le 65535 ]
}

# resolve_port FLAG_VALUE — flag, then the environment, then the file (already
# merged into the environment by load_env_file), then the default.
resolve_port() {
    local from_flag=$1 port
    port=${from_flag:-${PORT:-$DEFAULT_PORT}}
    valid_port "$port" || die 2 "port must be a number between 1 and 65535, not '$port'"
    if [ "$port" -lt 1024 ]; then
        warn "port $port is privileged; binding it needs root or a capability"
    fi
    printf '%s' "$port"
}

port_is_free() {
    local port=$1
    if command -v ss > /dev/null 2>&1; then
        if ss -ltnH "sport = :$port" 2>/dev/null | grep -q .; then
            return 1
        fi
        return 0
    fi
    # No `ss` (a slim container): try to connect instead. Connecting means
    # something is listening, so the port is not free.
    if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
        return 1
    fi
    return 0
}

is_deployed() { [ -e "$SERVICE_PATH" ] || [ -d "$INSTALL_DIR" ]; }

# The port an install actually listens on, read back from the unit that defines
# it. Deploy templates --port into ExecStart, so the unit is the one place that
# knows; assuming the default here made `status` report a healthy install as
# unreachable, and made `update` tell the operator to roll back a good update.
installed_port() {
    local port=""
    if [ -r "$SERVICE_PATH" ]; then
        port=$(sed -n 's/^[[:space:]]*--port[[:space:]]\{1,\}\([0-9]\{1,\}\).*/\1/p' "$SERVICE_PATH" | head -1)
    fi
    if [ -z "$port" ]; then
        port=$(env_file_value "$ENV_FILE_SYSTEM" PORT)
    fi
    printf '%s' "${port:-$DEFAULT_PORT}"
}

gen_secret() {
    "${PYTHON:-python3}" -c 'import secrets; print(secrets.token_urlsafe(48))'
}

# health_probe PORT SECONDS — poll /health until it answers or time runs out.
health_probe() {
    local port=$1 seconds=$2 waited=0
    while [ "$waited" -lt "$seconds" ]; do
        if curl -fsS --max-time 2 "http://127.0.0.1:$port/health" > /dev/null 2>&1; then
            return 0
        fi
        sleep 1
        waited=$((waited + 1))
    done
    return 1
}

# ---------------------------------------------------------------------------
# The virtualenv, shared by devel and deploy.
#
# ensure_venv DIR EXTRA — DIR holds .venv; EXTRA is "[dev]" for a working copy
# and empty for a server, which has no use for pytest and ruff.
#
# Rebuild when uvicorn is missing or pyproject.toml is newer than the stamp: a
# pull that adds a dependency would otherwise leave the venv a version behind
# and fail at import, which reads as a broken app rather than a stale one.
# ---------------------------------------------------------------------------

ensure_venv() {
    local dir=$1 extra=$2 venv="$1/.venv" stamp="$1/.venv/.shelfos-installed"
    local python=${PYTHON:-python3}

    if [ -x "$venv/bin/uvicorn" ] && [ ! "$dir/pyproject.toml" -nt "$stamp" ]; then
        return 1   # nothing to do; the caller decides whether to say so
    fi

    if ! "$python" -c 'import venv' > /dev/null 2>&1; then
        warn "$python cannot create virtualenvs (the venv module is missing)."
        if ask_yes_no "Install python3-venv with apt?" y; then
            sudo_run apt-get install -y python3-venv
        else
            die 1 "cannot continue without the venv module; on Debian/Ubuntu: sudo apt install python3-venv"
        fi
    fi

    # No "does it exist" guard: venv is idempotent, and the state that needs it
    # most is a half-built .venv/ from an interrupted first run — the module
    # makes the directories first and ensurepip is the slow part, so that is
    # exactly what an interruption leaves behind.
    run "$python" -m venv "$venv" \
        || die 1 "could not create $venv"
    run "$venv/bin/pip" install --quiet --upgrade pip
    run "$venv/bin/pip" install --quiet --editable "$dir$extra"
    run touch "$stamp"
    return 0
}

# ---------------------------------------------------------------------------
# devel — what run.sh did, plus the conveniences that were always missing.
#
# It runs in the clone and keeps its database there: data/shelfos.db and
# attachments/, exactly as before. Separate instances are separate clones, so
# there is nothing to isolate here and nothing under data/ or attachments/ is
# ever moved, renamed or deleted by this script. The one destructive path is
# --reset, which hands off to scripts/reset_db.py and its typed confirmation.
# ---------------------------------------------------------------------------

usage_devel() {
    cat <<'EOF'
Usage: ./shelfos.sh devel [options]

Run a local instance from this clone, reloading on edits.

  --port N        listen on N (default 9000, or PORT from the environment)
  --host ADDR     bind address (default 127.0.0.1)
  --reset         wipe the database and attachments first (asks, via reset_db.py)
  --no-seed       do not offer demo data for an empty database
  --force-seed    add demo data even to a populated database
  --env-file PATH read settings from PATH (default ~/.ShelfOS/.env)
  --python BIN    interpreter that builds the virtualenv (default python3)
  --no-install    never touch the virtualenv, even when it is stale

The database stays in this clone at data/shelfos.db. Run another instance from
another clone; --port only changes the port.
EOF
}

cmd_devel() {
    local port_flag="" host="127.0.0.1" do_reset=0 seed=ask env_file="" no_install=0

    while [ $# -gt 0 ]; do
        case $1 in
            --port)       port_flag=${2:-}; shift 2 ;;
            --host)       host=${2:-}; shift 2 ;;
            --reset)      do_reset=1; shift ;;
            --no-seed)    seed=no; shift ;;
            --force-seed) seed=force; shift ;;
            --env-file)   env_file=${2:-}; shift 2 ;;
            --python)     PYTHON=${2:-}; export PYTHON; shift 2 ;;
            --no-install) no_install=1; shift ;;
            -h|--help)    usage_devel; return 0 ;;
            *)            usage_devel >&2; die 2 "unknown option for devel: $1" ;;
        esac
    done

    cd "$REPO_ROOT"
    load_env_file "${env_file:-${SHELFOS_ENV_FILE:-$HOME/.ShelfOS/.env}}"
    if [ ! -f "${env_file:-${SHELFOS_ENV_FILE:-$HOME/.ShelfOS/.env}}" ]; then
        note "No ${env_file:-${SHELFOS_ENV_FILE:-$HOME/.ShelfOS/.env}} — development defaults (admin/admin, no shop keys)."
    fi

    local port; port=$(resolve_port "$port_flag")
    if ! port_is_free "$port"; then
        die 1 "something is already listening on port $port; try --port $((port + 1))"
    fi

    if [ "$no_install" = 0 ]; then
        if ensure_venv "$REPO_ROOT" "[dev]"; then
            note "Virtualenv built."
        fi
    fi
    if [ ! -x "$REPO_ROOT/.venv/bin/uvicorn" ]; then
        # A dry run reports rather than refuses: it is a preview of what would
        # happen, and it is the one mode that has to work on a machine where
        # nothing has been set up yet — including a CI runner that installed the
        # project into its own Python and never built a .venv here.
        if [ "$DRY_RUN" = 1 ]; then
            warn "no virtualenv at $REPO_ROOT/.venv; a real run would build one first"
        else
            die 1 "no virtualenv at $REPO_ROOT/.venv (drop --no-install to build one)"
        fi
    fi

    local py="$REPO_ROOT/.venv/bin/python"
    local db_path="${DATABASE_URL:-sqlite:///data/shelfos.db}"
    db_path=${db_path#sqlite:///}

    if [ "$do_reset" = 1 ]; then
        # reset_db.py owns the confirmation and the deletion; this script never
        # removes data itself. Its typed "yes" stands unless the caller already
        # said -y to everything.
        if [ "$ASSUME_YES" = 1 ]; then
            run "$py" scripts/reset_db.py --yes
        else
            run "$py" scripts/reset_db.py
        fi
    fi

    if [ "$seed" = force ]; then
        run "$py" scripts/seed_demo.py --force
    elif [ "$seed" = ask ] && [ ! -s "$db_path" ]; then
        # Only for a database that is absent or empty. seed_demo.py is itself a
        # no-op on a populated one, but asking first is the difference between
        # a tool that helps and one that surprises.
        note "No database at $db_path yet."
        if ask_yes_no "Fill it with demo data?" y; then
            run "$py" scripts/seed_demo.py
        fi
    fi

    info "ShelfOS on http://$host:$port  (database $db_path)"
    if [ "$DRY_RUN" = 1 ]; then
        printf '        %s+ exec .venv/bin/uvicorn app.main:app --reload --host %s --port %s%s\n' \
            "$C_DIM" "$host" "$port" "$C_OFF" >&2
        return 0
    fi
    exec "$REPO_ROOT/.venv/bin/uvicorn" app.main:app --reload --host "$host" --port "$port"
}

# ---------------------------------------------------------------------------
# deploy — the whole of deploy/README.md, performed.
# ---------------------------------------------------------------------------

usage_deploy() {
    cat <<'EOF'
Usage: sudo ./shelfos.sh deploy [options]

Install ShelfOS as a system service: a user, /opt/shelfos, /var/lib/shelfos,
settings in /etc/shelfos/env, a systemd unit, and Caddy holding the certificate.

  --domain HOST          hostname Caddy serves (asked for if omitted)
  --port N               port the service listens on (default 9000)
  --admin-user NAME      first admin's username (default admin)
  --admin-password-stdin read the first admin's password from stdin
  --no-caddy             install the service only; arrange TLS yourself
  --no-tls               no domain, no Caddy (implies --no-caddy)
  --printer/--no-printer keep or strip the label-printer support
  --import-db PATH       copy an existing database in before the first start
  --import-attachments D copy an existing attachments directory in with it
  --reinstall            repair an existing install instead of refusing
  --dry-run              print what would happen; never calls sudo
  -y, --yes              accept the defaults and skip the confirmation

Re-running is safe: every step reports what it skipped.
EOF
}

# What deploy learned before it starts changing things.
DEPLOY_DOMAIN=""
DEPLOY_PORT=""
DEPLOY_ADMIN_USER="admin"
DEPLOY_ADMIN_PASSWORD=""
DEPLOY_WANT_CADDY=1
DEPLOY_WANT_PRINTER=""
DEPLOY_PRINTER_GROUP="plugdev"
DEPLOY_IMPORT_DB=""
DEPLOY_IMPORT_ATTACHMENTS=""
DEPLOY_REINSTALL=0
DEPLOY_SOURCE_SHA=""

deploy_preflight() {
    local ids=""
    if [ -r /etc/os-release ]; then
        # In a subshell so the file's two dozen other variables stay out of ours.
        # shellcheck disable=SC1091  # a system file, not one this repo ships
        ids=$(. /etc/os-release && printf '%s %s' "${ID:-}" "${ID_LIKE:-}")
    fi
    case "$ids" in
        *debian*|*ubuntu*) ;;
        *) die 1 "deploy handles Debian and Ubuntu only. Everything it does is written out step by step in deploy/README.md under 'Doing it by hand'." ;;
    esac

    command -v apt-get > /dev/null 2>&1 || die 1 "apt-get is missing; see deploy/README.md for the manual steps"
    command -v systemctl > /dev/null 2>&1 || die 1 "systemctl is missing; this needs a systemd host"
    # systemctl existing is not the same as systemd running it — a container
    # often has the binary and no init, and every later step would fail oddly.
    [ -d /run/systemd/system ] || die 1 "systemd is not running here (no /run/systemd/system); this needs a real host or a systemd-enabled container"

    local python=${PYTHON:-python3}
    command -v "$python" > /dev/null 2>&1 || die 1 "$python is not installed"
    "$python" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' \
        || die 1 "ShelfOS needs Python 3.12 or newer; $python is $("$python" -V 2>&1). This is the one thing deploy cannot install for you on an older release."

    if [ "$DRY_RUN" = 0 ] && [ "$(id -u)" != 0 ] && ! command -v sudo > /dev/null 2>&1; then
        die 1 "run this as root, or install sudo"
    fi

    if git -C "$REPO_ROOT" rev-parse --git-dir > /dev/null 2>&1; then
        DEPLOY_SOURCE_SHA=$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || printf 'unknown')
        if [ -n "$(git -C "$REPO_ROOT" status --porcelain 2>/dev/null)" ]; then
            warn "this clone has uncommitted changes, and deploy installs committed work only ($DEPLOY_SOURCE_SHA)"
            ask_yes_no "Continue anyway?" n || die 1 "stopped; commit first, or deploy from a clean clone"
        fi
    else
        DEPLOY_SOURCE_SHA="no git"
    fi

    if is_deployed && [ "$DEPLOY_REINSTALL" = 0 ]; then
        info "ShelfOS is already installed:"
        [ -d "$INSTALL_DIR" ] && info "  code     $INSTALL_DIR"
        [ -e "$SERVICE_PATH" ] && info "  service  $SERVICE_PATH"
        [ -e "$ENV_FILE_SYSTEM" ] && info "  settings $ENV_FILE_SYSTEM"
        info ""
        info "Use './shelfos.sh update' to move it forward, './shelfos.sh status' to look"
        info "at it, or add --reinstall to repair this install in place."
        exit 0
    fi
}

# A Brother on USB, without needing lsusb.
detect_printer() {
    local f vendor
    for f in /sys/bus/usb/devices/*/idVendor; do
        [ -r "$f" ] || continue
        read -r vendor < "$f" || continue
        if [ "$vendor" = "$BROTHER_VENDOR" ]; then
            local product=""
            [ -r "${f%/idVendor}/product" ] && read -r product < "${f%/idVendor}/product"
            printf '%s' "${product:-Brother device}"
            return 0
        fi
    done
    return 1
}

deploy_gather() {
    # Everything asked here, before anything is written, so a half-answered
    # install is not a half-made one.
    if [ -z "$DEPLOY_PORT" ]; then
        DEPLOY_PORT=$DEFAULT_PORT
    fi
    valid_port "$DEPLOY_PORT" || die 2 "port must be a number between 1 and 65535, not '$DEPLOY_PORT'"

    if [ "$DEPLOY_WANT_CADDY" = 1 ] && [ -z "$DEPLOY_DOMAIN" ]; then
        if [ "$DRY_RUN" = 1 ] || ! have_tty; then
            DEPLOY_DOMAIN="shelfos.example.com"
        else
            while :; do
                ask_value DEPLOY_DOMAIN "Hostname Caddy should serve (blank for no TLS)" ""
                [ -n "$DEPLOY_DOMAIN" ] || { DEPLOY_WANT_CADDY=0; break; }
                case $DEPLOY_DOMAIN in
                    *.*[!.]) break ;;
                    *) warn "that does not look like a hostname" ;;
                esac
            done
        fi
    fi

    if [ -z "$DEPLOY_ADMIN_PASSWORD" ]; then
        if [ -f "$ENV_FILE_SYSTEM" ]; then
            # The existing file is kept whatever happens, so there is no
            # password to ask for.
            DEPLOY_ADMIN_PASSWORD="(keeping the existing settings file)"
        elif [ "$DRY_RUN" = 1 ]; then
            DEPLOY_ADMIN_PASSWORD="(would be asked for)"
        elif have_tty; then
            ask_secret DEPLOY_ADMIN_PASSWORD "Password for the first admin ($DEPLOY_ADMIN_USER)"
        else
            # Never invent one. A placeholder here would be written into
            # /etc/shelfos/env verbatim and would be long enough to clear the
            # app's own floor, so the install would come up healthy with the
            # first admin on a password that is printed in this file.
            die 1 "no terminal to ask for the first admin's password. Pass it with --admin-password-stdin, as in: printf '%s' \"\$PASSWORD\" | sudo ./shelfos.sh deploy --admin-password-stdin …"
        fi
    fi

    if [ -z "$DEPLOY_WANT_PRINTER" ]; then
        local found
        if found=$(detect_printer); then
            info "Found a label printer on USB: $found"
            if ask_yes_no "Set up label printing for it?" y; then
                DEPLOY_WANT_PRINTER=1
            else
                DEPLOY_WANT_PRINTER=0
            fi
        else
            DEPLOY_WANT_PRINTER=0
        fi
    fi
    # The unit and the udev rule have to name the same group, and Ubuntu Server
    # images do not always have plugdev. Choosing here keeps them in step.
    if [ "$DEPLOY_WANT_PRINTER" = 1 ] && ! getent group plugdev > /dev/null 2>&1; then
        DEPLOY_PRINTER_GROUP="lp"
    fi
}

deploy_summary() {
    info ""
    info "${C_BOLD}About to install ShelfOS${C_OFF}"
    info "  code            $INSTALL_DIR  (from $REPO_ROOT @ $DEPLOY_SOURCE_SHA)"
    info "  data            $DATA_DIR"
    if [ -f "$ENV_FILE_SYSTEM" ]; then
        info "  settings        $ENV_FILE_SYSTEM  (exists — kept as it is)"
    else
        info "  settings        $ENV_FILE_SYSTEM  (new, with a generated secret key)"
    fi
    info "  service         $SERVICE_NAME  port $DEPLOY_PORT, user $SERVICE_USER"
    if [ "$DEPLOY_WANT_CADDY" = 1 ]; then
        info "  proxy           Caddy on $DEPLOY_DOMAIN"
    else
        info "  proxy           none — the service listens on 127.0.0.1:$DEPLOY_PORT only"
    fi
    if [ "$DEPLOY_WANT_PRINTER" = 1 ]; then
        info "  label printer   kept, group $DEPLOY_PRINTER_GROUP, udev rule installed"
    else
        info "  label printer   not configured"
    fi
    [ -n "$DEPLOY_IMPORT_DB" ] && info "  importing       $DEPLOY_IMPORT_DB"
    info ""
}

deploy_failed() {
    printf '\n%serror:%s deploy failed during step %d/%d (%s).\n' \
        "$C_RED" "$C_OFF" "$STEP_INDEX" "$STEP_TOTAL" "$STEP_NAME" >&2
    cat >&2 <<EOF

Nothing has been rolled back, on purpose: removing a half-made user, an apt
source, or a directory that may already hold data is more dangerous than leaving
this as it stands. Every step above is safe to run again — fix what went wrong
and re-run the same command.
EOF
    exit 1
}

cmd_deploy() {
    while [ $# -gt 0 ]; do
        case $1 in
            --domain)               DEPLOY_DOMAIN=${2:-}; shift 2 ;;
            --port)                 DEPLOY_PORT=${2:-}; shift 2 ;;
            --admin-user)           DEPLOY_ADMIN_USER=${2:-}; shift 2 ;;
            --admin-password-stdin) IFS= read -r DEPLOY_ADMIN_PASSWORD || true; shift ;;
            --no-caddy)             DEPLOY_WANT_CADDY=0; shift ;;
            --no-tls)               DEPLOY_WANT_CADDY=0; DEPLOY_DOMAIN=""; shift ;;
            --printer)              DEPLOY_WANT_PRINTER=1; shift ;;
            --no-printer)           DEPLOY_WANT_PRINTER=0; shift ;;
            --import-db)            DEPLOY_IMPORT_DB=${2:-}; shift 2 ;;
            --import-attachments)   DEPLOY_IMPORT_ATTACHMENTS=${2:-}; shift 2 ;;
            --reinstall)            DEPLOY_REINSTALL=1; shift ;;
            -h|--help)              usage_deploy; return 0 ;;
            *)                      usage_deploy >&2; die 2 "unknown option for deploy: $1" ;;
        esac
    done

    cd "$REPO_ROOT"
    require_checkout
    deploy_preflight
    deploy_gather
    deploy_summary

    if [ "$DRY_RUN" = 1 ]; then
        note "(dry run: nothing below runs, and sudo is never called)"
    elif ! ask_yes_no "Proceed?" n; then
        die 1 "stopped; nothing was changed"
    fi

    trap 'deploy_failed' ERR
    STEP_TOTAL=12
    STEP_INDEX=0

    deploy_step_packages
    deploy_step_caddy_package
    deploy_step_user
    deploy_step_dirs
    deploy_step_code
    deploy_step_venv
    deploy_step_env
    deploy_step_import
    deploy_step_unit
    deploy_step_printer
    deploy_step_caddy_config
    deploy_step_verify
    trap - ERR
}

deploy_step_packages() {
    step "system packages"
    local missing="" pkg
    for pkg in python3-venv git curl; do
        dpkg-query -W -f='${Status}' "$pkg" 2>/dev/null | grep -q "^install ok installed$" || missing="$missing $pkg"
    done
    if [ -z "$missing" ]; then
        step_skipped "already present"
        return 0
    fi
    note "        needed:$missing"
    if ! ask_yes_no "  Install$missing with apt?" y; then
        die 1 "cannot continue without$missing"
    fi
    sudo_run apt-get update
    # shellcheck disable=SC2086  # deliberately word-split: a list of packages
    sudo_run apt-get install -y $missing
}

deploy_step_caddy_package() {
    step "Caddy"
    if [ "$DEPLOY_WANT_CADDY" = 0 ]; then
        step_skipped "not wanted"
        return 0
    fi
    if command -v caddy > /dev/null 2>&1; then
        step_skipped "already installed"
        return 0
    fi
    note "        not installed"
    # Try the distribution first. Where Caddy is packaged, that is one less
    # third-party source on the machine for the rest of its life.
    if apt-cache policy caddy 2>/dev/null | grep -q 'Candidate: [0-9]'; then
        if ask_yes_no "  Install caddy from the distribution?" y; then
            sudo_run apt-get install -y caddy
            return 0
        fi
    fi
    info ""
    info "  Caddy is not in this system's repositories. Installing it means adding"
    info "  a third-party apt source, which will keep feeding this machine packages:"
    info ""
    info "    key     https://dl.cloudsmith.io/public/caddy/stable/gpg.key"
    info "    source  https://dl.cloudsmith.io/public/caddy/stable/deb/debian any-version main"
    info ""
    if ! ask_yes_no "  Add it?" n; then
        DEPLOY_WANT_CADDY=0
        warn "skipping Caddy; the service will listen on 127.0.0.1 only and you must arrange TLS yourself"
        return 0
    fi
    sudo_run apt-get install -y debian-keyring debian-archive-keyring apt-transport-https
    run sh -c "curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor | sudo tee /usr/share/keyrings/caddy-stable-archive-keyring.gpg > /dev/null"
    run sh -c "curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list > /dev/null"
    sudo_run apt-get update
    sudo_run apt-get install -y caddy
}

deploy_step_user() {
    step "service user"
    if getent passwd "$SERVICE_USER" > /dev/null 2>&1; then
        # A human account that happens to share the name is not ours to adopt:
        # the deploy would chown its home and run a service as it.
        local uid; uid=$(id -u "$SERVICE_USER")
        if [ "$uid" -ge 1000 ]; then
            die 1 "a login account named '$SERVICE_USER' already exists (uid $uid); rename it or install by hand"
        fi
        step_skipped "exists"
        return 0
    fi
    sudo_run useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
    step_ok
}

deploy_step_dirs() {
    step "directories"
    local made=""
    local d
    for d in "$INSTALL_DIR" "$DATA_DIR" "$DATA_DIR/attachments" "$ETC_DIR"; do
        if [ -d "$d" ]; then continue; fi
        sudo_run install -d -m 755 "$d"
        made="$made $d"
    done
    if [ -z "$made" ]; then step_skipped "all exist"; else step_ok; fi
}

deploy_step_code() {
    step "code → $INSTALL_DIR"
    if [ -d "$INSTALL_DIR/.git" ]; then
        step_skipped "already a checkout"
        return 0
    fi
    if [ "$DEPLOY_SOURCE_SHA" = "no git" ]; then
        # A tarball rather than a clone. Copy, and exclude everything that is
        # local state: the venv, the database, the attachments, any .env.
        step_ok "copying (no git history — 'update' will need a clone)"
        sudo_run rsync -a \
            --exclude '.venv' --exclude 'data' --exclude 'attachments' \
            --exclude '.env' --exclude 'node_modules' --exclude '__pycache__' \
            "$REPO_ROOT/" "$INSTALL_DIR/"
        return 0
    fi
    # --local keeps .git, which update needs for `git pull`; --no-hardlinks
    # because a chown -R follows and hardlinked objects across an ownership
    # change are a trap. Neither data/ nor attachments/ is tracked, so this
    # copies the code and nothing else.
    sudo_run git clone --quiet --local --no-hardlinks "$REPO_ROOT" "$INSTALL_DIR"
    local origin
    if origin=$(git -C "$REPO_ROOT" remote get-url origin 2>/dev/null); then
        # Without this, update would try to pull from the deploying user's home
        # directory — which the service user cannot read, and which may not
        # exist on this machine at all.
        sudo_run git -C "$INSTALL_DIR" remote set-url origin "$origin"
    fi
    step_ok "$DEPLOY_SOURCE_SHA"
}

deploy_step_venv() {
    step "virtualenv"
    if [ -x "$INSTALL_DIR/.venv/bin/uvicorn" ] \
        && [ ! "$INSTALL_DIR/pyproject.toml" -nt "$INSTALL_DIR/.venv/.shelfos-installed" ]; then
        step_skipped "up to date"
    else
        # No [dev] extra: a server has no use for pytest, ruff and mypy.
        sudo_run "${PYTHON:-python3}" -m venv "$INSTALL_DIR/.venv"
        sudo_run "$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip
        sudo_run "$INSTALL_DIR/.venv/bin/pip" install --quiet --editable "$INSTALL_DIR"
        sudo_run touch "$INSTALL_DIR/.venv/.shelfos-installed"
        step_ok
    fi
    # The data is the service's; the code is not. ProtectSystem=strict already
    # makes /opt/shelfos read-only to the unit, so handing it over bought
    # nothing — and now that `backup` and `password` run root out of that tree,
    # a service account able to edit it would be a way from a web-app bug to
    # root on the operator's next sudo. Stated rather than left alone, so an
    # install made by an earlier version is corrected on the next deploy.
    sudo_run chown -R root:root "$INSTALL_DIR"
    sudo_run chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR"
}

deploy_step_env() {
    step "settings → $ENV_FILE_SYSTEM"
    if [ -f "$ENV_FILE_SYSTEM" ]; then
        # Never overwritten. It holds the signing secret, and replacing that
        # signs everyone out and invalidates every API token; it also holds the
        # shop API keys, which are not recoverable from here.
        step_skipped "kept as it is"
        if grep -q 'replace-me' "$ENV_FILE_SYSTEM" 2>/dev/null; then
            warn "$ENV_FILE_SYSTEM still contains 'replace-me'; the service will refuse to start until that is fixed"
        fi
        return 0
    fi
    local secret
    secret=$(gen_secret)
    render_env_file "$secret" | write_file "$ENV_FILE_SYSTEM" 640 "root:$SERVICE_USER"
    step_ok
}

# The example file with four of its values replaced, its comments intact —
# they are the documentation for every setting in it.
#
# Done with shell string handling rather than sed. A password is arbitrary text
# and sed's replacement side is not: `&` there expands to the whole matched
# line, so `p&ss` would be stored as a mangled string the user could never sign
# in with, and `|` would close the s/// command and fail the step outright.
# Neither is a thing anyone would connect to the password they had just typed.
render_env_file() {
    local secret=$1 line key
    while IFS= read -r line || [ -n "$line" ]; do
        key=${line%%=*}
        case $key in
            SHELFOS_SECRET_KEY)    printf 'SHELFOS_SECRET_KEY=%s\n' "$secret" ;;
            SHELFOS_ADMIN_USERNAME) printf 'SHELFOS_ADMIN_USERNAME=%s\n' "$DEPLOY_ADMIN_USER" ;;
            SHELFOS_ADMIN_PASSWORD) printf 'SHELFOS_ADMIN_PASSWORD=%s\n' "$DEPLOY_ADMIN_PASSWORD" ;;
            \#SHELFOS_LABEL_DEVICE|SHELFOS_LABEL_DEVICE)
                if [ "$DEPLOY_WANT_PRINTER" = 1 ]; then
                    printf 'SHELFOS_LABEL_DEVICE=/dev/shelfos-label\n'
                else
                    printf '#SHELFOS_LABEL_DEVICE=/dev/shelfos-label\n'
                fi ;;
            *) printf '%s\n' "$line" ;;
        esac
    done < "$REPO_ROOT/deploy/shelfos.env.example"
}

deploy_step_import() {
    step "existing data"
    if [ -z "$DEPLOY_IMPORT_DB" ] && [ -z "$DEPLOY_IMPORT_ATTACHMENTS" ]; then
        step_skipped "nothing to import"
        return 0
    fi
    # Before the first start, which is the only safe moment: once the service
    # has run, it has seeded its own admin and a copy over the top would be
    # replacing a database somebody may already have used.
    if [ -n "$DEPLOY_IMPORT_DB" ]; then
        [ -f "$DEPLOY_IMPORT_DB" ] || die 1 "no database at $DEPLOY_IMPORT_DB"
        if [ -s "$DATA_DIR/shelfos.db" ]; then
            die 1 "$DATA_DIR/shelfos.db already exists; move it aside first, or restore into a running install with './shelfos.sh backup restore'"
        fi
        sudo_run cp "$DEPLOY_IMPORT_DB" "$DATA_DIR/shelfos.db"
    fi
    if [ -n "$DEPLOY_IMPORT_ATTACHMENTS" ]; then
        [ -d "$DEPLOY_IMPORT_ATTACHMENTS" ] || die 1 "no attachments directory at $DEPLOY_IMPORT_ATTACHMENTS"
        sudo_run cp -r "$DEPLOY_IMPORT_ATTACHMENTS/." "$DATA_DIR/attachments/"
    fi
    sudo_run chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR"
    step_ok
}

deploy_step_unit() {
    step "systemd unit"
    local rendered tmp
    tmp=$(mktemp "${TMPDIR:-/tmp}/shelfos-unit.XXXXXX")
    # The unit in deploy/ is the source of truth; only the port and the printer
    # block are decided here.
    sed -e "s|^    --port [0-9]*|    --port $DEPLOY_PORT|" \
        -e "s|^SupplementaryGroups=.*|SupplementaryGroups=$DEPLOY_PRINTER_GROUP|" \
        "$REPO_ROOT/deploy/shelfos.service" > "$tmp"
    if [ "$DEPLOY_WANT_PRINTER" = 0 ]; then
        # Drop the printer block, leaving PrivateDevices=yes from the sandbox
        # section to stand.
        awk '/^# --- Label printer/{skip=1} /^\[Install\]/{skip=0} !skip' "$tmp" > "$tmp.stripped"
        mv "$tmp.stripped" "$tmp"
    fi
    rendered=$(cat "$tmp")
    rm -f "$tmp"

    if [ -f "$SERVICE_PATH" ] && [ "$rendered" = "$(cat "$SERVICE_PATH" 2>/dev/null)" ]; then
        step_skipped "unchanged"
    else
        if [ -f "$SERVICE_PATH" ]; then
            note "        an installed unit differs from this one:"
            info ""
            diff -u "$SERVICE_PATH" - <<< "$rendered" >&2 || true
            info ""
            ask_yes_no "  Install this unit?" n || die 1 "stopped; the unit was left as it was"
            sudo_run cp "$SERVICE_PATH" "$SERVICE_PATH.bak-$(date +%Y%m%d%H%M%S)"
        fi
        printf '%s\n' "$rendered" | write_file "$SERVICE_PATH" 644 "root:root"
        step_ok
    fi
    sudo_run systemctl daemon-reload
    sudo_run systemctl enable --now "$SERVICE_NAME"
}

deploy_step_printer() {
    step "label printer"
    if [ "$DEPLOY_WANT_PRINTER" = 0 ]; then
        step_skipped "not configured"
        return 0
    fi
    # Three things have to name the same group: this rule, the unit's
    # SupplementaryGroups, and nothing else. A mismatch fails with EACCES, which
    # reads exactly like the printer being unplugged.
    local rule="SUBSYSTEM==\"usbmisc\", ATTRS{idVendor}==\"$BROTHER_VENDOR\", MODE=\"0660\", GROUP=\"$DEPLOY_PRINTER_GROUP\", SYMLINK+=\"shelfos-label\""
    if [ -f "$UDEV_RULE" ] && grep -qF "$rule" "$UDEV_RULE" 2>/dev/null; then
        step_skipped "rule already installed"
    else
        printf '# Installed by shelfos.sh — a stable name and a group the service is in.\n%s\n' "$rule" \
            | write_file "$UDEV_RULE" 644 "root:root"
        sudo_run udevadm control --reload
        sudo_run udevadm trigger --subsystem-match=usbmisc
        step_ok "group $DEPLOY_PRINTER_GROUP"
    fi
    # Say now whether the rule matched. A missing symlink here is far easier to
    # understand than a failed print later.
    if [ "$DRY_RUN" = 0 ] && [ ! -e /dev/shelfos-label ]; then
        warn "/dev/shelfos-label does not exist yet — plug the printer in, or check that it is out of Editor Lite mode (see README.md)"
    fi
}

readonly CADDY_MARKER="# Written by shelfos.sh. Add a second site and this file stops being replaced."

# Whether /etc/caddy/Caddyfile is ours to replace wholesale.
#
# Only when it is absent, or has nothing in it but comments, or carries our
# marker AND still declares exactly one site. Anything else gets a file of its
# own under sites/ — overwriting a Caddyfile that serves somebody else's site
# takes that site off the air, which is not a thing to do while installing
# something unrelated. "It mentions shelfos" is not enough: a file with three
# sites, one of them this one, mentions it too.
caddyfile_is_ours_alone() {
    [ -f "$CADDYFILE" ] || return 0
    grep -qv '^[[:space:]]*\(#.*\)\?$' "$CADDYFILE" 2>/dev/null || return 0
    head -1 "$CADDYFILE" 2>/dev/null | grep -qF "$CADDY_MARKER" || return 1
    # One top-level `… {` line is one site block.
    [ "$(grep -c '^[^[:space:]#].*{[[:space:]]*$' "$CADDYFILE" 2>/dev/null)" = 1 ]
}

deploy_step_caddy_config() {
    step "Caddy site"
    if [ "$DEPLOY_WANT_CADDY" = 0 ]; then
        step_skipped "no proxy"
        return 0
    fi
    local rendered
    rendered=$(sed \
        -e "s|^shelfos\.example\.com {|$DEPLOY_DOMAIN {|" \
        -e "s|reverse_proxy 127\.0\.0\.1:[0-9]*|reverse_proxy 127.0.0.1:$DEPLOY_PORT|" \
        "$REPO_ROOT/deploy/Caddyfile")

    if caddyfile_is_ours_alone; then
        printf '%s\n%s\n' "$CADDY_MARKER" "$rendered" | write_file "$CADDYFILE" 644 "root:root"
        step_ok
    else
        # Someone else's sites are in there. Overwriting would take them down,
        # which is not a thing to do while installing something unrelated.
        local site="/etc/caddy/sites/shelfos.caddy"
        sudo_run install -d -m 755 /etc/caddy/sites
        printf '%s\n' "$rendered" | write_file "$site" 644 "root:root"
        step_ok "written to $site"
        if ! grep -q 'import sites/' "$CADDYFILE" 2>/dev/null; then
            info ""
            warn "$CADDYFILE already serves other sites, so it was left alone."
            info "  Add this line to it, then reload Caddy:"
            info ""
            info "      import sites/*.caddy"
            info ""
            return 0
        fi
    fi
    if [ "$DRY_RUN" = 0 ] && command -v caddy > /dev/null 2>&1; then
        sudo_run caddy validate --config "$CADDYFILE" --adapter caddyfile > /dev/null \
            || die 1 "the Caddy configuration does not validate; nothing was reloaded"
    fi
    sudo_run systemctl reload caddy
}

deploy_step_verify() {
    step "checking it answers"
    if [ "$DRY_RUN" = 1 ]; then
        step_skipped "dry run"
        return 0
    fi
    if health_probe "$DEPLOY_PORT" 30; then
        step_ok
    else
        step_ok "no answer within 30 seconds"
        info ""
        warn "ShelfOS did not answer on 127.0.0.1:$DEPLOY_PORT within 30 seconds."
        info ""
        systemctl status "$SERVICE_NAME" --no-pager -l 2>&1 | head -20 >&2 || true
        info ""
        journalctl -u "$SERVICE_NAME" -n 30 --no-pager 2>&1 >&2 || true
        info ""
        info "The three checks that stop a production start on purpose are the default"
        info "secret key, an unset admin password on a fresh database, and an admin still"
        info "on the default password. Each says so in the log above."
        exit 1
    fi
    info ""
    info "${C_GREEN}ShelfOS is running.${C_OFF}"
    if [ "$DEPLOY_WANT_CADDY" = 1 ]; then
        info "  https://$DEPLOY_DOMAIN  (Caddy will get the certificate on the first request;"
        info "  the name has to resolve here and ports 80 and 443 have to be open — 80 too,"
        info "  because that is how the certificate is issued and renewed)"
    else
        info "  http://127.0.0.1:$DEPLOY_PORT  — put your own TLS in front of it"
    fi
    info "  ./shelfos.sh status     what state it is in"
    info "  ./shelfos.sh backup     take one now; nothing else does it for you"
}

# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------

usage_update() {
    cat <<'EOF'
Usage: sudo ./shelfos.sh update [options]

Move an installed ShelfOS forward: back it up, pull, reinstall dependencies if
they changed, restart, and check it answers.

  --ref REF      check out REF instead of fast-forwarding the current branch
  --no-backup    skip the backup taken first (it is the only undo there is)
  --no-restart   update the files but leave the running service alone
  --dry-run      print what would happen; never calls sudo
  -y, --yes      do not ask about the unit or the Caddyfile
EOF
}

cmd_update() {
    local ref="" want_backup=1 want_restart=1
    while [ $# -gt 0 ]; do
        case $1 in
            --ref)        ref=${2:-}; shift 2 ;;
            --no-backup)  want_backup=0; shift ;;
            --no-restart) want_restart=0; shift ;;
            -h|--help)    usage_update; return 0 ;;
            *)            usage_update >&2; die 2 "unknown option for update: $1" ;;
        esac
    done

    is_deployed || die 3 "ShelfOS is not installed here; use './shelfos.sh deploy' first"
    [ -d "$INSTALL_DIR/.git" ] || die 1 "$INSTALL_DIR has no git history (it was copied, not cloned), so there is nothing to pull; re-deploy from a clone"

    local port; port=$(installed_port)

    if [ "$want_backup" = 1 ]; then
        info "Taking a backup first — it is the only way back from a bad update."
        cmd_backup create || die 1 "the backup failed; not going any further"
    fi

    local dirty; dirty=$(sudo_run git -C "$INSTALL_DIR" status --porcelain 2>/dev/null || true)
    if [ -n "$dirty" ]; then
        warn "$INSTALL_DIR has local modifications:"
        printf '%s\n' "$dirty" >&2
        die 1 "refusing to update over hand-edited files; revert them or re-deploy"
    fi

    local before after
    before=$(sudo_run git -C "$INSTALL_DIR" rev-parse --short HEAD)
    sudo_run git -C "$INSTALL_DIR" fetch --quiet origin
    if [ -n "$ref" ]; then
        sudo_run git -C "$INSTALL_DIR" checkout --quiet "$ref"
    else
        sudo_run git -C "$INSTALL_DIR" merge --ff-only --quiet FETCH_HEAD \
            || die 1 "cannot fast-forward $INSTALL_DIR; look at it by hand"
    fi
    after=$(sudo_run git -C "$INSTALL_DIR" rev-parse --short HEAD)

    if [ "$before" = "$after" ]; then
        info "Already at $after; nothing to update."
    else
        info "$before → $after"
        sudo_run git -C "$INSTALL_DIR" --no-pager log --oneline "$before..$after" >&2 || true
    fi

    if sudo_run git -C "$INSTALL_DIR" diff --name-only "$before" "$after" 2>/dev/null | grep -q '^pyproject.toml$'; then
        info "Dependencies changed; reinstalling."
        sudo_run "$INSTALL_DIR/.venv/bin/pip" install --quiet --editable "$INSTALL_DIR"
        sudo_run touch "$INSTALL_DIR/.venv/.shelfos-installed"
    fi
    # Root's, not the service's — see deploy_step_venv.
    sudo_run chown -R root:root "$INSTALL_DIR"

    # The installed unit and Caddyfile carry this machine's port and domain, so
    # a new version of either cannot simply be copied over them.
    local f
    for f in deploy/shelfos.service deploy/Caddyfile; do
        if sudo_run git -C "$INSTALL_DIR" diff --name-only "$before" "$after" 2>/dev/null | grep -q "^$f$"; then
            warn "$f changed upstream; yours carries this machine's settings, so it was not touched."
            info "  Compare and apply by hand, or re-run deploy --reinstall."
        fi
    done

    if [ "$want_restart" = 1 ]; then
        sudo_run systemctl restart "$SERVICE_NAME"
        if health_probe "$port" 30; then
            info "${C_GREEN}Updated and answering.${C_OFF}"
        else
            warn "ShelfOS did not come back on port $port."
            journalctl -u "$SERVICE_NAME" -n 30 --no-pager >&2 2>&1 || true
            die 1 "restore the backup above with './shelfos.sh backup restore <archive>' if you need to go back"
        fi
    fi
}

# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------

usage_status() {
    cat <<'EOF'
Usage: ./shelfos.sh status

Report on the installed service, or on this clone when there is none.
Exit status: 0 healthy, 1 installed but not healthy, 3 not installed.
EOF
}

status_line() { printf '  %-16s %s\n' "$1" "$2"; }

cmd_status() {
    while [ $# -gt 0 ]; do
        case $1 in
            -h|--help) usage_status; return 0 ;;
            *)         usage_status >&2; die 2 "unknown option for status: $1" ;;
        esac
    done

    local healthy=0

    if is_deployed; then
        printf '%sInstalled service%s\n' "$C_BOLD" "$C_OFF"
        status_line "code" "$INSTALL_DIR"
        if [ -d "$INSTALL_DIR/.git" ]; then
            status_line "version" "$(git -C "$INSTALL_DIR" describe --always --dirty 2>/dev/null || echo unknown)"
        fi
        local active enabled
        active=$(systemctl is-active "$SERVICE_NAME" 2>/dev/null || true)
        enabled=$(systemctl is-enabled "$SERVICE_NAME" 2>/dev/null || true)
        status_line "service" "${active:-unknown} (${enabled:-unknown} at boot)"
        [ "$active" = active ] || healthy=1

        local port; port=$(installed_port)
        if health_probe "$port" 1; then
            status_line "health" "answering on 127.0.0.1:$port"
        else
            status_line "health" "no answer on 127.0.0.1:$port"
            healthy=1
        fi

        if [ -f "$ENV_FILE_SYSTEM" ]; then
            local mode owner
            mode=$(stat -c '%a' "$ENV_FILE_SYSTEM" 2>/dev/null || echo '?')
            owner=$(stat -c '%U:%G' "$ENV_FILE_SYSTEM" 2>/dev/null || echo '?')
            status_line "settings" "$ENV_FILE_SYSTEM ($owner $mode)"
            # Never prints a value — only whether one is still a placeholder.
            if grep -q 'replace-me' "$ENV_FILE_SYSTEM" 2>/dev/null; then
                status_line "" "${C_YELLOW}still contains 'replace-me'${C_OFF}"
                healthy=1
            fi
        else
            status_line "settings" "missing ($ENV_FILE_SYSTEM)"
            healthy=1
        fi

        local db; db=$(env_file_value "$ENV_FILE_SYSTEM" DATABASE_URL)
        db=${db#sqlite:///}
        db=${db:-$DATA_DIR/shelfos.db}
        if [ -f "$db" ]; then
            status_line "database" "$db ($(du -h "$db" 2>/dev/null | cut -f1), changed $(date -r "$db" '+%Y-%m-%d %H:%M' 2>/dev/null))"
        else
            status_line "database" "$db (does not exist yet)"
        fi
        local att; att=$(env_file_value "$ENV_FILE_SYSTEM" SHELFOS_ATTACHMENTS_DIR)
        att=${att:-$DATA_DIR/attachments}
        if [ -d "$att" ]; then
            status_line "attachments" "$(find "$att" -maxdepth 1 -type f 2>/dev/null | wc -l) files in $att"
        fi

        if command -v caddy > /dev/null 2>&1; then
            status_line "caddy" "$(systemctl is-active caddy 2>/dev/null || echo 'not running')"
        else
            status_line "caddy" "not installed"
        fi

        if [ "$healthy" != 0 ]; then
            printf '\n%sLast log lines%s\n' "$C_BOLD" "$C_OFF"
            journalctl -u "$SERVICE_NAME" -n 10 --no-pager 2>/dev/null | sed 's/^/  /' || \
                printf '  (no access to the journal; try with sudo)\n'
        fi
        return "$healthy"
    fi

    printf '%sNo installed service%s\n' "$C_BOLD" "$C_OFF"
    status_line "checked" "$SERVICE_PATH, $INSTALL_DIR"
    printf '\n%sThis clone%s\n' "$C_BOLD" "$C_OFF"
    status_line "path" "$REPO_ROOT"
    if [ -d "$REPO_ROOT/.git" ]; then
        status_line "version" "$(git -C "$REPO_ROOT" describe --always --dirty 2>/dev/null || echo unknown)"
    fi
    if [ -x "$REPO_ROOT/.venv/bin/uvicorn" ]; then
        status_line "virtualenv" "built"
    else
        status_line "virtualenv" "not built yet"
    fi
    if [ -f "$REPO_ROOT/data/shelfos.db" ]; then
        status_line "database" "data/shelfos.db ($(du -h "$REPO_ROOT/data/shelfos.db" 2>/dev/null | cut -f1))"
    else
        status_line "database" "none yet"
    fi
    printf '\n  Run it with ./shelfos.sh devel, or install it with sudo ./shelfos.sh deploy\n'
    return 3
}

# ---------------------------------------------------------------------------
# backup — scripts/backup.py, pointed at the right database as the right user.
# ---------------------------------------------------------------------------

usage_backup() {
    cat <<'EOF'
Usage: ./shelfos.sh backup [create] [-o PATH]
       ./shelfos.sh backup restore ARCHIVE [--yes] [--force]

Wrap scripts/backup.py with the paths and the user of whichever install is here.
For a deployed service that means running as the service user against
/var/lib/shelfos; for a clone, against this directory.

The archive holds the database and the attachments. It does NOT hold
/etc/shelfos/env, so a restore without that file comes back with a different
signing secret and every session invalid — back that file up separately.
EOF
}

# Hand the database and attachments back to the service after root has written
# them. Paths are resolved first: backup.py deliberately supports an attachments
# directory that is a symlink to external storage, and `chown -R` on a symlinked
# operand changes the link rather than the tree behind it — so without this the
# files stay root-owned and the service starts unable to write uploads.
give_data_back() {
    local db_path att_path
    db_path=$(readlink -f "${DEPLOY_DB#sqlite:///}" 2>/dev/null || printf '%s' "${DEPLOY_DB#sqlite:///}")
    att_path=$(readlink -f "$DEPLOY_ATT" 2>/dev/null || printf '%s' "$DEPLOY_ATT")
    sudo_run chown -R "$SERVICE_USER:$SERVICE_USER" "$db_path" "$att_path"
}

cmd_backup() {
    local action="create"
    case ${1:-} in
        create|restore) action=$1; shift ;;
        -h|--help)      usage_backup; return 0 ;;
    esac

    local py script db att
    local -a env_args=()
    if is_deployed; then
        py="$INSTALL_DIR/.venv/bin/python"
        script="$INSTALL_DIR/scripts/backup.py"
        db=$(env_file_value "$ENV_FILE_SYSTEM" DATABASE_URL)
        att=$(env_file_value "$ENV_FILE_SYSTEM" SHELFOS_ATTACHMENTS_DIR)
        db=${db:-sqlite:///$DATA_DIR/shelfos.db}
        att=${att:-$DATA_DIR/attachments}
        env_args=("DATABASE_URL=$db" "SHELFOS_ATTACHMENTS_DIR=$att")
        [ -x "$py" ] || die 1 "no virtualenv at $py"
    else
        py="$REPO_ROOT/.venv/bin/python"
        script="$REPO_ROOT/scripts/backup.py"
        [ -x "$py" ] || die 1 "no virtualenv here; run './shelfos.sh devel' once to build it"
    fi

    # -y is eaten by the global flag parser, so a documented
    # `backup restore snap.tar.gz --yes` would otherwise reach backup.py without
    # it and sit on an input() prompt — with the service already stopped.
    if [ "$ASSUME_YES" = 1 ] && [ "$action" = restore ]; then
        set -- "$@" --yes
    fi

    # An archive named relatively is about to be read by a process that may not
    # share this working directory, and named in messages that outlive it. The
    # archive is not always the first argument — `restore --force snap.tar.gz`
    # is a reasonable thing to type — so find the first one that is not a flag.
    if [ "$action" = restore ]; then
        local -a resolved=()
        local found=0 arg dir
        for arg in "$@"; do
            if [ "$found" = 0 ]; then
                case $arg in
                    -*) ;;
                    *)
                        dir=$(cd "$(dirname "$arg")" 2>/dev/null && pwd) \
                            || die 1 "no such directory: $(dirname "$arg")"
                        # Without the check above this silently becomes
                        # "/<basename>": cd fails, the substitution is empty, and
                        # a typo in the directory turns into a path at the root
                        # that the operator never typed — and might even exist.
                        arg="$dir/$(basename "$arg")"
                        found=1 ;;
                esac
            fi
            resolved+=("$arg")
        done
        set -- "${resolved[@]+"${resolved[@]}"}"
    fi

    if [ "$action" = create ] && is_deployed; then
        local has_output=0 arg
        for arg in "$@"; do
            # Every spelling argparse accepts, not only the spaced ones: with
            # --output=PATH missed, an appended -o would win (argparse takes the
            # last) and the archive would quietly land somewhere else.
            case $arg in
                -o|--output|--output=*|-o=*) has_output=1 ;;
            esac
        done
        if [ "$has_output" = 0 ]; then
            # root:root 0700. The archives are root's business, like
            # /etc/shelfos/env: each one carries every password hash in the
            # database, and the service itself has no reason to read them back.
            sudo_run install -d -o root -g root -m 700 "$DATA_DIR/backups"
            set -- "$@" -o "$DATA_DIR/backups/shelfos-backup-$(date +%Y%m%d-%H%M%S).tar.gz"
        fi
    fi

    if [ "$action" = restore ] && is_deployed; then
        info "Stopping the service so nothing writes to the database mid-restore."
        sudo_run systemctl stop "$SERVICE_NAME"
    fi

    local status=0
    if is_deployed; then
        # As root, not as the service user. Stepping down to it means giving up
        # root's right to traverse directories, and an archive normally sits in
        # the operator's home — 0750 on Ubuntu, so the service user cannot enter
        # it whatever the archive's own mode is. That failure reads as
        # "Permission denied" on a file that is plainly world-readable, and no
        # amount of chmod on it helps.
        sudo_run env "${env_args[@]}" "$py" "$script" "$action" "$@" || status=$?
    else
        run "$py" "$script" "$action" "$@" || status=$?
    fi

    if [ "$action" = restore ] && is_deployed; then
        # Unconditionally, not only on success. A restore that fails part way
        # has usually already replaced the database — backup.py swaps it before
        # the attachments — so the failure path is exactly when root-owned data
        # is left behind, and the one where nobody thinks to check ownership.
        # It costs nothing when nothing changed.
        give_data_back
        sudo_run systemctl start "$SERVICE_NAME"
    fi

    if [ "$action" = create ] && [ "$status" = 0 ]; then
        note "The archive does not contain $ENV_FILE_SYSTEM — back that up separately."
        if is_deployed; then
            note "It is owned by root and readable only with sudo; it holds every password hash."
        fi
    fi
    return "$status"
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

usage() {
    cat <<EOF
ShelfOS $SHELFOS_VERSION

Usage: ./shelfos.sh <command> [options]

  devel     run a local instance from this clone, reloading on edits
  deploy    install as a system service, with TLS (needs root)
  update    move an installed service forward, backing it up first
  status    what is installed, and whether it is healthy
  backup    create or restore a backup of whichever install is here

Options that work anywhere:
  --dry-run   print what would happen and change nothing (never calls sudo)
  -y, --yes   take the default answer to every question
  -q, --quiet only errors
  -h, --help  this, or a command's own help with './shelfos.sh <command> --help'
  --version   print the version

  ./shelfos.sh devel                    a local copy on http://127.0.0.1:9000
  ./shelfos.sh devel --port 9001        a second one, from a second clone
  sudo ./shelfos.sh deploy              a real install, asking what it needs
  sudo ./shelfos.sh deploy --dry-run    the same, previewed
EOF
}

main() {
    resolve_repo_root

    local command=""
    local -a rest=()
    while [ $# -gt 0 ]; do
        case $1 in
            --dry-run)   DRY_RUN=1; shift ;;
            -y|--yes)    ASSUME_YES=1; shift ;;
            -q|--quiet)  QUIET=1; shift ;;
            --version)   printf 'ShelfOS %s\n' "$SHELFOS_VERSION"; return 0 ;;
            -h|--help)   if [ -z "$command" ]; then usage; return 0; fi; rest+=("$1"); shift ;;
            -*)          if [ -z "$command" ]; then usage >&2; die 2 "unknown option: $1"; fi; rest+=("$1"); shift ;;
            *)           if [ -z "$command" ]; then command=$1; else rest+=("$1"); fi; shift ;;
        esac
    done

    case $command in
        "")       usage; return 0 ;;
        devel)    cmd_devel "${rest[@]+"${rest[@]}"}" ;;
        deploy)   cmd_deploy "${rest[@]+"${rest[@]}"}" ;;
        update)   cmd_update "${rest[@]+"${rest[@]}"}" ;;
        status)   cmd_status "${rest[@]+"${rest[@]}"}" ;;
        backup)   cmd_backup "${rest[@]+"${rest[@]}"}" ;;
        *)        usage >&2; die 2 "unknown command: $command" ;;
    esac
}

main "$@"
