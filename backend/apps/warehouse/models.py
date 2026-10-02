"""
库房管理模型
"""
from django.db import models
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from apps.authentication.models import User


class Unit(models.Model):
    """单位模型"""
    name = models.CharField('单位名称', max_length=5, unique=True)
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='created_units', verbose_name='创建人'
    )
    is_active = models.BooleanField('是否启用', default=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    
    class Meta:
        db_table = 'wh_unit'
        verbose_name = '单位'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
    
    def __str__(self):
        return self.name
    
    @property
    def is_linked(self):
        """是否已关联至品类"""
        return self.categories.exists()


class Category(models.Model):
    """品类模型"""
    name = models.CharField('品类名称', max_length=10, unique=True)
    unit = models.ForeignKey(
        Unit, on_delete=models.PROTECT,
        related_name='categories', verbose_name='单位'
    )
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='created_categories', verbose_name='创建人'
    )
    is_active = models.BooleanField('是否启用', default=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    
    class Meta:
        db_table = 'wh_category'
        verbose_name = '品类'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
    
    def __str__(self):
        return self.name
    
    @property
    def is_linked(self):
        """是否已关联至品种"""
        return self.varieties.exists()


class Variety(models.Model):
    """品种模型"""
    name = models.CharField('品种名称', max_length=20)
    category = models.ForeignKey(
        Category, on_delete=models.PROTECT,
        related_name='varieties', verbose_name='所属品类'
    )
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='created_varieties', verbose_name='创建人'
    )
    is_active = models.BooleanField('是否启用', default=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    
    class Meta:
        db_table = 'wh_variety'
        verbose_name = '品种'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
        unique_together = ['category', 'name']
    
    def __str__(self):
        return f"{self.category.name} - {self.name}"
    
    @property
    def is_in_stock(self):
        """是否已入库"""
        return self.goods.exists()
    
    @property
    def unit_name(self):
        """获取单位名称"""
        return self.category.unit.name if self.category and self.category.unit else ''


class Goods(models.Model):
    """货物模型"""
    variety = models.ForeignKey(
        Variety, on_delete=models.CASCADE,
        related_name='goods', verbose_name='所属品种'
    )
    name = models.CharField('货物名称', max_length=200)
    code = models.CharField('货物编码', max_length=50, unique=True)
    specification = models.CharField('规格型号', max_length=200, blank=True)
    quantity = models.DecimalField('库存数量', max_digits=12, decimal_places=2, default=0)
    warning_threshold = models.DecimalField('预警阈值', max_digits=12, decimal_places=2, default=10)
    location = models.CharField('存放位置', max_length=100, blank=True)
    remark = models.TextField('备注', blank=True)
    is_active = models.BooleanField('是否启用', default=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    
    class Meta:
        db_table = 'wh_goods'
        verbose_name = '货物'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
    
    def __str__(self):
        return self.name
    
    @property
    def is_warning(self):
        """是否预警"""
        return self.quantity <= self.warning_threshold


class StockIn(models.Model):
    """入库记录模型"""
    STATUS_CHOICES = [
        ('pending', '未完成'),
        ('completed', '已完成'),
    ]

    goods = models.ForeignKey(
        Goods, on_delete=models.CASCADE,
        related_name='stock_ins', verbose_name='货物'
    )
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='created_stock_ins', verbose_name='原始创建人'
    )
    operator = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='stock_in_operations', verbose_name='当前处理人'
    )
    quantity = models.DecimalField('入库数量', max_digits=12, decimal_places=2)
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='pending')
    batch_no = models.CharField('批次号', max_length=50, blank=True)
    supplier = models.CharField('供应商', max_length=200, blank=True)
    stock_in_time = models.DateTimeField('入库完成时间', null=True, blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    remark = models.TextField('备注', blank=True)

    class Meta:
        db_table = 'wh_stock_in'
        verbose_name = '入库记录'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.goods.name} - {self.quantity}"

    def save(self, *args, **kwargs):
        # 原始创建人默认取首个处理人，且后续永不随交班改变
        if self._state.adding and not self.created_by_id and self.operator_id:
            self.created_by_id = self.operator_id
        super().save(*args, **kwargs)

    @property
    def is_pending(self):
        """是否为未完成收发事项"""
        return self.status == 'pending'


