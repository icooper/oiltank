# syntax=docker/dockerfile:1

# ---- builder: uv-managed CPython + a non-editable venv -------------------------------------
FROM debian:bookworm-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.11 /uv /uvx /bin/

# Install a uv-managed (python-build-standalone) interpreter at a fixed path so it can be copied
# verbatim into the distroless runtime, where the venv's `python` symlink still resolves.
ENV UV_PYTHON_INSTALL_DIR=/python \
    UV_PYTHON_PREFERENCE=only-managed \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app
COPY .python-version ./
RUN uv python install

# Dependencies first (cached layer), then the project itself.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

COPY README.md LICENSE.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# The numpy/OpenCV wheels link against the system zlib, which distroless/cc does not ship. It is
# the only shared library they need beyond glibc/libstdc++. Stage it under its multiarch path.
RUN libdir="/usr/lib/$(uname -m)-linux-gnu" \
    && mkdir -p "/runtime-libs${libdir}" \
    && cp -L "${libdir}/libz.so.1" "/runtime-libs${libdir}/"

# ---- runtime: distroless, no shell, non-root -----------------------------------------------
FROM gcr.io/distroless/cc-debian12:nonroot

COPY --from=builder /runtime-libs/ /
COPY --from=builder /python /python
COPY --from=builder /app/.venv /app/.venv

ENV PATH=/app/.venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# distroless :nonroot already runs as 65532:65532; restated so it is explicit.
USER 65532:65532
# Run from / so a relative OILTANK_DATA_DIR of `data` (the default) means /data here and ./data in a
# checkout: the same directory, through the Compose bind mount. One .env works for both.
WORKDIR /

ENTRYPOINT ["/app/.venv/bin/oiltank"]
CMD ["--help"]
