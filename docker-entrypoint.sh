#!/bin/bash
set -e

umask 000

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

# With thanks adopted from http://github.com/danielquinn/paperless to work with ubuntu
install_languages() {
    local langs="$1"
    read -ra langs <<<"$langs"

    # Check that it is not empty
    if [ ${#langs[@]} -eq 0 ]; then
        return
    fi

    # Loop over languages to be installed
    for lang in "${langs[@]}"; do
        pkg="tesseract-ocr-$lang"

        # English is installed by default
        if [ "$lang" ==  "eng" ]; then
            continue
        fi

        if ! apt show "$pkg" > /dev/null 2>&1; then
            continue
        fi

        apt-get --no-upgrade -q install "$pkg"
    done
}

if [[ "$1" != "/"* ]]; then
    initialize

    # Install additional languages if specified
    if [ ! -z "$OCR_LANGUAGES"  ]; then
        install_languages "$OCR_LANGUAGES"
    fi

    . /appenv/bin/activate
    cd /app
    exec gosu docker python3 ocrmypdf-auto.py "$@"
fi

exec "$@"

