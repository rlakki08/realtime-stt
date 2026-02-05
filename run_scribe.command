#!/bin/bash
# Get the directory where this script is located
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Navigate to the project directory
cd "$DIR"

# Activate the virtual environment
source .venv/bin/activate

# Run the background transcriber
# Using --no-type is NOT set, so it will type by default
echo "🚀 Starting Scribe STT..."
echo "📂 Working directory: $DIR"
echo "🔑 Permissions: This script runs in Terminal. Ensure Terminal has Accessibility permissions."
echo ""

python background_transcriber.py
