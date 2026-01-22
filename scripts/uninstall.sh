#!/bin/bash
set -euo pipefail

# playtimed uninstaller

INSTALL_DIR="/opt/playtimed"
CONFIG_DIR="/etc/playtimed"
DATA_DIR="/var/lib/playtimed"
SYSTEMD_DIR="/etc/systemd/system"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info() { echo -e "${GREEN}[INFO]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }

# Check root
[[ $EUID -eq 0 ]] || { echo "Must run as root"; exit 1; }

# Stop and disable service
if systemctl is-active --quiet playtimed 2>/dev/null; then
    info "Stopping playtimed service..."
    systemctl stop playtimed
fi

if systemctl is-enabled --quiet playtimed 2>/dev/null; then
    info "Disabling playtimed service..."
    systemctl disable playtimed
fi

# Remove systemd service
if [[ -f "$SYSTEMD_DIR/playtimed.service" ]]; then
    info "Removing systemd service..."
    rm -f "$SYSTEMD_DIR/playtimed.service"
    systemctl daemon-reload
fi

# Remove CLI wrapper
if [[ -f /usr/local/bin/playtimed ]]; then
    info "Removing CLI..."
    rm -f /usr/local/bin/playtimed
fi

# Remove installation directory
if [[ -d "$INSTALL_DIR" ]]; then
    info "Removing $INSTALL_DIR..."
    rm -rf "$INSTALL_DIR"
fi

# Ask about config and data
echo ""
read -p "Remove config directory $CONFIG_DIR? [y/N] " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    rm -rf "$CONFIG_DIR"
    info "Config removed"
else
    warn "Config preserved at $CONFIG_DIR"
fi

read -p "Remove data directory $DATA_DIR (includes activity history)? [y/N] " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    rm -rf "$DATA_DIR"
    info "Data removed"
else
    warn "Data preserved at $DATA_DIR"
fi

info "Uninstall complete!"
