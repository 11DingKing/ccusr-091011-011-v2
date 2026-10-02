from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def copy_operator_to_responsibility(apps, schema_editor):
    """存量出库单：原始创建人/当前责任人默认沿用原操作人，保留历史轨迹。"""
    StockOut = apps.get_model('warehouse', 'StockOut')
    StockOut.objects.filter(created_by__isnull=True).update(
        created_by=models.F('operator'))
    StockOut.objects.filter(current_handler__isnull=True).update(
        current_handler=models.F('operator'))


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('warehouse', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='stockout',
            name='created_by',
            field=models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='created_stock_outs', to=settings.AUTH_USER_MODEL,
                verbose_name='原始创建人'),
        ),
        migrations.AddField(
            model_name='stockout',
            name='current_handler',
            field=models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='handled_stock_outs', to=settings.AUTH_USER_MODEL,
                verbose_name='当前处理责任人'),
        ),
        migrations.RunPython(copy_operator_to_responsibility, migrations.RunPython.noop),
    ]
