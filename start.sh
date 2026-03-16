#!/bin/bash
set -e
cd /root/autoreporte

if [ ! -d "venv" ]; then
    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
else
    source venv/bin/activate
fi

uvicorn main:app --host 0.0.0.0 --port 8001 --reload
