#!/bin/bash
set -euo pipefail

# playtimed installer
# Creates isolated venv at /opt/playtimed, installs package, sets up systemd

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
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# Check root
[[ $EUID -eq 0 ]] || error "Must run as root"

# Check Python
command -v python3 >/dev/null || error "Python 3 not found"
PYTHON_VERSION=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
info "Found Python $PYTHON_VERSION"

# Get script directory (where the package source is)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
info "Installing from $SCRIPT_DIR"

# Create directories
info "Creating directories..."
mkdir -p "$INSTALL_DIR"
mkdir -p "$CONFIG_DIR"
mkdir -p "$DATA_DIR"

# Create venv
info "Creating virtual environment..."
python3 -m venv "$INSTALL_DIR/venv"
source "$INSTALL_DIR/venv/bin/activate"

# Install package
info "Installing playtimed..."
pip install --upgrade pip wheel
pip install "$SCRIPT_DIR"

# Copy source for reference (optional, helps with debugging)
cp -r "$SCRIPT_DIR/src/playtimed" "$INSTALL_DIR/src"

# Create symlink for CLI
info "Creating CLI symlink..."
ln -sf "$INSTALL_DIR/venv/bin/python" "$INSTALL_DIR/python"
cat > /usr/local/bin/playtimed << 'WRAPPER'
#!/bin/bash
exec /opt/playtimed/venv/bin/python -m playtimed.main "$@"
WRAPPER
chmod +x /usr/local/bin/playtimed

# Install config if not exists
if [[ ! -f "$CONFIG_DIR/config.yaml" ]]; then
    info "Installing default config..."
    cp "$SCRIPT_DIR/config.example.yaml" "$CONFIG_DIR/config.yaml"
    warn "Edit $CONFIG_DIR/config.yaml to configure users and limits"
else
    info "Config already exists, not overwriting"
fi

# Install systemd service
info "Installing systemd service..."
cat > "$SYSTEMD_DIR/playtimed.service" << 'SERVICE'
[Unit]
Description=Screen time daemon with personality
After=network.target

[Service]
Type=simple
ExecStart=/opt/playtimed/venv/bin/python -m playtimed.main run -c /etc/playtimed/config.yaml
Restart=always
RestartSec=10
Environment=PYTHONPATH=/opt/playtimed/src

# Security hardening
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/var/lib/playtimed
PrivateTmp=true

[Install]
WantedBy=multi-user.target
SERVICE

# Reload systemd
systemctl daemon-reload

info "Installation complete!"
echo ""
echo "Next steps:"
echo "  1. Configure users: playtimed user add <username> --gaming-limit 120"
echo "  2. Review config:   $CONFIG_DIR/config.yaml"
echo "  3. Start daemon:    systemctl start playtimed"
echo "  4. Enable on boot:  systemctl enable playtimed"
echo "  5. Check status:    playtimed status <username>"
echo ""
echo "Logs: journalctl -u playtimed -f"
