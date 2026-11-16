#!/bin/sh
# Copy the app from this folder (the master copy) to the Folio group server and restart it.
set -e
cd "$(dirname "$0")"
cp app.py pyproject.toml start.sh /srv/shared/folio/
cp -r static /srv/shared/folio/
chmod -R g+w /srv/shared/folio/app.py /srv/shared/folio/static 2>/dev/null || true
box shared restart folio
for i in $(seq 1 40); do curl -s -o /dev/null http://127.0.0.1:19001/api/me && break; sleep 1; done
curl -s -o /dev/null -w "Folio group server answered: %{http_code} (401 = up, sign-in required)\n" http://127.0.0.1:19001/api/me
