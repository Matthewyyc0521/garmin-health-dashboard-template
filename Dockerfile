FROM python:3.13-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY requirements-garmin.txt ./
RUN python -m pip install --no-cache-dir -r requirements-garmin.txt
COPY sync_garmin.py ./
USER 65534:65534
ENTRYPOINT ["python", "sync_garmin.py"]
