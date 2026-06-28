# Dockerfile
# Builds a Lambda-compatible container image with Playwright + Chromium.
# Lambda does not support GUI; we install Chromium headlessly via Playwright.

FROM public.ecr.aws/lambda/python:3.12

# ── System dependencies for Chromium ─────────────────────────────────────────
RUN dnf install -y \
    atk \
    at-spi2-atk \
    cups-libs \
    libdrm \
    libXcomposite \
    libXdamage \
    libXfixes \
    libXrandr \
    libgbm \
    libxkbcommon \
    mesa-libgbm \
    nss \
    pango \
    xdg-utils \
    && dnf clean all

# ── Python dependencies ───────────────────────────────────────────────────────
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Install Playwright's Chromium into /tmp-compatible path ──────────────────
# PLAYWRIGHT_BROWSERS_PATH must be writable at runtime; /ms-playwright is baked
# into the image layer (read-only at runtime is fine — Playwright reads it).
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN playwright install chromium --with-deps

# ── Copy Lambda handler ───────────────────────────────────────────────────────
COPY src/handler.py ${LAMBDA_TASK_ROOT}/handler.py

# ── Lambda entry point ────────────────────────────────────────────────────────
CMD ["handler.lambda_handler"]
