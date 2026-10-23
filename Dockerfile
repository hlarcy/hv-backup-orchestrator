FROM python:3.12-slim

# Optional Kerberos support for PSRP (auth: kerberos). NTLM/negotiate works without it.
ARG KERBEROS=0
RUN if [ "$KERBEROS" = "1" ]; then \
      apt-get update && apt-get install -y --no-install-recommends gcc libkrb5-dev krb5-user && \
      rm -rf /var/lib/apt/lists/*; fi

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    HVBO_CONFIG=/config/config.yaml HVBO_DATA_DIR=/data TZ=Europe/London

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    if [ "$KERBEROS" = "1" ]; then pip install --no-cache-dir "pypsrp[kerberos]==0.9.1"; fi

COPY app ./app
RUN useradd -r -u 10001 hvbo && mkdir -p /data /config && chown -R hvbo /data
USER hvbo

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz')"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--forwarded-allow-ips", "*"]
