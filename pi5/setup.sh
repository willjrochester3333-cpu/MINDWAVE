#!/usr/bin/env bash
# setup.sh — one-shot installer for the MindWave Pi 5 build.
#
# Run this ON THE PI, after copying/cloning this repo onto it:
#     cd mindwave/pi5
#     chmod +x setup.sh
#     ./setup.sh
#
# What it does:
#   1. Enables I2C and SPI (needed for the OLED and LED strip)
#   2. Installs the system + Python packages main.py needs
#   3. Creates config.py from config.py.example if it doesn't exist yet
#   4. Installs and enables the systemd service so main.py auto-starts on boot
#
# It's safe to re-run — re-running just re-applies the same steps.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CURRENT_USER="$(whoami)"

echo "== MindWave Pi 5 setup =="
echo "Installing from: $SCRIPT_DIR"
echo "As user: $CURRENT_USER"
echo

echo "-- Enabling I2C and SPI --"
sudo raspi-config nonint do_i2c 0
sudo raspi-config nonint do_spi 0

echo "-- Installing system packages --"
sudo apt-get update
sudo apt-get install -y python3-pip python3-venv i2c-tools

echo "-- Installing Python packages --"
pip install --break-system-packages \
    luma.oled Pillow adafruit-blinka adafruit-circuitpython-neopixel-spi gpiozero rpi-lgpio

echo "-- Checking config.py --"
if [ ! -f "$SCRIPT_DIR/config.py" ]; then
    cp "$SCRIPT_DIR/config.py.example" "$SCRIPT_DIR/config.py"
    echo "  Created config.py from the example."
    echo "  >>> EDIT IT NOW with your laptop's IP address: nano $SCRIPT_DIR/config.py"
else
    echo "  config.py already exists — leaving it alone."
fi

echo "-- Installing the auto-start service --"
sudo tee /etc/systemd/system/mindwave-pi5.service > /dev/null <<EOF
[Unit]
Description=MindWave Pi 5 display (OLED + LED strip + buzzer)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=/usr/bin/python3 $SCRIPT_DIR/main.py
WorkingDirectory=$SCRIPT_DIR
User=$CURRENT_USER
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable mindwave-pi5.service

echo
echo "== Done =="
echo "1. Make sure config.py has your laptop's real IP address (see above)."
echo "2. Wire up the OLED, LED strip, and buzzer — see the WIRING section at"
echo "   the top of main.py."
echo "3. Reboot to apply the I2C/SPI changes and start the service:"
echo "     sudo reboot"
echo "4. After reboot, check it's running:"
echo "     sudo systemctl status mindwave-pi5.service"
echo "     journalctl -u mindwave-pi5.service -f"
