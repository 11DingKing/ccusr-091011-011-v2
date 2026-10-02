"""
Pytest配置文件
"""
import os
import tempfile
import django
from django.conf import settings

# 设置Django配置
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'warehouse_system.settings')

# 并发测试需要跨线程共享同一份测试库：内存SQLite是每连接独立的，
# 这里仅在pytest运行期间把测试库指向临时文件，结束后由Django自动删除。
os.environ.setdefault(
    'SQLITE_TEST_PATH',
    os.path.join(tempfile.gettempdir(), 'wh_pytest.sqlite3'),
)


def pytest_configure():
    """Pytest配置"""
    settings.DEBUG = False
    django.setup()
