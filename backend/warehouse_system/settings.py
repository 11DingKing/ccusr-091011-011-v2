"""监管物资保管服务的运行配置。"""
import os
from datetime import timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "local-custody-service-key")
DEBUG = os.environ.get("DJANGO_DEBUG", "false").lower() == "true"
ALLOWED_HOSTS = ["*"]
INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "rest_framework",
    "django_filters",
    "apps.authentication",
    "apps.warehouse",
    "apps.personnel",
    "apps.reports",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "apps.core.logging_middleware.RequestLoggingMiddleware",
    "apps.core.logging_middleware.SecurityLoggingMiddleware",
    "apps.authentication.middleware.JWTAuthenticationMiddleware",
    "apps.authentication.middleware.OperationLogMiddleware",
]
ROOT_URLCONF = "warehouse_system.urls"
TEMPLATES = []
WSGI_APPLICATION = "warehouse_system.wsgi.application"
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": os.environ.get("SQLITE_PATH", str(BASE_DIR / "db.sqlite3")),
        # timeout 让写事务排队；WAL 模式由下方 connection_created 信号开启，
        # 避免交班并发接受时 SQLite 锁升级直接报 database is locked。
        "OPTIONS": {"timeout": 20},
        "TEST": {
            # 使用文件型测试库：内存共享缓存库不支持 WAL，
            # 岗位交班的多线程并发用例需要真实的 WAL 锁语义。
            "NAME": str(BASE_DIR / "test_db.sqlite3"),
        },
    }
}


def _enable_sqlite_wal(sender, connection, **kwargs):
    """每个 SQLite 连接开启 WAL 与忙等待。"""
    if connection.vendor == "sqlite":
        cursor = connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA busy_timeout=20000;")


from django.db.backends.signals import connection_created  # noqa: E402

connection_created.connect(_enable_sqlite_wal)
AUTH_PASSWORD_VALIDATORS = []
AUTH_USER_MODEL = "authentication.User"
LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["apps.authentication.backends.JWTAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_FILTER_BACKENDS": [
        "django_filters.rest_framework.DjangoFilterBackend",
        "rest_framework.filters.SearchFilter",
        "rest_framework.filters.OrderingFilter",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 10,
    "EXCEPTION_HANDLER": "apps.core.exceptions.custom_exception_handler",
}
JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY", "local-custody-jwt-key")
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_DELTA = timedelta(hours=24)
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"
LOGS_DIR = BASE_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)
from apps.core.logging_config import setup_logging
setup_logging(BASE_DIR, DEBUG)
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": "WARNING"},
}
