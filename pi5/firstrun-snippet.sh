# firstrun-snippet.sh — paste this into firstrun.sh on the boot partition
# to make the Pi fully self-install MindWave on its very first boot, with
# no SSH steps needed at all.
#
# HOW TO USE THIS
# ----------------
# 1. Flash the SD card with Raspberry Pi Imager as usual, using the
#    OS Customisation (gear icon / Ctrl+Shift+X) to set your hostname,
#    enable SSH, set a username/password, and enter your WiFi details.
#
# 2. Fill in the three placeholders below:
#      TARGET_USER  -> the exact username you set in Imager
#      LAPTOP_IP    -> your laptop's local IP (ipconfig on Windows)
#    (REPO_URL is already filled in.)
#
# 3. Before ejecting the SD card, open the drive that appears on your
#    laptop (it'll be called "bootfs" or "boot") and open firstrun.sh
#    in a text editor. Near the end of that file you'll find a line
#    that deletes the file itself, something like:
#        rm -f /boot/firstrun.sh
#    (it may say /boot/firmware/firstrun.sh instead — either is fine)
#    Paste everything below the line of dashes into firstrun.sh, on
#    its own line(s), just ABOVE that "rm -f ... firstrun.sh" line.
#    Save the file.
#
# 4. Eject the SD card, put it in the Pi, wire up the OLED/LED
#    strip/buzzer per pi5/main.py's docstring, and power it on.
#
# 5. Give it a few minutes on first boot (it needs to download
#    packages). It will reboot itself once when done. After that, the
#    MindWave service runs automatically every time it boots — for
#    good, no further setup needed.
#
# 6. If anything goes wrong, SSH is still enabled as a fallback (that's
#    what Imager's customisation set up) — SSH in and check the log:
#      cat /boot/mindwave-install.log
#    or /boot/firmware/mindwave-install.log on newer Raspberry Pi OS.
#    You can always finish by hand from there with ./setup.sh, exactly
#    as documented in main.py.
# ─────────────────────────────────────────────────────────────────────

(
  # Wait for the network to actually be up before trying to fetch anything.
  for i in $(seq 1 60); do
    ping -c1 -W2 8.8.8.8 >/dev/null 2>&1 && break
    sleep 2
  done

  REPO_URL="https://github.com/willjrochester3333-cpu/mindwave.git"
  TARGET_USER="PI_USERNAME"     # <-- same username you set in Imager
  LAPTOP_IP="192.168.1.100"     # <-- your laptop's local IP address
  TARGET_HOME="/home/$TARGET_USER"

  sudo -u "$TARGET_USER" git clone "$REPO_URL" "$TARGET_HOME/mindwave"
  cd "$TARGET_HOME/mindwave/pi5"
  sudo -u "$TARGET_USER" cp config.py.example config.py
  sudo -u "$TARGET_USER" sed -i "s/192.168.1.100/$LAPTOP_IP/" config.py
  chmod +x setup.sh
  sudo -u "$TARGET_USER" ./setup.sh
) > /boot/mindwave-install.log 2>&1
# (deliberately NOT backgrounded with "&" — firstrun.sh reboots
# automatically as soon as it finishes, so this has to block until the
# install is actually done, or the reboot could cut it off partway through)
