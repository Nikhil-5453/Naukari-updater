FROM public.ecr.aws/lambda/python:3.12

# Chromium dependencies via dnf (Amazon Linux 2023)
RUN dnf install -y \
    alsa-lib \
    atk \
    at-spi2-atk \
    at-spi2-core \
    cairo \
    cups-libs \
    dbus-libs \
    expat \
    gdk-pixbuf2 \
    glib2 \
    gtk3 \
    libdrm \
    libgbm \
    libX11 \
    libX11-xcb \
    libXcomposite \
    libXcursor \
    libXdamage \
    libXext \
    libXfixes \
    libXi \
    libXrandr \
    libXrender \
    libXScrnSaver \
    libXtst \
    libxcb \
    libxkbcommon \
    mesa-libgbm \
    nspr \
    nss \
    nss-util \
    pango \
    xdg-utils \
    && dnf clean all

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN playwright install chromium

COPY handler.py ${LAMBDA_TASK_ROOT}/handler.py

CMD ["handler.lambda_handler"]