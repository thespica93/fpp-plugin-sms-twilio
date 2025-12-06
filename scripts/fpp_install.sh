#!/bin/bash
###############################################################################
# FPP SMS Twilio Plugin - Installation Script
###############################################################################

# Make this script executable (GitHub strips permissions)
chmod +x "$0" 2>/dev/null || true

LOG="/home/fpp/media/logs/sms_plugin_install.log"

# Function to log and display
log_and_show() {
    echo "$1" | tee -a "$LOG"
}

log_and_show "========================================"
log_and_show "FPP SMS Twilio Plugin Installer"
log_and_show "$(date)"
log_and_show "========================================"

# Install pip3 if needed
if ! command -v pip3 &> /dev/null; then
    log_and_show "Installing pip3..."
    apt-get update -qq >> "$LOG" 2>&1
    DEBIAN_FRONTEND=noninteractive apt-get install -y python3-pip >> "$LOG" 2>&1
fi

# Install packages with explicit waits
log_and_show "Installing Flask..."
pip3 install --break-system-packages --no-cache-dir flask==3.0.0 >> "$LOG" 2>&1
log_and_show "Flask complete"

log_and_show "Installing Twilio (this may take 60+ seconds)..."
# Twilio has many dependencies, run without timeout
pip3 install --break-system-packages --no-cache-dir twilio==8.10.0 >> "$LOG" 2>&1
TWILIO_EXIT=$?
if [ $TWILIO_EXIT -ne 0 ]; then
    log_and_show "ERROR: Twilio installation failed with exit code $TWILIO_EXIT"
    exit 1
fi
log_and_show "Twilio complete"

log_and_show "Installing Requests..."
pip3 install --break-system-packages --no-cache-dir requests==2.31.0 >> "$LOG" 2>&1
log_and_show "Requests complete"

# Create directories
mkdir -p /home/fpp/media/config /home/fpp/media/logs

# Create config files if they don't exist
if [ ! -f "/home/fpp/media/config/blacklist.txt" ]; then
cat > /home/fpp/media/config/blacklist.txt << 'EOF'
fuck
shit
damn
hell
ass
bitch
crap
bastard
piss
EOF
fi

[ ! -f "/home/fpp/media/config/whitelist.txt" ] && touch /home/fpp/media/config/whitelist.txt
[ ! -f "/home/fpp/media/config/blocked_phones.json" ] && echo "[]" > /home/fpp/media/config/blocked_phones.json

# Set permissions
chown -R fpp:fpp /home/fpp/media/config /home/fpp/media/logs 2>/dev/null

# Create service log file with proper permissions
touch /home/fpp/media/logs/sms_plugin.log
chmod 666 /home/fpp/media/logs/sms_plugin.log
chown fpp:fpp /home/fpp/media/logs/sms_plugin.log

log_and_show "========================================"
log_and_show "Installation complete!"
log_and_show "Restart FPPD to start the service"
log_and_show "========================================"

exit 0
