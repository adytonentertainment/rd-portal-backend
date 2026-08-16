#!/bin/bash

# PRO Audit Tool - Run Script

cd "$(dirname "$0")"

# Check if venv exists
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
fi

# Activate venv
source venv/bin/activate

# Install dependencies
echo "Installing dependencies..."
pip install -r requirements.txt

# Install Playwright browsers
echo "Installing Playwright browsers..."
playwright install chromium

echo ""
echo "================================"
echo "  PRO Audit Tool"
echo "  Running on http://localhost:8080"
echo "================================"
echo ""

# Run server
python server.py
