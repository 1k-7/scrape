#!/usr/bin/env bash
# exit on error
set -o errexit

# --- Install Python Dependencies ---
echo "Installing Python packages from requirements.txt..."
pip install -r requirements.txt

# --- Install Google Chrome ---
# We need to install the actual Chrome browser for Selenium to use.
echo "Installing Google Chrome..."
apt-get update
apt-get install -y wget gnupg

# Add Google's official GPG key
wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | apt-key add -

# Set up the repository
sh -c 'echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" >> /etc/apt/sources.list.d/google-chrome.list'

# Update APT and install Chrome
apt-get update
apt-get install -y google-chrome-stable

echo "Build setup complete. Google Chrome and Python packages are installed."
