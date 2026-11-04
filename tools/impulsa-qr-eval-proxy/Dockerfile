# Reuse the API sandbox image already built on the VPS. No install or pull.
FROM escalar-qr-api-eval:068d8f4
WORKDIR /proxy
COPY --chown=10001:10001 fixed-api-proxy.py /proxy/fixed-api-proxy.py
USER 10001:10001
HEALTHCHECK --interval=15s --timeout=5s --retries=3 CMD python -c "import urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8769/api/v1/health',timeout=3); assert r.status==200"
ENTRYPOINT ["python", "/proxy/fixed-api-proxy.py"]
