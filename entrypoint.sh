#!/bin/sh
set -e

echo "Aplicando migraciones..."
python manage.py migrate --noinput

echo "Arrancando gunicorn..."
exec gunicorn config.wsgi:application --bind 0.0.0.0:8000 --workers 3
