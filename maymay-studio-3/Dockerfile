FROM node:24.22.1-bookworm-slim AS ui
WORKDIR /ui
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build
FROM python:3.12.14-slim-bookworm
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT=8000 DATA_DIR=/tmp/maymay
WORKDIR /app
COPY requirements-web.lock ./
RUN pip install --no-cache-dir -r requirements-web.lock && useradd --uid 10001 --create-home studio
COPY maymay/ ./maymay/
COPY --from=ui /ui/dist ./frontend/dist/
USER studio
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import os,urllib.request;urllib.request.urlopen('http://127.0.0.1:'+os.getenv('PORT','8000')+'/healthz',timeout=4)"
CMD ["sh","-c","exec uvicorn maymay.api:create_app --factory --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers"]
