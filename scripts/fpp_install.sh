#!/bin/bash
###############################################################################
# FPP SMS Twilio Plugin - Installation Script
###############################################################################

LOG="/home/fpp/media/logs/sms_plugin_install.log"
exec > >(tee -a "$LOG") 2>&1

echo "========================================"
echo "FPP SMS Twilio Plugin Installer"
echo "$(date)"
echo "========================================"

# Install pip3 if needed
if ! command -v pip3 &> /dev/null; then
    echo "Installing pip3..."
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y python3-pip
fi

# Install packages with explicit waits
echo "Installing Flask..."
pip3 install --break-system-packages --no-cache-dir flask==3.0.0
echo "Flask complete"

echo "Installing Twilio (this may take 60+ seconds)..."
# Twilio has many dependencies, run without timeout
pip3 install --break-system-packages --no-cache-dir twilio==8.10.0 2>&1 | tee -a "$LOG"
TWILIO_EXIT=$?
if [ $TWILIO_EXIT -ne 0 ]; then
    echo "ERROR: Twilio installation failed with exit code $TWILIO_EXIT"
    exit 1
fi
echo "Twilio complete"

echo "Installing Requests..."
pip3 install --break-system-packages --no-cache-dir requests==2.31.0
echo "Requests complete"

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

echo "========================================"
echo "Installation complete!"
echo "Restart FPPD to start the service"
echo "========================================"

exit 0
