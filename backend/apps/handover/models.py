"""
岗位交班模型

交班时把指定时间点（cutoff_time）仍挂在交班人责任范围内的未决收发事项
快照入册，由接班人逐件确认。责任转移只改写 StockOut.current_handler，
原始创建人（StockOut.created_by）及历史轨迹（operator、审批记录等）保持不变。
"""
import functools
import time

from django.db import OperationalError, models, transaction
from apps.authentication.models import User
from apps.warehouse.models import StockOut


def retry_on_locked(max_attempts=20):
    """
    SQLite 在锁升级冲突时会立即抛 locked（不等待 busy_timeout），
    对整段事务做指数退避重试；其他数据库下该错误极少见，重试同样安全。
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            delay = 0.01
            for attempt in range(max_attempts):
                try:
                    return func(*args, **kwargs)
                except OperationalError as exc:
                    if 'locked' not in str(exc).lower() or attempt == max_attempts - 1:
                        raise
                    time.sleep(delay)
                    delay = min(delay * 2, 0.2)
        return wrapper
    return decorator


class HandoverSession(models.Model):
    """岗位交班会话：一次班次交接的清单载体"""

    STATUS_CHOICES = [
        ('open', '待确认'),
        ('completed', '已完成'),
        ('cancelled', '已取消'),
    ]

    handover_user = models.ForeignKey(
        User, on_delete=models.PROTECT,
        related_name='handovers_given', verbose_name='交班人'
    )
    receiver_user = models.ForeignKey(
        User, on_delete=models.PROTECT,
        related_name='handovers_received', verbose_name='接班人',
        null=True, blank=True
    )
    cutoff_time = models.DateTimeField('事项截止时间点')
    status = models.CharField('状态', max_length=20, choices=STATUS_CHOICES, default='open')
    created_at = models.DateTimeField('发起时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)
    completed_at = models.DateTimeField('完成时间', null=True, blank=True)
    cancelled_at = models.DateTimeField('取消时间', null=True, blank=True)
    cancel_reason = models.CharField('取消原因', max_length=200, blank=True)
    remark = models.TextField('交班说明', blank=True)

    class Meta:
        db_table = 'wh_handover_session'
        verbose_name = '岗位交班会话'
        verbose_name_plural = verbose_name
        ordering = ['-created_at']
        constraints = [
            # 同一交班人同时只能有一个待确认会话（并发建会兜底）
            models.UniqueConstraint(
                fields=['handover_user'],
                condition=models.Q(status='open'),
                name='uniq_open_handover_session_per_user',
            ),
        ]

    def __str__(self):
        return f"交班 {self.id}（{self.handover_user} → {self.receiver_user or '待定'}）"

    @property
    def pending_count(self):
        return self.items.filter(status='pending').count()

    @property
    def accepted_count(self):
        return self.items.filter(status='accepted').count()

    @property
    def rejected_count(self):
        return self.items.filter(status='rejected').count()

    def refresh_status(self):
        """
        事项处置后收口会话状态：
        - 无待确认且存在接班人处置（接受/退回）的事项 → 已完成
        - 无待确认且全部被交班人取消 → 已取消
        """
        if self.status != 'open':
            return
        if not self.items.exists() or self.pending_count > 0:
            return

        from django.utils import timezone
        now = timezone.now()
        decided = self.accepted_count + self.rejected_count
        if decided > 0:
            self.status = 'completed'
            self.completed_at = now
        else:
            self.status = 'cancelled'
            self.cancelled_at = now
            self.cancel_reason = '清单事项已全部取消'
        self.save(update_fields=['status', 'completed_at', 'cancelled_at',
                                 'cancel_reason', 'updated_at'])


class HandoverItem(models.Model):
    """交班清单事项：对截点未决出库单的快照留痕"""

    TYPE_STOCK_OUT = 'stock_out'
    TYPE_CHOICES = [
        (TYPE_STOCK_OUT, '未完成出库'),
    ]

    STATUS_CHOICES = [
        ('pending', '待确认'),
        ('accepted', '已接受'),
        ('rejected', '已退回'),
        ('cancelled', '已取消'),
    ]

    session = models.ForeignKey(
        HandoverSession, on_delete=models.CASCADE,
        related_name='items', verbose_name='所属交班会话'
    )
    ref_type = models.CharField('事项类型', max_length=20, choices=TYPE_CHOICES,
                                default=TYPE_STOCK_OUT)
    ref_id = models.BigIntegerField('关联单据ID')
    # 交班时点的责任归属：用于退回后核对责任是否仍在交班人名下
    original_handler = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True,
        related_name='handover_items_as_owner', verbose_name='交班前责任人'
    )
    accepted_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='handover_items_accepted', verbose_name='接受人'
    )
    resolved_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True,
        related_name='handover_items_resolved', verbose_name='处置人'
    )
    status = models.CharField('确认状态', max_length=20, choices=STATUS_CHOICES,
                              default='pending')
    # 交班时点的快照字段——事后单据被改动或删除，清单仍能还原交班现场
    snapshot = models.JSONField('单据快照', default=dict)
    reject_reason = models.CharField('退回原因', max_length=200, blank=True)
    accepted_at = models.DateTimeField('接受时间', null=True, blank=True)
    resolved_at = models.DateTimeField('处置时间', null=True, blank=True)
    created_at = models.DateTimeField('入册时间', auto_now_add=True)
    updated_at = models.DateTimeField('更新时间', auto_now=True)

    class Meta:
        db_table = 'wh_handover_item'
        verbose_name = '交班清单事项'
        verbose_name_plural = verbose_name
        ordering = ['session_id', '-created_at']
        constraints = [
            # 同一未决单据在同一时间只能挂在一个待确认会话中，避免重复移交
            models.UniqueConstraint(
                fields=['ref_type', 'ref_id'],
                condition=models.Q(status='pending'),
                name='uniq_pending_handover_item_per_doc',
            ),
        ]

    def __str__(self):
        return f"{self.get_ref_type_display()}#{self.ref_id}（{self.get_status_display()}）"

    @classmethod
    def snapshot_stock_out(cls, stock_out):
        """生成出库单的交班快照（含完整审批轨迹标识，轨迹本身留存在业务表）。"""
        approvals = list(stock_out.approvals.values('id', 'status', 'remark').order_by('created_at'))
        return {
            'id': stock_out.id,
            'goods': stock_out.goods_id,
            'goods_name': stock_out.goods.name,
            'goods_code': stock_out.goods.code,
            'quantity': str(stock_out.quantity),
            'receiver': stock_out.receiver,
            'receiver_dept': stock_out.receiver_dept,
            'status': stock_out.status,
            'status_display': stock_out.get_status_display(),
            'remark': stock_out.remark,
            'created_by': stock_out.created_by_id,
            'created_by_name': getattr(stock_out.created_by, 'username', None),
            'operator': stock_out.operator_id,
            'operator_name': getattr(stock_out.operator, 'username', None),
            'current_handler': stock_out.current_handler_id,
            'current_handler_name': getattr(stock_out.current_handler, 'username', None),
            'created_at': stock_out.created_at.isoformat() if stock_out.created_at else None,
            'approvals': approvals,
        }

    def resolve_object(self):
        """按类型取当前业务单据，不存在则返回 None。"""
        if self.ref_type == self.TYPE_STOCK_OUT:
            return StockOut.objects.filter(pk=self.ref_id).first()
        return None

    @retry_on_locked()
    @transaction.atomic
    def accept(self, accepted_by):
        """
        接班人确认接受，转移处理责任。

        用条件 UPDATE 做状态比较交换（pending → accepted）：
        两个接班人并发接受同一事项时，只有一个事务能拿到 1 行影响。
        首条语句即写操作（直接取 RESERVED 锁），避免 SQLite 下
        先持 SHARED 锁再升级造成的写死锁。
        """
        from django.utils import timezone

        now = timezone.now()
        updated = HandoverItem.objects.filter(
            pk=self.pk, status='pending'
        ).update(
            status='accepted', accepted_by=accepted_by, resolved_by=accepted_by,
            accepted_at=now, resolved_at=now, updated_at=now,
        )
        if updated == 0:
            return False

        stock_out = self.resolve_object()
        if stock_out is None:
            # 单据已被删除：无法转移责任，接受回滚为待确认
            self._rollback_pending(now)
            raise HandoverConflict('该事项的单据已不存在，无法接受')

        if stock_out.status not in ('pending', 'approved'):
            # 单据在交班期间已办结（已完成/已拒绝）：没有待处理责任可转移
            self._rollback_pending(now)
            raise HandoverConflict('该事项已办结，无需移交')

        # 责任转移：只改当前责任人，created_by / operator 永不动
        stock_out.current_handler = accepted_by
        stock_out.save(update_fields=['current_handler'])

        self.refresh_from_db(fields=['status', 'accepted_by', 'accepted_at',
                                     'resolved_at', 'updated_at', 'snapshot'])
        self.session.refresh_status()
        return True

    def _rollback_pending(self, now):
        HandoverItem.objects.filter(pk=self.pk).update(
            status='pending', accepted_by=None, resolved_by=None,
            accepted_at=None, resolved_at=None, updated_at=now,
        )
        self.refresh_from_db()

    @retry_on_locked()
    @transaction.atomic
    def reject(self, rejected_by, reason):
        """接班人退回该事项，责任留在交班人名下。"""
        from django.utils import timezone

        now = timezone.now()
        updated = HandoverItem.objects.filter(
            pk=self.pk, status='pending'
        ).update(
            status='rejected', reject_reason=reason,
            resolved_by=rejected_by, resolved_at=now, updated_at=now,
        )
        if updated == 0:
            return False

        stock_out = self.resolve_object()
        if stock_out is not None:
            # 退回不转移责任；若责任已因其他途径漂移，拉回交班人，保持责任边界清晰
            if stock_out.current_handler_id != self.original_handler_id:
                stock_out.current_handler_id = self.original_handler_id
                stock_out.save(update_fields=['current_handler'])

        self.session.refresh_status()
        return True

    @retry_on_locked()
    @transaction.atomic
    def cancel(self):
        """交班人在确认前取消该事项（不入本次交接）。"""
        from django.utils import timezone

        now = timezone.now()
        updated = HandoverItem.objects.filter(
            pk=self.pk, status='pending'
        ).update(status='cancelled', resolved_at=now, updated_at=now)
        if updated == 0:
            return False
        self.session.refresh_status()
        return True


class HandoverConflict(Exception):
    """事项处置冲突（并发抢占、单据状态变化等）。"""
    pass
