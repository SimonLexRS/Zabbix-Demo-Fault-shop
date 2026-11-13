# Build context: the repository root (compose.yaml), so the Zabbix templates go inside the image.
# Files are copied, not bind-mounted: Dokploy re-clones the code on every deploy and a bind mount of a container that
# is not recreated would keep pointing at the deleted folder.
FROM python:3.13-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY python/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY python/*.py ./
COPY zabbix/*.yaml ./zabbix/
