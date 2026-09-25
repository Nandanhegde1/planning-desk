FROM python:3.12-slim

# Port 7860 suits both targets: Azure Container Apps takes --target-port 7860,
# and Hugging Face Spaces runs containers as uid 1000 and expects that port.
RUN useradd -m -u 1000 user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    MPLCONFIGDIR=/home/user/.matplotlib \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR $HOME/app

COPY --chown=user requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -r requirements.txt

COPY --chown=user . .
RUN mkdir -p outputs .cache && chown -R user:user $HOME

USER user
# Build matplotlib's font cache into the image rather than on the first chart,
# which on a small shared CPU is a visible stall.
RUN python -c "import matplotlib; matplotlib.use('Agg'); import matplotlib.pyplot"
EXPOSE 7860

# Liveness only. /api/health reports 503 when the model provider is
# unconfigured, which is a config problem rather than a dead container.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,os,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','7860')+'/api/health/live',timeout=4).status==200 else 1)"

# One worker is a correctness requirement, not a cost setting: the session store
# and the MCP child processes live in this process. See HOSTING.md.
# --proxy-headers so the app sees the real scheme and client IP behind ingress.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-7860} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
