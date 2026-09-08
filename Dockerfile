# One image serves both roles used by the local demo (compose.yaml runs it
# twice, with different commands): the upstream fixture server
# (examples/upstream_server.py) and the SentinelMCP gateway itself.

FROM python:3.12-slim

WORKDIR /app

# Install sentinelmcp (and its declared runtime dependencies) as a real
# package first, so dependency layers cache independently of source changes.
COPY pyproject.toml ./
COPY sentinelmcp ./sentinelmcp
RUN pip install --no-cache-dir .

# examples/ is demo/fixture code, not part of the installed package (see
# pyproject.toml's [tool.setuptools.packages.find]) - copied separately and
# run the same way it is locally: `python -m examples.upstream_server` from
# this working directory.
COPY examples ./examples

# No default CMD/ENTRYPOINT - compose.yaml sets the command per service.
