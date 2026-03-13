#!/bin/bash
###############################################################################
# FPP SMS Twilio Plugin - Installation Script
###############################################################################

# Source FPP common functions and set FPPDIR environment
. ${FPPDIR}/scripts/common

# Create directories FIRST before any logging to files
mkdir -p /home/fpp/media/config /home/fpp/media/logs

LOG="/home/fpp/media/logs/sms_plugin_install.log"

# Log to both file and stdout so FPP UI shows progress
log_and_show() {
    echo "$1" | tee -a "$LOG"
}

log_and_show "========================================"
log_and_show "FPP SMS Twilio Plugin Installer"
log_and_show "$(date)"
log_and_show "========================================"
log_and_show ""
log_and_show "NOTE: Installation can take 3-5 minutes."
log_and_show "Please do not close this window."
log_and_show ""

# Install pip3 if needed
if ! command -v pip3 &> /dev/null; then
    log_and_show "Installing pip3... please wait"
    apt-get update -qq >> "$LOG" 2>&1
    DEBIAN_FRONTEND=noninteractive apt-get install -y python3-pip >> "$LOG" 2>&1
fi

# Install packages
log_and_show "[1/4] Installing Flask... please wait"
pip3 install --break-system-packages --no-cache-dir flask==3.0.0 >> "$LOG" 2>&1
log_and_show "[1/4] Flask complete"

log_and_show "[2/4] Installing Twilio... please wait (this is the slow one)"
pip3 install --break-system-packages --no-cache-dir twilio==8.10.0 >> "$LOG" 2>&1
TWILIO_EXIT=$?
if [ $TWILIO_EXIT -ne 0 ]; then
    log_and_show "ERROR: Twilio installation failed with exit code $TWILIO_EXIT"
    exit 1
fi
log_and_show "[2/4] Twilio complete"

log_and_show "[3/4] Installing Requests... please wait"
pip3 install --break-system-packages --no-cache-dir requests==2.31.0 >> "$LOG" 2>&1
log_and_show "[3/4] Requests complete"

log_and_show "[4/4] Installing Pillow (image rendering)... please wait"
pip3 install --break-system-packages --no-cache-dir pillow >> "$LOG" 2>&1
log_and_show "[4/4] Pillow complete"

# Create config files if they don't exist
[ ! -f "/home/fpp/media/config/blocked_phones.json" ] && echo "[]" > /home/fpp/media/config/blocked_phones.json

# whitelist.txt and blacklist.txt ship with the plugin via git.
# Force git checkout to ensure they are present (FPP update may not pull all files).
PLUGIN_DIR="/home/fpp/media/plugins/fpp-plugin-sms-twilio"
cd "$PLUGIN_DIR" && git checkout -- whitelist.txt blacklist.txt >> "$LOG" 2>&1
if [ ! -f "$PLUGIN_DIR/whitelist.txt" ]; then
    log_and_show "WARNING: whitelist.txt still missing after git checkout - creating empty file"
    touch "$PLUGIN_DIR/whitelist.txt"
fi
if [ ! -f "$PLUGIN_DIR/blacklist.txt" ]; then
    log_and_show "WARNING: blacklist.txt still missing after git checkout - creating empty file"
    touch "$PLUGIN_DIR/blacklist.txt"
fi
chown fpp:fpp "$PLUGIN_DIR/whitelist.txt" "$PLUGIN_DIR/blacklist.txt" 2>/dev/null
chmod 664 "$PLUGIN_DIR/whitelist.txt" "$PLUGIN_DIR/blacklist.txt" 2>/dev/null

# Set permissions on config/logs directories
chown -R fpp:fpp /home/fpp/media/config /home/fpp/media/logs 2>/dev/null
touch /home/fpp/media/logs/sms_plugin.log
chmod 666 /home/fpp/media/logs/sms_plugin.log
chown fpp:fpp /home/fpp/media/logs/sms_plugin.log

# Install scheduler scripts into FPP's scripts directory so they appear in
# the scheduler under: Command → Run Script → TwilioStart / TwilioStop
mkdir -p /home/fpp/media/scripts
cp "$PLUGIN_DIR/scripts/fpp_activate.sh"   /home/fpp/media/scripts/TwilioStart.sh
cp "$PLUGIN_DIR/scripts/fpp_deactivate.sh" /home/fpp/media/scripts/TwilioStop.sh
chmod +x /home/fpp/media/scripts/TwilioStart.sh /home/fpp/media/scripts/TwilioStop.sh
chown fpp:fpp /home/fpp/media/scripts/TwilioStart.sh /home/fpp/media/scripts/TwilioStop.sh
log_and_show "Scheduler scripts installed: TwilioStart.sh / TwilioStop.sh"

log_and_show "========================================"
log_and_show "Installation complete!"
log_and_show "Restart FPPD to start the service"
log_and_show "========================================"

# Restart the plugin service if it's already running (e.g. during an update)
if pgrep -f sms_plugin.py > /dev/null 2>&1; then
    log_and_show "Restarting SMS plugin service..."
    pkill -f sms_plugin.py 2>/dev/null || true
    sleep 1
    PLUGIN_DIR="/home/fpp/media/plugins/fpp-plugin-sms-twilio"
    setsid su fpp -c "cd '$PLUGIN_DIR' && nohup python3 sms_plugin.py > /dev/null 2>/home/fpp/media/logs/sms_plugin.log &" < /dev/null > /dev/null 2>&1
    log_and_show "SMS plugin service restarted"
fi

# Trigger the "FPPD Restart Required" banner in FPP's UI
setSetting "restartFlag" "1"

# No errors — remove the install log, it's only useful for debugging failures
rm -f "$LOG"

exit 0
