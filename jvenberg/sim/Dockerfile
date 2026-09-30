FROM ubuntu:24.04
RUN apt-get update -qq && apt-get install -y -qq curl ca-certificates libgomp1 libzmq5 >/dev/null && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
RUN uv venv -q --python 3.12 /venv && VIRTUAL_ENV=/venv uv pip install -q numpy "pycapnp==2.1.0" casadi pyzmq setproctitle sentry-sdk requests tqdm pycryptodome zstandard
ENV PATH=/venv/bin:$PATH
ENTRYPOINT ["/venv/bin/python3"]
