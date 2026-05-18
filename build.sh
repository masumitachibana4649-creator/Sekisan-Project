#!/usr/bin/env bash
set -o errexit

pip install -r requirements.txt
python manage.py collectstatic --no-input
python manage.py migrate

if [[ -n "$DJANGO_SUPERUSER_USERNAME" ]]; then
  python manage.py shell <<'PY'
import os

from django.contrib.auth import get_user_model

User = get_user_model()
username = os.environ["DJANGO_SUPERUSER_USERNAME"]
email = os.environ.get("DJANGO_SUPERUSER_EMAIL", "")
password = os.environ.get("DJANGO_SUPERUSER_PASSWORD")

user, created = User.objects.get_or_create(
    username=username,
    defaults={"email": email, "is_staff": True, "is_superuser": True},
)
user.email = email or user.email
user.is_staff = True
user.is_superuser = True
if password:
    user.set_password(password)
user.save()
print(f"Superuser {'created' if created else 'updated'}: {username}")
PY
fi
