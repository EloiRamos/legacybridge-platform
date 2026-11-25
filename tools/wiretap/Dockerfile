# LBF wire-tap — recording/fault proxy for HALDEN (test profile only). Python 3.12 standard library only.
FROM python:3.12-slim-trixie@sha256:191083cdf6c7bda8c76158983163ead5d5cf7b77cf3033e805bb0f5296f51b4b
# CVE-2026-124851 (2026-11-24): fixed in Debian's security archive, not yet in the published base; fails if it is not applied
RUN apt-get update -qq && apt-get install -y -qq --only-upgrade libzstd1 && rm -rf /var/lib/apt/lists/* \
    && dpkg --compare-versions "$(dpkg-query -W -f='${Version}' libzstd1)" ge 1.5.7+dfsg-1+deb13u1
# nothing installs packages at run time
RUN pip uninstall -y -q --root-user-action=ignore pip && rm -rf /usr/local/lib/python3.12/ensurepip
RUN useradd --system --uid 10002 --home-dir /opt/wiretap wiretap \
 && mkdir -p /wiretap /opt/wiretap && chown wiretap:wiretap /wiretap
WORKDIR /opt/wiretap
COPY wiretap.py ./wiretap.py
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER wiretap
EXPOSE 8090 8099
HEALTHCHECK --interval=3s --timeout=2s --retries=20 CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8099/_wiretap/health',timeout=1); sys.exit(0)"
ENTRYPOINT ["python", "/opt/wiretap/wiretap.py"]
