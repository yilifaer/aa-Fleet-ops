"""Minimal Alliance Auth settings used to run the FleetOps test suite."""

import os

from allianceauth.project_template.project_name.settings.base import *  # noqa: F401,F403

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ROOT_URLCONF = "testauth.urls"
WSGI_APPLICATION = None
SECRET_KEY = "fleetops-test-secret-key"
SITE_NAME = "FleetOps Test"
SITE_URL = "http://localhost:8000"
DEBUG = False

INSTALLED_APPS += [  # noqa: F405
    "fleetops",
]

STATICFILES_DIRS = []
STATIC_ROOT = os.path.join(BASE_DIR, ".test-static")
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.path.join(BASE_DIR, ".test-db.sqlite3"),
        "TEST": {"NAME": ":memory:"},
    }
}

_test_db_url = os.environ.get("FLEETOPS_TEST_DB", "")
if _test_db_url.startswith("mysql://"):
    from urllib.parse import urlparse

    _db = urlparse(_test_db_url)
    _db_name = _db.path.lstrip("/") or "fleetops"
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.mysql",
            "NAME": _db_name,
            "USER": _db.username or "root",
            "PASSWORD": _db.password or "",
            "HOST": _db.hostname or "127.0.0.1",
            "PORT": str(_db.port or 3306),
            "OPTIONS": {"charset": "utf8mb4"},
            "TEST": {
                "NAME": f"test_{_db_name}",
                "CHARSET": "utf8mb4",
                "COLLATION": "utf8mb4_unicode_ci",
            },
        }
    }

CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        # Keep away from the DBs a local Alliance Auth uses by default (0 and 1).
        "LOCATION": os.environ.get("FLEETOPS_TEST_REDIS", "redis://localhost:6379/13"),
    }
}
SESSION_ENGINE = "django.contrib.sessions.backends.db"

CELERY_TASK_ALWAYS_EAGER = True
CELERY_TASK_EAGER_PROPAGATES = True
BROKER_URL = "memory://"

PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]

ESI_SSO_CLIENT_ID = "test-client-id"
ESI_SSO_CLIENT_SECRET = "test-client-secret"
ESI_SSO_CALLBACK_URL = "http://localhost:8000/sso/callback"
ESI_USER_CONTACT_EMAIL = "fleetops-tests@example.com"

EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"null": {"class": "logging.NullHandler"}},
    "root": {"handlers": ["null"], "level": "WARNING"},
}
CSRF_TRUSTED_ORIGINS = [SITE_URL]
SILENCED_SYSTEM_CHECKS = ["allianceauth.checks.A002"]
