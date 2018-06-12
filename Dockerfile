FROM ubuntu:18.04

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ghostscript \
        python3-venv \
        python3-pip \
        qpdf \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-osd \
        unpaper \
    && rm -rf /var/lib/apt/lists/*

ENV LANG=C.UTF-8

RUN python3 -m venv --system-site-packages /appenv

RUN . /appenv/bin/activate; \
    pip install --upgrade pip

# Pull in ocrmypdf via requirements.txt and install pinned version
COPY requirements.txt /app/

RUN . /appenv/bin/activate; \
    pip install -r /app/requirements.txt

COPY . /app/

VOLUME ["/input", "/output"]

ENTRYPOINT ["/app/docker-entrypoint.sh"]

