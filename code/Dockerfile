FROM python:3.12-slim

# DejaVu: las leyendas de los mapas que se generan necesitan una fuente real.
# Chromium y Noto: la versión imprimible de cada unidad se regenera imprimiéndola con el
# modo de impresión de la propia plantilla (web/impresion.py).
# Montserrat y Noto: el texto de los PDF maquetados se reescribe con la fuente con la que
# se dibujó (web/pdf_texto.py). Liberation y Gelasio sustituyen a Arial y Georgia con sus
# mismas métricas; Gelasio no está en Debian y se toma de Google Fonts (licencia OFL).
# Tesseract: la posición de cada rótulo dentro de las imágenes con texto (web/imagenes.py).
# PT Sans y PT Sans Narrow: la letra humanista y estrecha de las infografías.
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-dejavu-core fonts-noto-core fonts-montserrat \
    fonts-liberation chromium curl \
    tesseract-ocr tesseract-ocr-glg tesseract-ocr-cat tesseract-ocr-spa fonts-paratype \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p /usr/local/share/fonts/gelasio \
 && curl -fsSL -o /usr/local/share/fonts/gelasio/Gelasio-Regular.ttf \
    "https://github.com/google/fonts/raw/main/ofl/gelasio/Gelasio%5Bwght%5D.ttf" \
 && chmod 644 /usr/local/share/fonts/gelasio/Gelasio-Regular.ttf

WORKDIR /app
# El kit de la plataforma de demos (acceso con Keycloak, MCP, tema Sarela Coach) llega
# como contexto adicional de construcción: el compose lo toma de ../plataforma/kit.
# restricciones.txt fija las versiones del árbol entero, las mismas que prueba el kit.
COPY --from=kit . /kit
COPY requirements.txt .
RUN pip install --no-cache-dir -c /kit/restricciones.txt -r requirements.txt "/kit[mcp]"

COPY src/ src/
COPY web/ web/
COPY scripts/ scripts/
COPY mcp_servidor.py .

# Sin privilegios: la aplicación solo escribe en /datos.
RUN useradd --create-home --uid 1000 sarela && mkdir -p /datos && chown sarela /datos
USER sarela

ENV SARELA_DATOS=/datos \
    SARELA_CHROMIUM=/usr/bin/chromium \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8800
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD curl -fsS http://127.0.0.1:8800/salud || exit 1

# Un solo proceso: el asistente trabaja en hilos dentro de él y guarda el progreso en la
# base de datos. Detrás de Caddy, que termina el HTTPS.
CMD ["uvicorn", "web.app:aplicacion", "--host", "0.0.0.0", "--port", "8800", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]
