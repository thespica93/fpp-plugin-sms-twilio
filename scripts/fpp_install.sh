#!/bin/bash
###############################################################################
# FPP SMS Twilio Plugin - Installation Script
###############################################################################

PLUGIN_DIR="/home/fpp/media/plugins/fpp-plugin-sms-twilio"
LOG_FILE="/home/fpp/media/logs/sms_plugin_install.log"

exec > >(tee -a $LOG_FILE) 2>&1

echo "========================================"
echo "FPP SMS Twilio Plugin Installer"
echo "========================================"

# Install pip3
if ! command -v pip3 &> /dev/null; then
    echo "Installing pip3..."
    apt-get update -qq
    apt-get install -y python3-pip
fi

# Install packages one at a time with retries
install_package() {
    local package=$1
    local max_attempts=3
    local attempt=1
    
    while [ $attempt -le $max_attempts ]; do
        echo "Installing $package (attempt $attempt/$max_attempts)..."
        
        if timeout 120 pip3 install --break-system-packages --no-cache-dir $package; then
            echo "✅ $package installed successfully"
            return 0
        fi
        
        echo "⚠️  $package install failed, retrying..."
        attempt=$((attempt + 1))
        sleep 2
    done
    
    echo "❌ Failed to install $package after $max_attempts attempts"
    return 1
}

# Install each package
install_package "flask==3.0.0" || exit 1
install_package "twilio==8.10.0" || exit 1
install_package "requests==2.31.0" || exit 1

# Create directories
echo "Creating directories..."
mkdir -p /home/fpp/media/config
mkdir -p /home/fpp/media/logs

# Create config files
echo "Creating config files..."
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
chown -R fpp:fpp /home/fpp/media/config 2>/dev/null || true
chown -R fpp:fpp /home/fpp/media/logs 2>/dev/null || true

# Stop any existing service
echo "Stopping any existing service..."
pkill -f sms_plugin.py 2>/dev/null || true
sleep 2

# Prepare log file
touch /home/fpp/media/logs/sms_plugin.log
chmod 666 /home/fpp/media/logs/sms_plugin.log
chown fpp:fpp /home/fpp/media/logs/sms_plugin.log

# Start service
echo "Starting SMS Twilio service..."
cd "$PLUGIN_DIR"
su fpp -c "cd '$PLUGIN_DIR' && nohup python3 sms_plugin.py > /home/fpp/media/logs/sms_plugin.log 2>&1 &"
sleep 5

# Verify
if pgrep -f sms_plugin.py > /dev/null; then
    echo "========================================"
    echo "✅ Installation complete!"
    echo "Service is running on port 5000"
    echo "========================================"
    exit 0
else
    echo "========================================"
    echo "❌ Service failed to start"
    echo "Check: /home/fpp/media/logs/sms_plugin.log"
    echo "========================================"
    exit 1
fi
