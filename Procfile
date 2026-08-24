web: gunicorn school_erp.wsgi --workers 2 --log-file -
worker: cd whatsapp_service && node server.js
release: python manage.py collectstatic --noinput && python manage.py migrate --noinput && cd whatsapp_service && npm install --omit=dev
