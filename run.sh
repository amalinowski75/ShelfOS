#!/bin/sh

. .venv/bin/activate
. ~/.ShelfOS/.env
uvicorn app.main:app --reload --port 9000
