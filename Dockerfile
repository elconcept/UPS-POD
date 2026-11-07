FROM mcr.microsoft.com/playwright/python:v1.56.0-noble
ARG REVISION=unknown
LABEL org.opencontainers.image.revision=$REVISION
WORKDIR /app
COPY requirements-web.txt /app/
RUN python -m pip install --no-cache-dir -r requirements-web.txt
COPY UPS_POD/ups_pod.py /app/ups_pod.py
COPY ups_web/ups-logo.svg /app/ups-logo.svg
COPY web/app.py web/service.py web/config_store.py web/gmail_oauth.py web/ups_localization.py web/support_mail.py web/worker.py web/supervisor.py /app/
COPY web/templates/ /app/templates/
COPY web/static/ /app/static/
RUN useradd --uid 1031 --gid 100 --no-create-home --shell /usr/sbin/nologin upspod
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app HOME=/tmp PLAYWRIGHT_BROWSERS_PATH=/ms-playwright UPS_POD_HOME=/data
USER 1031:100
EXPOSE 8080
CMD ["python","/app/supervisor.py"]
