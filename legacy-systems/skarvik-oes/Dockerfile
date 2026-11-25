# SKARVIK OES 4 simulator — Python 3.12 standard library only.
FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
RUN useradd --system --uid 10001 --home-dir /opt/skarvik skarvik \
 && mkdir -p /exchange /data && chown skarvik:skarvik /exchange /data
WORKDIR /opt/skarvik
COPY src ./src
COPY seed ./seed
ENV PYTHONPATH=/opt/skarvik/src PYTHONDONTWRITEBYTECODE=1 SKARVIK_SEED_FILE=/opt/skarvik/seed/seed.json
USER skarvik
HEALTHCHECK --interval=5s --timeout=3s --retries=12 CMD python -c "import os,sys,time; p='/data/heartbeat'; sys.exit(0 if os.path.exists(p) and time.time()-float(open(p).read() or 0)<15 else 1)"
CMD ["python", "-m", "skarvik"]
