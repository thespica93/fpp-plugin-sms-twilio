#!/bin/bash
###############################################################################
# FPP SMS Twilio Plugin - FPP Installation Script
# FPP automatically calls this from scripts/fpp_install.sh
###############################################################################

# Make this script executable (in case Git stripped permissions)
chmod +x "$0" 2>/dev/null || true

PLUGIN_DIR="/home/fpp/media/plugins/fpp-plugin-sms-twilio"
LOG_FILE="/home/fpp/media/logs/sms_plugin_install.log"

echo "================================" | tee $LOG_FILE
echo "FPP SMS Twilio Plugin Installer" | tee -a $LOG_FILE
echo "================================" | tee -a $LOG_FILE

# STEP 1: Install pip3
echo "Installing pip3..." | tee -a $LOG_FILE
if ! command -v pip3 &> /dev/null; then
    apt-get update -qq >> $LOG_FILE 2>&1
    apt-get install -y python3-pip >> $LOG_FILE 2>&1
fi

# STEP 2: Install Python packages
echo "Installing Python packages..." | tee -a $LOG_FILE
pip3 install --break-system-packages flask==3.0.0 >> $LOG_FILE 2>&1
pip3 install --break-system-packages twilio==8.10.0 >> $LOG_FILE 2>&1
pip3 install --break-system-packages requests==2.31.0 >> $LOG_FILE 2>&1

# STEP 3: Create directories
echo "Creating directories..." | tee -a $LOG_FILE
mkdir -p /home/fpp/media/config
mkdir -p /home/fpp/media/logs

# STEP 4: Create config files
echo "Creating config files..." | tee -a $LOG_FILE
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

if [ ! -f "/home/fpp/media/config/whitelist.txt" ]; then
    touch /home/fpp/media/config/whitelist.txt
fi

if [ ! -f "/home/fpp/media/config/blocked_phones.json" ]; then
    echo "[]" > /home/fpp/media/config/blocked_phones.json
fi

# STEP 5: Set permissions
chown -R fpp:fpp /home/fpp/media/config 2>/dev/null || true
chown -R fpp:fpp /home/fpp/media/logs 2>/dev/null || true

# STEP 6: Start the service
echo "Starting service..." | tee -a $LOG_FILE
pkill -f sms_plugin.py 2>/dev/null || true
sleep 2

# Create log file with proper permissions
touch /home/fpp/media/logs/sms_plugin.log
chown fpp:fpp /home/fpp/media/logs/sms_plugin.log

# Start service as fpp user
su -c "nohup python3 '$PLUGIN_DIR/sms_plugin.py' > /home/fpp/media/logs/sms_plugin.log 2>&1 &" fpp
sleep 3

# STEP 7: Verify
if ps aux | grep -v grep | grep sms_plugin.py > /dev/null; then
    echo "✅ Installation complete! Service running on port 5000" | tee -a $LOG_FILE
    exit 0
else
    echo "❌ Service failed to start. Check /home/fpp/media/logs/sms_plugin.log" | tee -a $LOG_FILE
    exit 1
fi
