#!/usr/bin/env bash
set -euo pipefail
cd /home/nurkhat/wms-backend
git pull origin main
.venv/bin/pip install -r requirements.txt -q
.venv/bin/alembic upgrade head
sudo systemctl restart wms-api wms-worker
echo "Deploy complete: $(git rev-parse --short HEAD)"
sudo systemctl status wms-api wms-worker --no-pager -l
