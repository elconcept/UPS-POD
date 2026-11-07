FROM mcr.microsoft.com/playwright/python:v1.56.0-noble
ARG REVISION=unknown
LABEL org.opencontainers.image.revision=$REVISION
WORKDIR /app
COPY requirements-web.txt /app/
RUN python -m pip install --no-cache-dir -r requirements-web.txt
COPY UPS_POD/ups_pod.py /app/ups_pod.py
COPY web/app.py web/service.py web/worker.py /app/
COPY web/templates/ /app/templates/
COPY web/static/ /app/static/
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin upspod
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app HOME=/tmp PLAYWRIGHT_BROWSERS_PATH=/ms-playwright UPS_POD_HOME=/data
USER 10001:10001
EXPOSE 8080
CMD ["gunicorn","--workers","1","--threads","4","--bind","0.0.0.0:8080","--timeout","120","--access-logfile","-","app:app"]
