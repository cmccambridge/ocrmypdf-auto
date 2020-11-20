FROM ubuntu:20.04 as base

FROM base as builder

ENV LANG=C.UTF-8

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        autoconf \
        automake \
        build-essential \
        ca-certificates \
        curl \
        libleptonica-dev \
        libtool \
        zlib1g-dev \
    && mkdir src \
    && cd src \
    && curl -L https://github.com/agl/jbig2enc/archive/ea6a40a2cbf05efb00f3418f2d0ad71232565beb.tar.gz --output jbig2.tgz \
    && tar xzf jbig2.tgz --strip-components=1 \
    && ./autogen.sh \
    && ./configure \
    && make \
    && make install

FROM base

ENV LANG=C.UTF-8

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ghostscript \
        gosu \
        liblept5 \
        pngquant \
        python3-venv \
        python3-pip \
        qpdf \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-osd \
        unpaper \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv --system-site-packages /appenv \
    && . /appenv/bin/activate \
    && pip install --upgrade pip

# Copy jbig2 from builder image
COPY --from=builder /usr/local/bin/ /usr/local/bin/
COPY --from=builder /usr/local/lib/ /usr/local/lib/

# Pull in ocrmypdf via requirements.txt and install pinned version
COPY src/requirements.txt /app/

RUN . /appenv/bin/activate; \
    pip install -r /app/requirements.txt

COPY src/ /app/

# Create restricted privilege user docker:docker to drop privileges
# to later. We retain root for the entrypoint in order to install
# additional tesseract OCR language packages.
RUN groupadd -g 1000 docker && \
    useradd -u 1000 -g docker -N --home-dir /app docker && \
    mkdir /config /input /output /ocrtemp /archive && \
    chown -Rh docker:docker /app /config /input /output /ocrtemp /archive && \
    chmod 755 /app/docker-entrypoint.sh

VOLUME ["/config", "/input", "/output", "/ocrtemp", "/archive"]

ENTRYPOINT ["/app/docker-entrypoint.sh"]

