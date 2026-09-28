# Botfusions Voice Agent — üretim imajı
FROM python:3.11-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8090
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY server ./server
COPY widget ./widget
COPY agents ./agents
RUN useradd -m app && mkdir -p /app/data && chown -R app /app/data
USER app
EXPOSE 8090
HEALTHCHECK CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8090\")}/health')" || exit 1
CMD ["sh", "-c", "uvicorn server.app:app --host 0.0.0.0 --port ${PORT} --proxy-headers"]
