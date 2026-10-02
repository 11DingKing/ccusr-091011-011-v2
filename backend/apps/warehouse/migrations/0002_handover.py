# -*- coding: utf-8 -*-
"""岗位交班与收发责任字段迁移

- StockOut/StockIn 增加不可变的 created_by（原始创建人），历史数据回填为 operator；
- StockIn 增加 status，历史入库记录一律视为“已完成”；
- stock_in_time 由自动写入改为入库完成时间（未完成时为空）；
- 新增交班会话、交班事项（含快照与部分唯一索引）、交班操作轨迹三张表。
"""
from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


def backfill_created_by(apps, schema_editor):
    """历史收发记录的原始创建人回填为当前操作人。"""
    for model_name in ('StockIn', 'StockOut'):
        model = apps.get_model('warehouse', model_name)
        for record in model.objects.filter(created_by__isnull=True):
            record.created_by_id = record.operator_id
            record.save(update_fields=['created_by'])


def reverse_created_by(apps, schema_editor):
    """回滚时无需处理，字段将随 RemoveField 删除。"""
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('contenttypes', '0002_remove_content_type_name'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('warehouse', '0001_initial'),
    ]

    operations = [
        # ---------- 收发记录责任字段 ----------
        migrations.AddField(
            model_name='stockin',
            name='created_by',
            field=models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='created_stock_ins', to=settings.AUTH_USER_MODEL,
                verbose_name='原始创建人',
            ),
        ),
        migrations.AddField(
            model_name='stockout',
            name='created_by',
            field=models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='created_stock_outs', to=settings.AUTH_USER_MODEL,
                verbose_name='原始创建人',
            ),
        ),
        # 历史记录先整体落为 completed，再把新记录默认值切回 pending
        migrations.AddField(
            model_name='stockin',
            name='status',
            field=models.CharField(
                choices=[('pending', '未完成'), ('completed', '已完成')],
                default='completed', max_length=20, verbose_name='状态',
            ),
        ),
        migrations.AlterField(
            model_name='stockin',
            name='status',
            field=models.CharField(
                choices=[('pending', '未完成'), ('completed', '已完成')],
                default='pending', max_length=20, verbose_name='状态',
            ),
        ),
        migrations.AddField(
            model_name='stockin',
            name='created_at',
            field=models.DateTimeField(auto_now_add=True, verbose_name='创建时间'),
        ),
        migrations.AlterField(
            model_name='stockin',
            name='operator',
            field=models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='stock_in_operations', to=settings.AUTH_USER_MODEL,
                verbose_name='当前处理人',
            ),
        ),
        migrations.AlterField(
            model_name='stockout',
            name='operator',
            field=models.ForeignKey(
                null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name='stock_out_operations', to=settings.AUTH_USER_MODEL,
                verbose_name='当前处理人',
            ),
        ),
        migrations.AlterField(
            model_name='stockin',
            name='stock_in_time',
            field=models.DateTimeField(
                blank=True, null=True, verbose_name='入库完成时间',
            ),
        ),
        migrations.AlterModelOptions(
            name='stockin',
            options={
                'ordering': ['-created_at'],
                'verbose_name': '入库记录',
                'verbose_name_plural': '入库记录',
            },
        ),
        migrations.RunPython(backfill_created_by, reverse_created_by),

        # ---------- 岗位交班 ----------
        migrations.CreateModel(
            name='HandoverSession',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('handover_at', models.DateTimeField(verbose_name='交班时间点')),
                ('status', models.CharField(
                    choices=[('open', '待接班'), ('confirmed', '已完成交班'), ('cancelled', '已取消')],
                    default='open', max_length=20, verbose_name='状态',
                )),
                ('remark', models.TextField(blank=True, verbose_name='交班说明')),
                ('confirmed_at', models.DateTimeField(null=True, blank=True, verbose_name='确认时间')),
                ('cancelled_at', models.DateTimeField(null=True, blank=True, verbose_name='取消时间')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='创建时间')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='更新时间')),
                ('handover_from', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='handled_handovers', to=settings.AUTH_USER_MODEL,
                    verbose_name='交班人',
                )),
                ('handover_to', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    related_name='received_handovers', to=settings.AUTH_USER_MODEL,
                    verbose_name='接班人',
                )),
            ],
            options={
                'verbose_name': '岗位交班会话',
                'verbose_name_plural': '岗位交班会话',
                'db_table': 'wh_handover_session',
                'ordering': ['-handover_at', '-id'],
            },
        ),
        migrations.CreateModel(
            name='HandoverItem',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('item_type', models.CharField(
                    choices=[('stock_out', '出库'), ('stock_in', '入库')],
                    max_length=20, verbose_name='事项类型',
                )),
                ('object_id', models.BigIntegerField(verbose_name='记录ID')),
                ('snapshot', models.JSONField(default=dict, verbose_name='事项快照')),
                ('accepted_at', models.DateTimeField(null=True, blank=True, verbose_name='接受时间')),
                ('returned_at', models.DateTimeField(null=True, blank=True, verbose_name='退回时间')),
                ('return_reason', models.TextField(blank=True, verbose_name='退回原因')),
                ('status', models.CharField(
                    choices=[
                        ('pending', '待确认'),
                        ('accepted', '已接受'),
                        ('returned', '已退回'),
                        ('cancelled', '随会话取消'),
                    ],
                    default='pending', max_length=20, verbose_name='状态',
                )),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='创建时间')),
                ('updated_at', models.DateTimeField(auto_now=True, verbose_name='更新时间')),
                ('session', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='items', to='warehouse.handoversession',
                    verbose_name='交班会话',
                )),
                ('content_type', models.ForeignKey(
                    on_delete=django.db.models.deletion.PROTECT,
                    to='contenttypes.contenttype', verbose_name='记录类型',
                )),
                ('originator', models.ForeignKey(
                    null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='originated_handover_items',
                    to=settings.AUTH_USER_MODEL, verbose_name='原始创建人',
                )),
                ('previous_operator', models.ForeignKey(
                    null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='previous_handover_items',
                    to=settings.AUTH_USER_MODEL, verbose_name='交班时处理人',
                )),
                ('accepted_by', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='accepted_handover_items',
                    to=settings.AUTH_USER_MODEL, verbose_name='实际接受人',
                )),
                ('returned_by', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='returned_handover_items',
                    to=settings.AUTH_USER_MODEL, verbose_name='退回操作人',
                )),
            ],
            options={
                'verbose_name': '交班事项',
                'verbose_name_plural': '交班事项',
                'db_table': 'wh_handover_item',
                'ordering': ['item_type', '-id'],
            },
        ),
        migrations.AddConstraint(
            model_name='handoveritem',
            constraint=models.UniqueConstraint(
                condition=models.Q(('status', 'pending')),
                fields=('content_type', 'object_id'),
                name='wh_handover_item_unique_pending_per_record',
            ),
        ),
        migrations.CreateModel(
            name='HandoverEvent',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('action', models.CharField(
                    choices=[
                        ('create', '创建交班'),
                        ('accept', '接受事项'),
                        ('return', '退回事项'),
                        ('confirm', '确认完成'),
                        ('cancel', '取消交班'),
                    ],
                    max_length=20, verbose_name='动作',
                )),
                ('detail', models.TextField(blank=True, verbose_name='操作详情')),
                ('created_at', models.DateTimeField(auto_now_add=True, verbose_name='操作时间')),
                ('session', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='events', to='warehouse.handoversession',
                    verbose_name='交班会话',
                )),
                ('item', models.ForeignKey(
                    blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='events', to='warehouse.handoveritem',
                    verbose_name='关联事项',
                )),
                ('actor', models.ForeignKey(
                    null=True, on_delete=django.db.models.deletion.SET_NULL,
                    related_name='handover_events', to=settings.AUTH_USER_MODEL,
                    verbose_name='操作人',
                )),
            ],
            options={
                'verbose_name': '交班操作轨迹',
                'verbose_name_plural': '交班操作轨迹',
                'db_table': 'wh_handover_event',
                'ordering': ['created_at', 'id'],
            },
        ),
    ]
