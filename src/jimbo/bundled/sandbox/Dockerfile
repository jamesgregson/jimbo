# The image jimbo builds as jimbo-sandbox:<version> for sandbox = "docker" or "podman" when sandbox_image is
# empty. Builds with docker build and podman build alike.
# To add tools, copy this file, add to it, build it and set sandbox_image to the result.
FROM docker.io/library/debian:stable-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        bash coreutils procps socat git curl ca-certificates ripgrep \
        build-essential python3 python3-venv python3-pip pipx nodejs npm clangd \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --break-system-packages uv pytest basedpyright \
    && basedpyright --version

# Commands run as the host user's id, which has no home in the image: any id may write here.
RUN mkdir -p /home/jimbo/.cache && chmod 1777 /home/jimbo /home/jimbo/.cache
ENV HOME=/home/jimbo
