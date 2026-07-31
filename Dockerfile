FROM ubuntu:24.04 AS compiler

LABEL maintainer="Kacper Kowalik <xarthisius.kk@gmail.com>"

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=en_US.UTF-8 \
    LC_ALL=C.UTF-8

RUN apt-get update && apt-get install -qy \
    gcc \
    gosu \
    libpython3-dev \
    git \
    libldap2-dev \
    libsasl2-dev \
    libacl1-dev \
    libcairo2 \
    python3-pip \
    python3-venv \
    curl \
    libmagic-dev \
&& python3 -m venv /venv \
&& apt-get clean && rm -rf /var/lib/apt/lists/* \
&& . /venv/bin/activate \
&& python3 -m pip install --upgrade --no-cache-dir \
    pip \
    setuptools \
    setuptools_scm \
    build \
    wheel \
    gunicorn

RUN curl -sL https://deb.nodesource.com/setup_22.x | bash - && \
    apt-get install -qy nodejs

ENV PATH=/venv/bin:$PATH \
  VIRTUAL_ENV=/venv

COPY . /src/
RUN cd /src && \
  cd girder_sivacor/web_client && \
  npm install && \
  npm run build && \
  cd ../../ && \
  python -m build .

# Temporary OAuth
# RUN cd /tmp && \
#  git clone https://github.com/xarthisius/girder -b auth_email_events && \
#  cd /tmp/girder && \
#  git checkout auth_email_events && \
#  cd girder/web && \
#  npm i && \
#  npm run build && \
#  cd ../../ && \
#  python -m build . && \
#  cp dist/* /src/dist/ && \
#  cd /tmp && rm -rf girder

RUN python -m pip wheel --wheel-dir=/src/dist pylibacl

FROM python:3.12-slim

LABEL maintainer="Kacper Kowalik <xarthisius.kk@gmail.com>"

ENV DEBIAN_FRONTEND=noninteractive \
  LANG=en_US.UTF-8 \
  LC_ALL=C.UTF-8

RUN apt-get update -qy \
  && apt-get install -yq --no-install-recommends \
    tini \
    git \
    libcairo2 \
    libmagic1 \
    libmagic-mgc \
    libacl1 \
    gnupg \
  && apt-get clean \
  && rm -rf /var/lib/apt/lists/*

RUN python3 -m pip install --no-cache-dir \
  'celery>=5.5.2' \
  'girder>=5.0.12' \
  'girder-oauth>=5.0.12' \
  'girder-user-quota>=5.0.12'

# Copy the virtual environment from the compiler stage
COPY --from=compiler /src/dist /src/dist

RUN python3 -m pip install \
  --no-cache-dir \
  /src/dist/*.whl && \
  # Ensure all dependencies are installed
  python3 -m pip check || true

RUN python3 -m pip install --no-cache-dir gunicorn uvicorn[standard] uvicorn-worker
# The baked docker GID is only a fallback, and it is NOT reliable: a fresh JS2
# Ubuntu 24.04 host came up with 127, not 112. Anything that needs the docker socket
# must override the group at RUN time, because the group baked here cannot match
# every host:
#   docker run   ->  --group-add "$(getent group docker | cut -d: -f3)"
#   Swarm        ->  user: "1000:<host docker gid>"   (group_add is rejected by
#                    `docker stack config`, so the primary group is the only lever)
# Overridable at build time for a host whose GID is known: --build-arg DOCKER_GID=127
ARG DOCKER_GID=112
RUN groupadd -g 1000 girder \
 && groupadd -g "${DOCKER_GID}" docker \
 && useradd -g 1000 -G "${DOCKER_GID}" -u 1000 -m -s /bin/bash girder

EXPOSE 8080

USER girder
ENTRYPOINT ["/usr/bin/tini", "--", "gunicorn", "--workers", "4", "--worker-class", "uvicorn.workers.UvicornWorker", "--bind", "0.0.0.0:8080", "--worker-connections", "1000", "girder_sivacor.asgi:app"]
