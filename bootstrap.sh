#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

find_python() {
    if command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' 2>/dev/null; then
        command -v python3
    elif command -v python >/dev/null 2>&1 && python -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' 2>/dev/null; then
        command -v python
    fi
}

as_root() {
    if [ "$(id -u)" -eq 0 ]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo "$@"
    else
        printf '%s\n' "Python installation requires root privileges or sudo." >&2
        exit 1
    fi
}

PYTHON=$(find_python || true)
if [ -z "$PYTHON" ]; then
    case "$(uname -s)" in
        Darwin)
            if ! command -v brew >/dev/null 2>&1; then
                printf '%s\n' "Homebrew is required when Python is missing: https://brew.sh" >&2
                exit 1
            fi
            brew install python
            ;;
        Linux)
            if [ -f /etc/arch-release ]; then
                as_root pacman -S --needed --noconfirm python
            elif command -v apt-get >/dev/null 2>&1; then
                as_root apt-get update
                as_root apt-get install -y python3
            else
                printf '%s\n' "Unsupported Linux package manager. Install Python 3.10+ and retry." >&2
                exit 1
            fi
            ;;
        *)
            printf '%s\n' "Unsupported operating system. Install Python 3.10+ and run dotai.py directly." >&2
            exit 1
            ;;
    esac
    PYTHON=$(find_python || true)
fi

if [ -z "$PYTHON" ]; then
    printf '%s\n' "Python 3.10+ was not found after installation." >&2
    exit 1
fi

exec "$PYTHON" "$ROOT/dotai.py" install "$@"
