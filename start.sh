#!/bin/sh
# Folio group server. Source of truth: the "finance" project of mk30; copy changes here and restart.
cd "$(dirname "$0")"
exec ./.venv/bin/python app.py