class StockOut(models.Model):
    """出库记录模型"""
    STATUS_CHOICES = [
        ('pending', '待审批'),
        ('approved', '已通过'),
        ('rejected', '已拒绝'),
        ('completed', '已完成'),
    ]
    
    goods = models.ForeignKey(
        Goods, on_delete=models.CASCADE,
        related_name='stock_outs', verbose_name='货物'
    )
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='created_stock_outs', verbose_name='原始创建人'
    )
    operator = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='stock_out_operations', verbose_name='当前处理人'
    )
    receiver = models.CharField('领用人', max_length=100)
    receiver_dept = models.CharField('领用部门', max_length=100, blank=True)
    quantity = models.DecimalField('出库数量', max_digits=12, decimal_places=2)
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='pending')
    stock_out_time = models.DateTimeField('出库时间', null=True, blank=True)
    remark = models.TextField('备注', blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)

    class Meta:
        db_table = 'wh_stock_out'
        verbose_name = '出库记录'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.goods.name} - {self.quantity}"

    def save(self, *args, **kwargs):
        # 原始创建人默认取首个处理人，且后续永不随交班改变
        if self._state.adding and not self.created_by_id and self.operator_id:
            self.created_by_id = self.operator_id
        super().save(*args, **kwargs)

    @property
    def is_pending_handover(self):
        """是否为需要交班的未决事项（待审批或已通过未完成）"""
        return self.status in ['pending', 'approved']


class Warning(models.Model):
    """预警记录模型"""
    TYPE_CHOICES = [
        ('low_stock', '库存不足'),
        ('expiring', '即将过期'),
        ('expired', '已过期'),
    ]
    
    goods = models.ForeignKey(
        Goods, on_delete=models.CASCADE,
        related_name='warnings', verbose_name='货物'
    )
    type = models.CharField('预警类型', max_length=20, choices=TYPE_CHOICES)
    message = models.TextField('预警信息')
    is_read = models.BooleanField('是否已读', default=False)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    
    class Meta:
        db_table = 'wh_warning'
        verbose_name = '预警记录'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.goods.name} - {self.get_type_display()}"


class Approval(models.Model):
    """审批记录模型"""
    STATUS_CHOICES = [
        ('pending', '待审批'),
        ('approved', '已通过'),
        ('rejected', '已拒绝'),
    ]
    
    stock_out = models.ForeignKey(
        StockOut, on_delete=models.CASCADE,
        related_name='approvals', verbose_name='出库记录'
    )
    approver = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='approvals', verbose_name='审批人'
    )
    status = models.CharField('审批状态', max_length=20, choices=STATUS_CHOICES, default='pending')
    remark = models.TextField('审批意见', blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    
    class Meta:
        db_table = 'wh_approval'
        verbose_name = '审批记录'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
    
    def __str__(self):
        return f"{self.stock_out} - {self.get_status_display()}"


# ==================== 岗位交班 ====================

class HandoverSession(models.Model):
    """岗位交班会话：把指定时间点的未决收发事项移交下一班"""
    STATUS_CHOICES = [
        ('open', '待接班'),
        ('confirmed', '已完成交班'),
        ('cancelled', '已取消'),
    ]

    handover_at = models.DateTimeField('交班时间点')
    handover_from = models.ForeignKey(
        User, on_delete=models.PROTECT,
        related_name='handled_handovers', verbose_name='交班人'
    )
    handover_to = models.ForeignKey(
        User, on_delete=models.PROTECT,
        related_name='received_handovers', verbose_name='接班人'
    )
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='open')
    remark = models.TextField('交班说明', blank=True)
    confirmed_at = models.DateTimeField('确认时间', null=True, blank=True)
    cancelled_at = models.DateTimeField('取消时间', null=True, blank=True)
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'wh_handover_session'
        verbose_name = '岗位交班会话'
        verbose_name_plural = verbose_name
        ordering = ['-handover_at', '-id']

    def __str__(self):
        return f"{self.handover_at:%Y-%m-%d %H:%M} {self.handover_from_id}→{self.handover_to_id}"

    @property
    def is_open(self):
        return self.status == 'open'


