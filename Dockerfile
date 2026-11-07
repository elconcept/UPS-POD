FROM mcr.microsoft.com/playwright/python:v1.56.0-noble
WORKDIR /app
COPY requirements-web.txt /app/
RUN pip install --no-cache-dir -r requirements-web.txt
COPY UPS_POD/ups_pod.py UPS_POD/ustawienia.ini /app/
COPY web/app.py web/scheduler.py /app/
RUN useradd -u 10001 -m -s /usr/sbin/nologin upspod
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 UPS_POD_HOME=/data PLAYWRIGHT_BROWSERS_PATH=/ms-playwright TZ=Europe/Warsaw
USER 10001:10001
EXPOSE 8080
CMD ["gunicorn","--workers","1","--threads","4","--bind","0.0.0.0:8080","--timeout","120","--access-logfile","-","app:app"]
