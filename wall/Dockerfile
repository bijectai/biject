# biject-wall — a single static page behind nginx.
# The page is self-contained (inline CSS+JS, system fonts, no CDNs), so the
# image is nginx plus two files.
FROM nginx:alpine

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY wall.html /usr/share/nginx/html/wall.html

EXPOSE 80
