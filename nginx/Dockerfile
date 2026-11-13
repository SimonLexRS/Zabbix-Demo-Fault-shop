# Build context: the repository root. Pages and nginx template inside the image (no bind mounts, see python/Dockerfile).
FROM nginx:1.29-alpine
COPY web/ /usr/share/nginx/html/
COPY nginx/default.conf.template /etc/nginx/templates/default.conf.template
