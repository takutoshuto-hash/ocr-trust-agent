FROM python:3.12-slim

WORKDIR /srv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080

COPY pyproject.toml ./
COPY app ./app
COPY data/master ./data/master
COPY data/master/formats ./data/master/formats
COPY models ./models
COPY eval/out/curve_*.csv eval/out/summary_*.json ./eval/out/
COPY data/measurement/results_cond2_after.csv ./data/measurement/

RUN pip install --no-cache-dir . && mkdir -p /srv/models

EXPOSE 8080
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
