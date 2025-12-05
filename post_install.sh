#!/bin/bash
###############################################################################
# FPP SMS Twilio Plugin - Post Install Script
# FPP automatically runs this after cloning the repository
###############################################################################

echo "Running post-install setup for FPP SMS Twilio Plugin..."

# Get the directory where this script is located
PLUGIN_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

cd "$PLUGIN_DIR"

# Run the main installer
if [ -f "install.sh" ]; then
    echo "Running install.sh..."
    bash install.sh
else
    echo "ERROR: install.sh not found!"
    exit 1
fi

# Start the plugin service
echo "Starting SMS Twilio Plugin service..."
pkill -f sms_plugin.py 2>/dev/null || true
sleep 1
nohup python3 "$PLUGIN_DIR/sms_plugin.py" > /home/fpp/media/logs/sms_plugin.log 2>&1 &

echo ""
echo "✅ FPP SMS Twilio Plugin installed and started!"
echo "📱 Access the web interface at: http://YOUR_FPP_IP:5000"
echo ""

exit 0
