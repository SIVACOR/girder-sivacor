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

# The fleet controller runs from THIS image rather than its own python:3.12-slim
# one (worker_sizing_plan.md S3, P0.5). It assigns work through Girder's model
# layer in-process, so it needs girder, girder_worker and girder_sivacor
# importable -- which a standalone controller image cannot have without
# duplicating this entire install. `beat` and `local_worker` already share this
# image with different entrypoints; the controller becomes the third.
#
# openstacksdk is the only real addition: redis and pymongo already arrive with
# girder/celery. Verified 2026-08-13 -- `pip check` reports nothing broken
# against the full girder-sivacor requirement set with openstacksdk added.
#
# This also lands openstacksdk on every WORKER VM, because workers boot this
# same image. It is inert there -- clouds.yaml is bind-mounted only into the
# controller service, so a worker gets the library and no credentials -- but it
# is a deliberate decision rather than a side effect: a box running untrusted
# researcher code now carries an OpenStack client.
#
# Pinned by sha rather than tracking main, so girder-sivacor's own git decides
# what is in this image. An unpinned ref would let a merge in another repo
# silently change this build, which is the failure mode sivacor-autoscaler's own
# CI calls out by name. Bump deliberately; that repo's test suite gates the ref.
ARG AUTOSCALER_REF=d39e238d87d4158011d115391cbd4b506aa165ef
RUN python3 -m pip install --no-cache-dir \
  "git+https://github.com/SIVACOR/sivacor-autoscaler.git@${AUTOSCALER_REF}"
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
