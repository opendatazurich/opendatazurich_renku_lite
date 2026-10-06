# Stage 1: build the static JupyterLite site.
FROM python:3.12-slim-bookworm AS build
# Node is only needed to compile the JupyterLab extension (jlpm ships with jupyterlab).
COPY --from=node:22-bookworm-slim /usr/local/bin/node /usr/local/bin/node
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /src

COPY build/requirements.txt build/
RUN pip install -r build/requirements.txt

COPY extension/odz-launcher extension/odz-launcher
RUN pip install --no-build-isolation ./extension/odz-launcher

COPY opendata_lite opendata_lite
COPY build/wheels.txt build/
RUN pip wheel --no-deps -w wheels ./opendata_lite \
 && pip download --no-deps --only-binary=:all: -d wheels -r build/wheels.txt

COPY jupyter_lite_config.json jupyter-lite.json overrides.json ./
COPY content content
RUN jupyter lite build --output-dir /site

# Stage 2: serve it. Non-root nginx listening on 8080 (Renku app port).
FROM nginxinc/nginx-unprivileged:1.29-alpine
# Lets the entrypoint render ${NGINX_LOCAL_RESOLVERS} from /etc/resolv.conf
# (cluster DNS on Kubernetes) for the /proxy/ upstreams.
ENV NGINX_ENTRYPOINT_LOCAL_RESOLVERS=1
COPY nginx/templates/ /etc/nginx/templates/
COPY --from=build /site /usr/share/nginx/html
EXPOSE 8080
