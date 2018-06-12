#!/bin/bash
set -e

. /appenv/bin/activate
cd /app
exec ocrmypdf "$@"

