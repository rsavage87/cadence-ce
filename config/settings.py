"""
Django settings for Cadence CE.

Configuration comes from environment variables (see .env.example).
Tests run on SQLite automatically; everything else expects PostgreSQL.
"""
import os
import sys
from pathlib import Path

import dj_database_url

BASE_DIR = Path(__file__).resolve().parent.parent

# Load a local .env file if present (no third-party dependency).
_env_file = BASE_DIR / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

TESTING = "pytest" in sys.modules or os.environ.get("CADENCE_TESTING") == "1"

SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-insecure-key")
DEBUG = os.environ.get("DJANGO_DEBUG", "0") == "1"
ALLOWED_HOSTS = [h for h in os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h]
CSRF_TRUSTED_ORIGINS = [o for o in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if o]
# Slice 23: the largest multipart body accepted (apps.core.http.RejectNulMiddleware): an import file of 5 MB (apps.imports.base.MAX_BYTES)
# and its form. A proxy in front must allow at least this much (nginx: client_max_body_size 6m).
UPLOAD_MAX_BYTES = 6 * 1024 * 1024
# Slice 22: a page left open from before a facility switch (which renews the token) goes to its screen instead of a bare 403.
CSRF_FAILURE_VIEW = "apps.web.htmx.csrf_failure"

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "whitenoise.runserver_nostatic",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework.authtoken",
    "simple_history",
    "django_htmx",
    "apps.core",
    "apps.tenants",
    "apps.accounts",
    "apps.equipment",
    "apps.contracts",
    "apps.workorders",
    "apps.pm",
    "apps.recalls",
    "apps.credentials",
    "apps.portal",
    "apps.facility",
    "apps.jobs",
    "apps.reports",
    "apps.notifications",
    "apps.imports",
    "apps.api",
    "apps.demo",
    "apps.web",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "apps.core.http.RejectNulMiddleware",  # PostgreSQL text cannot hold NUL: a 400, never a 500 (slice 17)
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "apps.tenants.middleware.TenantMiddleware",
    "simple_history.middleware.HistoryRequestMiddleware",
    "django_htmx.middleware.HtmxMiddleware",
    "apps.web.htmx.VaryOnHtmxMiddleware",
    "apps.web.htmx.FacilityTabMiddleware",  # slice 22: a tab left in another facility reloads instead of reading this one
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.web.context_processors.shell",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

# --- Database -------------------------------------------------------------
# 'default' is the runtime connection (non-superuser role, subject to row-level security).
# 'migrate' is the owner connection used by `migrate` and `enable_rls`.
if TESTING and os.environ.get("CADENCE_TEST_DATABASE_URL"):
    # The suite on PostgreSQL (CI's second test job, slice 17): pytest-django creates test_<name> on this server and migrates it.
    # Connect as a superuser (CI's service container user): the row-level security policies, which tests/conftest.py installs on
    # the test database, do not apply to it, so the suite runs as on SQLite; tests/test_postgres_rls.py switches to the non-owner
    # role cadence_app for the paths that must hold under the policies. 'migrate' points at the same test database.
    DATABASES = {"default": dj_database_url.parse(os.environ["CADENCE_TEST_DATABASE_URL"])}
    DATABASES["migrate"] = {**DATABASES["default"], "TEST": {"MIRROR": "default"}}
elif TESTING:
    DATABASES = {
        "default": {"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"},
    }
    DATABASES["migrate"] = DATABASES["default"]
else:
    _default_url = os.environ.get("DATABASE_URL", "postgres://cadence:cadence@localhost:5432/cadence")
    DATABASES = {
        "default": dj_database_url.parse(_default_url, conn_max_age=60),
        "migrate": dj_database_url.parse(os.environ.get("MIGRATE_DATABASE_URL", _default_url)),
    }

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "accounts.User"
LOGIN_URL = "web:login"
LOGIN_REDIRECT_URL = "web:overview"
LOGOUT_REDIRECT_URL = "web:login"
# Sign in with the username or the email address in any letter case; accounts of a deactivated facility cannot sign in.
AUTHENTICATION_BACKENDS = ["apps.accounts.backends.UsernameOrEmailBackend"]

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = os.environ.get("DJANGO_TIME_ZONE", "America/New_York")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    # The manifest backend needs collectstatic first, so tests and DEBUG use the plain one.
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage" if DEBUG or TESTING
                    else "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}

