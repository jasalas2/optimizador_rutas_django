FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1

WORKDIR /app

# build-essential como red de seguridad, por si algún paquete (geopandas,
# shapely, ortools) no trae wheel prearmado para esta imagen exacta.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Sin .env en la imagen (está en .dockerignore) — los defaults de
# settings.py alcanzan para que collectstatic corra en build time.
RUN python manage.py collectstatic --noinput

EXPOSE 8000

ENTRYPOINT ["sh", "entrypoint.sh"]
