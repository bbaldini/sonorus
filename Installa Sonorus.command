#!/bin/bash
# Doppio clic in macOS: installa Sonorus
cd "$(dirname "$0")" && ./install.sh
read -n 1 -s -r -p "Premi un tasto per chiudere"