CACHES = {
    "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"},
    # Sign-in lockouts and rate limits (apps.accounts.signin, the portal): their own store, sized so a flood of junk logins
    # cannot push real counters out (LocMem evicts once full; the default holds 300 keys). Per process: see the README.
    "limits": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "limits", "OPTIONS": {"MAX_ENTRIES": 200_000}},
}

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
        "apps.api.authentication.TokenAuthentication",  # also refuses a deactivated facility
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
    "DEFAULT_FILTER_BACKENDS": ["rest_framework.filters.SearchFilter", "rest_framework.filters.OrderingFilter"],
}

# --- Security defaults for hosted deployments ------------------------------
if not DEBUG and not TESTING:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = os.environ.get("DJANGO_SSL_REDIRECT", "1") == "1"
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 60 * 60 * 24 * 30
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True

# --- Product settings -------------------------------------------------------
PORTAL_BASE_URL = os.environ.get("PORTAL_BASE_URL", "http://localhost:8000")
PORTAL_RATE_LIMIT_PER_HOUR = int(os.environ.get("PORTAL_RATE_LIMIT_PER_HOUR", "20"))
PORTAL_EMAILS_PER_ADDRESS_PER_HOUR = 3     # portal confirmations to one address, whatever the sender's network address
PORTAL_EMAILS_PER_FACILITY_PER_HOUR = 60   # and from one facility's portal in all
PM_LEAD_DAYS = int(os.environ.get("PM_LEAD_DAYS", "21"))  # generate PM work orders this many days before due
# The hour the daily jobs run: each facility's own jobs at this time on its own clock (Tenant.timezone), the openFDA import at this time
# on the server's (TIME_ZONE). Slice 21.
SCHEDULER_DAILY_AT = os.environ.get("SCHEDULER_DAILY_AT", "02:30")
CREDENTIAL_EXPIRY_WARNING_DAYS = 60
CONTRACT_EXPIRY_WARNING_DAYS = 90

# --- Email and sign-in (slice 10) ------------------------------------------------
# Links in invitation and password-reset emails start with APP_BASE_URL, never with the request's Host header.
APP_BASE_URL = os.environ.get("APP_BASE_URL", PORTAL_BASE_URL).rstrip("/")
EMAIL_BACKEND = os.environ.get("DJANGO_EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend" if DEBUG
                               else "django.core.mail.backends.smtp.EmailBackend")
EMAIL_HOST = os.environ.get("EMAIL_HOST", "localhost")
EMAIL_PORT = int(os.environ.get("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.environ.get("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.environ.get("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = os.environ.get("EMAIL_USE_TLS", "1") == "1"   # STARTTLS, usually port 587
EMAIL_USE_SSL = os.environ.get("EMAIL_USE_SSL", "0") == "1"   # implicit TLS, usually port 465; set EMAIL_USE_TLS=0 with it
EMAIL_TIMEOUT = 10  # seconds; a send happens inside the request that asked for it
DEFAULT_FROM_EMAIL = os.environ.get("DEFAULT_FROM_EMAIL", "Cadence CE <no-reply@localhost>")
INVITATION_VALID_DAYS = 7              # an invitation link sets the first password within this many days
PASSWORD_RESET_TIMEOUT = 60 * 60 * 2   # a password-reset link works for two hours (seconds, Django's setting)
SIGNIN_MAX_FAILURES = 10               # failed sign-ins for one account within the window lock that account for the window
SIGNIN_MAX_FAILURES_PER_IP = 100       # generous: a hospital's staff often share one outbound address
SIGNIN_WINDOW_MINUTES = 15
PASSWORD_RESET_MAX_PER_HOUR = 5        # reset emails one address can be sent per hour
PASSWORD_RESET_MAX_PER_IP_PER_HOUR = 30

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": os.environ.get("DJANGO_LOG_LEVEL", "INFO")},
}
