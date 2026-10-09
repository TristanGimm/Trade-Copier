FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home copier && mkdir /app/data && chown copier:copier /app/data
COPY --chown=copier:copier server.py /app/server.py
COPY --chown=copier:copier copier /app/copier
COPY --chown=copier:copier public /app/public
USER copier
ENV DATABASE_PATH=/app/data/copier.db PORT=8080 PYTHONUNBUFFERED=1
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"
CMD ["python", "server.py"]
