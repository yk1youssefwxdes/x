#!/bin/bash
set -e

echo "=================================================="
echo "  Starting School ERP Production Services"
echo "=================================================="

# 1. Start Node.js WhatsApp automation service in background
if [ -d "whatsapp_service" ]; then
    echo "[*] Launching WhatsApp automation background service..."
    (cd whatsapp_service && node server.js) &
    WA_PID=$!
    echo "[✔] WhatsApp service started with PID $WA_PID"
fi

# 2. Start Gunicorn web server for Django in foreground
echo "[*] Starting Gunicorn web server on port ${PORT:-8000}..."
exec gunicorn school_erp.wsgi --bind 0.0.0.0:${PORT:-8000} --workers 2 --log-file -
