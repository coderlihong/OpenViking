# Build from the repository root. Production runs use the image digest in lock.json.
FROM debian:bookworm-slim@sha256:3783cc01769c7b2b1b83a5c5ad96c815348e28ed7da68e2e3687004faa906251
ENV DEBIAN_FRONTEND=noninteractive PYTHONPATH=/opt/openviking
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-writer libreoffice-calc libreoffice-impress fontconfig fonts-dejavu-core fonts-liberation fonts-noto-cjk python3 python3-pip \
    && python3 -m pip install --break-system-packages --no-cache-dir PyMuPDF==1.26.7 defusedxml==0.7.1 \
    && mkdir -p /opt/openviking/openviking/parse /opt/openviking/openviking_cli \
    && touch /opt/openviking/openviking/__init__.py /opt/openviking/openviking/parse/__init__.py /opt/openviking/openviking_cli/__init__.py \
    && dpkg-query -W > /opt/openviking/packages.lock \
    && rm -rf /var/lib/apt/lists/*
COPY openviking/parse/office_converter.py openviking/parse/office_layout.py /opt/openviking/openviking/parse/
COPY openviking_cli/exceptions.py /opt/openviking/openviking_cli/exceptions.py
COPY deploy/office-converter/convert.py /opt/openviking/convert.py
USER 10001:10001
ENTRYPOINT ["python3", "/opt/openviking/convert.py"]
