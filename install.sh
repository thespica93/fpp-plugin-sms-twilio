#!/bin/bash
###############################################################################
# FPP SMS Twilio Plugin - Dependency Installer
# This script is called by FPP when the plugin is installed
###############################################################################

echo "Installing FPP SMS Twilio Plugin dependencies..."

# Check if pip3 is installed, if not, install it
if ! command -v pip3 &> /dev/null; then
    echo "pip3 not found, installing..."
    apt-get update -qq
    apt-get install -y python3-pip
    echo "pip3 installed successfully!"
fi

# Install Python packages with --break-system-packages flag for Debian 12+
echo "Installing Flask..."
pip3 install --break-system-packages flask==3.0.0 2>&1 | grep -v "already satisfied" || true

echo "Installing Twilio..."
pip3 install --break-system-packages twilio==8.10.0 2>&1 | grep -v "already satisfied" || true

echo "Installing Requests..."
pip3 install --break-system-packages requests==2.31.0 2>&1 | grep -v "already satisfied" || true

echo "Dependencies installed successfully!"

# Create config directory if it doesn't exist
mkdir -p /home/fpp/media/config
mkdir -p /home/fpp/media/logs

# Create default blacklist if it doesn't exist
if [ ! -f "/home/fpp/media/config/blacklist.txt" ]; then
    echo "Creating default blacklist.txt..."
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
    echo "Default blacklist created!"
fi

# Create empty whitelist if it doesn't exist
if [ ! -f "/home/fpp/media/config/whitelist.txt" ]; then
    echo "Creating empty whitelist.txt..."
    touch /home/fpp/media/config/whitelist.txt
    echo "Empty whitelist created!"
fi

# Create empty blocklist if it doesn't exist
if [ ! -f "/home/fpp/media/config/blocked_phones.json" ]; then
    echo "Creating empty blocked_phones.json..."
    echo "[]" > /home/fpp/media/config/blocked_phones.json
    echo "Empty blocklist created!"
fi

# Set permissions
chown -R fpp:fpp /home/fpp/media/config 2>/dev/null || true
chown -R fpp:fpp /home/fpp/media/logs 2>/dev/null || true

echo ""
echo "✅ Plugin installation complete!"
echo ""
echo "Next steps:"
echo "1. Enable the plugin in FPP Plugin Manager"
echo "2. Access web interface at: http://YOUR_FPP_IP:5000"
echo "3. Configure your Twilio settings"
echo ""
