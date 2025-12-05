#!/bin/bash
###############################################################################
# FPP SMS Twilio Plugin - Dependency Installer
# This script is called by FPP when the plugin is installed
###############################################################################

echo "Installing FPP SMS Twilio Plugin dependencies..."

# Install Python packages with --break-system-packages flag for Debian 12+
pip3 install --break-system-packages flask==3.0.0 2>&1
pip3 install --break-system-packages twilio==8.10.0 2>&1
pip3 install --break-system-packages requests==2.31.0 2>&1

echo "Dependencies installed successfully!"

# Create config directory if it doesn't exist
mkdir -p /home/fpp/media/config
mkdir -p /home/fpp/media/logs

# Set permissions
chown -R fpp:fpp /home/fpp/media/config
chown -R fpp:fpp /home/fpp/media/logs

echo "Plugin ready to use!"
