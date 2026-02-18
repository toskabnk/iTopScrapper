# Usamos una imagen base de Python
FROM python:latest

# Variables de entorno para evitar preguntas interactivas
ENV DEBIAN_FRONTEND=noninteractive

# Actualizamos e instalamos dependencias necesarias
RUN apt-get update && apt-get install -y \
    chromium-driver \
    chromium \
    wget \
    unzip \
    curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Establecemos las variables para Selenium y Chromium
ENV CHROME_BIN=/usr/bin/chromium
ENV PATH=$PATH:/usr/lib/chromium/

# Instalamos Selenium
RUN pip install --no-cache-dir \
    selenium \
    beautifulsoup4 \
    paho-mqtt \
    python-dotenv \
    supabase

# Creamos un directorio para tu código
WORKDIR /app

# Copiamos tu código Python
COPY . /app

CMD ["python", "main.py"]