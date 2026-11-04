FROM python:3.11-slim
WORKDIR /sandbox
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    ESCALAR_ENV_FILE_DISABLED=true ENVIRONMENT=test AUTO_CREATE_SCHEMA=false \
    DATABASE_URL=sqlite+pysqlite:///:memory: \
    ESCALAR_QR_SANDBOX_CONTAINER=1 ESCALAR_QR_SANDBOX_API_ROOT=/sandbox/api \
    UPLOAD_DIR=/tmp/qr-eval-uploads
COPY requirements.txt /sandbox/requirements.txt
RUN pip install --no-cache-dir -r /sandbox/requirements.txt \
    && groupadd --gid 10001 sandbox \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin sandbox
COPY api/app /sandbox/api/app
COPY api-sandbox.py /sandbox/api-sandbox.py
USER 10001:10001
EXPOSE 8769
HEALTHCHECK --interval=15s --timeout=5s --retries=3 CMD python -c "import urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8769/api/v1/health',timeout=3); assert r.status==200"
ENTRYPOINT ["python", "/sandbox/api-sandbox.py", "--serve", "--listen-host", "0.0.0.0", "--container-isolation", "--advertise-host", "impulsa-qr-api-eval", "--port", "8769", "--binding-file", "/run/eval/binding.json"]