class HandoverItem(models.Model):
    """交班事项：未决收发记录在交班时间点的快照"""
    ITEM_TYPE_CHOICES = [
        ('stock_out', '出库'),
        ('stock_in', '入库'),
    ]
    STATUS_CHOICES = [
        ('pending', '待确认'),
        ('accepted', '已接受'),
        ('returned', '已退回'),
        ('cancelled', '随会话取消'),
    ]

    session = models.ForeignKey(
        HandoverSession, on_delete=models.CASCADE,
        related_name='items', verbose_name='交班会话'
    )
    item_type = models.CharField('事项类型', max_length=20, choices=ITEM_TYPE_CHOICES)
    content_type = models.ForeignKey(
        ContentType, on_delete=models.PROTECT, verbose_name='记录类型'
    )
    object_id = models.BigIntegerField('记录ID')
    content_object = GenericForeignKey('content_type', 'object_id')

    # 快照与责任轨迹（原始记录后续被修改也不影响交班凭据）
    snapshot = models.JSONField('事项快照', default=dict)
    originator = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='originated_handover_items', verbose_name='原始创建人'
    )
    previous_operator = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='previous_handover_items', verbose_name='交班时处理人'
    )
    accepted_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='accepted_handover_items', verbose_name='实际接受人'
    )
    accepted_at = models.DateTimeField('接受时间', null=True, blank=True)
    returned_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='returned_handover_items', verbose_name='退回操作人'
    )
    returned_at = models.DateTimeField('退回时间', null=True, blank=True)
    return_reason = models.TextField('退回原因', blank=True)
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='pending')
    created_at = models.DateTimeField('创建时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'wh_handover_item'
        verbose_name = '交班事项'
        verbose_name_plural = verbose_name
        ordering = ['item_type', '-id']
        constraints = [
            # 同一条收发记录在全局只能存在一个“待确认”事项：
            # 已接受/已退回后允许进入下一轮交班（链式交班）。
            models.UniqueConstraint(
                fields=['content_type', 'object_id'],
                condition=models.Q(status='pending'),
                name='wh_handover_item_unique_pending_per_record',
            ),
        ]

    def __str__(self):
        return f"{self.get_item_type_display()}#{self.object_id}-{self.get_status_display()}"


class HandoverEvent(models.Model):
    """交班操作轨迹：创建、接受、退回、确认、取消全程留痕"""
    ACTION_CHOICES = [
        ('create', '创建交班'),
        ('accept', '接受事项'),
        ('return', '退回事项'),
        ('confirm', '确认完成'),
        ('cancel', '取消交班'),
    ]

    session = models.ForeignKey(
        HandoverSession, on_delete=models.CASCADE,
        related_name='events', verbose_name='交班会话'
    )
    item = models.ForeignKey(
        HandoverItem, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='events', verbose_name='关联事项'
    )
    action = models.CharField('动作', max_length=20, choices=ACTION_CHOICES)
    actor = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='handover_events', verbose_name='操作人'
    )
    detail = models.TextField('操作详情', blank=True)
    created_at = models.DateTimeField('操作时间', auto_now_add=True)

    class Meta:
        db_table = 'wh_handover_event'
        verbose_name = '交班操作轨迹'
        verbose_name_plural = verbose_name
        ordering = ['created_at', 'id']

    def __str__(self):
        return f"{self.session_id}-{self.action}-{self.actor_id}"
