FROM ubuntu:20.10 as base

FROM base as builder

ENV LANG=C.UTF-8

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ghostscript \
        gosu \
        build-essential autoconf automake libtool \
        libleptonica-dev \
        zlib1g-dev \
        ca-certificates \
        curl \
        git \
        python3-venv \
        python3-pip \
        qpdf \
        pngquant \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-osd \
        unpaper \
    && rm -rf /var/lib/apt/lists/*

# Get the latest pip (Ubuntu version doesn't support manylinux2010)
RUN \
  curl https://bootstrap.pypa.io/get-pip.py | python3

# Compile and install jbig2
# Needs libleptonica-dev, zlib1g-dev
RUN \
  mkdir jbig2 \
  && curl -L https://github.com/agl/jbig2enc/archive/ea6a40a.tar.gz | \
  tar xz -C jbig2 --strip-components=1 \
  && cd jbig2 \
  && ./autogen.sh && ./configure && make && make install \
  && cd .. \
  && rm -rf jbig2


COPY src/requirements.txt /app/
RUN python3 -m venv --system-site-packages /appenv
RUN . /appenv/bin/activate; \
    pip install -r /app/requirements.txt


### Begin Runtime Image
FROM base

ENV LANG=C.UTF-8

RUN apt-get update && apt-get install -y --no-install-recommends \
  ghostscript \
  img2pdf \
  liblept5 \
  libsm6 libxext6 libxrender-dev \
  zlib1g \
  pngquant \
  python3 \
  python3-venv \
  qpdf \
  gosu \
  tesseract-ocr \
  tesseract-ocr-deu \
  tesseract-ocr-eng \
  unpaper

WORKDIR /app

COPY --from=builder /usr/local/lib/ /usr/local/lib/
COPY --from=builder /usr/local/bin/ /usr/local/bin/
COPY --from=builder /appenv /appenv

RUN python3 -m venv --system-site-packages /appenv

RUN . /appenv/bin/activate;

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