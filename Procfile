web: gunicorn school_erp.wsgi --workers 2 --log-file -
release: python manage.py collectstatic --noinput && python manage.py migrate --noinput
