#!/bin/bash
# FPP SMS Twilio Plugin - Activate
# Enables the plugin, starts SMS polling, and starts the default waiting playlist.
# Add this to the FPP Scheduler: Command → Run Script → TwilioStart
curl -s -X POST http://127.0.0.1:5000/api/activate
