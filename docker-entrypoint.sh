#!/bin/bash
set -e

# Source: https://github.com/danielquinn/paperless/
map_uidgid() {
    USERMAP_ORIG_UID=$(id -u docker)
    USERMAP_ORIG_GID=$(id -g docker)
    USERMAP_NEW_UID=${USERMAP_UID:-$USERMAP_ORIG_UID}
    USERMAP_NEW_GID=${USERMAP_GID:-${USERMAP_ORIG_GID:-$USERMAP_NEW_UID}}
    if [[ ${USERMAP_NEW_UID} != "${USERMAP_ORIG_UID}" || ${USERMAP_NEW_GID} != "${USERMAP_ORIG_GID}" ]]; then
        echo "Mapping UID and GID for docker:docker to $USERMAP_NEW_UID:$USERMAP_NEW_GID"
        groupmod -o -g "${USERMAP_NEW_GID}" docker
        usermod -o -g "${USERMAP_NEW_GID}" -u "${USERMAP_NEW_UID}" docker
    fi
}

ensure_tempdir() {
    TARGET_DIR=${OCR_TEMP_DIR:-/temp}
    mkdir -p "${TARGET_DIR}"
}

populate_defaults() {
    if [[ ! -f /config/ocr.config ]]; then
        cp /app/ocr.config /config/ocr.config
        chown docker:docker /config/ocr.config
    fi
}

initialize() {
    map_uidgid
    ensure_tempdir
    populate_defaults
}

if [[ "$1" != "/"* ]]; then
    initialize

    . /appenv/bin/activate
    cd /app
    exec gosu docker python3 ocrmypdf-auto.py "$@"
fi

exec "$@"

