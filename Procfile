web: gunicorn school_erp.wsgi --bind 0.0.0.0:${PORT:-8000} --workers 2 --log-file -
worker: cd whatsapp_service && node server.js
release: python manage.py migrate --noinput && python manage.py initadmin && python manage.py collectstatic --noinput && cd whatsapp_service && npm install --omit=dev
