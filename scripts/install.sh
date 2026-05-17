#!/usr/bin/env bash
# Cogitum Linux/macOS installer
# Usage: curl -fsSL https://raw.githubusercontent.com/StarryCod/cogitum/master/scripts/install.sh | bash

set -e

REPO="https://github.com/StarryCod/cogitum.git"
INSTALL_DIR="${HOME}/.local/share/cogitum"
VENV_DIR="${INSTALL_DIR}/.venv"
BIN_DIR="${HOME}/.local/bin"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

info()  { echo -e "${GREEN}[cogitum]${NC} $*"; }
warn()  { echo -e "${YELLOW}[cogitum]${NC} $*"; }
error() { echo -e "${RED}[cogitum]${NC} $*"; }

check_python() {
    if command -v python3.11 &>/dev/null; then
        PYTHON="python3.11"
    elif command -v python3 &>/dev/null; then
        local ver
        ver=$(python3 --version 2>&1 | awk '{print $2}')
        if [[ "${ver%%.*}" -ge 3 && "${ver#*.}" -ge 11 ]]; then
            PYTHON="python3"
        else
            error "Python 3.11+ required. Found: ${ver}"
            error "Install Python 3.11+ and try again."
            exit 1
        fi
    else
        error "Python 3.11+ not found."
        error "Install Python 3.11+ (e.g. sudo apt install python3.11 python3.11-venv)"
        exit 1
    fi
    info "Using Python: $($PYTHON --version)"
}

ensure_bin_dir() {
    mkdir -p "${BIN_DIR}"
    case ":${PATH}:" in
        *":${BIN_DIR}:"*) ;;
        *)
            warn "${BIN_DIR} is not in your PATH."
            warn "Add this to your shell config (~/.bashrc or ~/.zshrc):"
            warn "  export PATH=\"\$HOME/.local/bin:\$PATH\""
            ;;
    esac
}

install_cogitum() {
    info "Installing Cogitum to ${INSTALL_DIR} ..."
    mkdir -p "${INSTALL_DIR}"

    if [ -d "${INSTALL_DIR}/.git" ]; then
        info "Existing install found. Pulling latest ..."
        git -C "${INSTALL_DIR}" pull --ff-only
    else
        info "Cloning repository ..."
        git clone --depth 1 "${REPO}" "${INSTALL_DIR}"
    fi

    if [ ! -d "${VENV_DIR}" ]; then
        info "Creating virtual environment ..."
        "${PYTHON}" -m venv "${VENV_DIR}"
    fi

    info "Installing dependencies ..."
    "${VENV_DIR}/bin/pip" install --upgrade pip -q
    "${VENV_DIR}/bin/pip" install -e "${INSTALL_DIR}[all]" -q

    info "Linking binaries ..."
    ln -sf "${VENV_DIR}/bin/cogitum" "${BIN_DIR}/cogitum"
    ln -sf "${VENV_DIR}/bin/cog" "${BIN_DIR}/cog"

    info "Done! Run 'cog' or 'cogitum' to start."
    info "First-time setup: run 'cog setup' to configure providers."
}

main() {
    echo "⚔️  Cogitum Installer"
    echo ""
    check_python
    ensure_bin_dir
    install_cogitum
}

main "$@"
