"""
岗位交班业务服务

建会时按 cutoff_time 对未决出库单做"时间点切片"并生成不可变快照，
后续确认环节只改责任归属，不改写快照。
"""
from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.warehouse.models import StockOut

from .models import HandoverSession, HandoverItem, retry_on_locked


class HandoverError(Exception):
    """交班业务校验错误。"""


# 未决口径：待审批、已批准但尚未完成实物出库
UNRESOLVED_STOCK_OUT_STATUSES = ('pending', 'approved')


def collect_pending_stock_outs(handover_user, cutoff_time):
    """
    取截止时间点仍由交班人负责的未决出库单。

    口径：单据在截点之前已创建（created_at <= cutoff），到截点时
    仍为待审批/未完成出库状态、当前处理责任在交班人名下。
    """
    return (
        StockOut.objects
        .select_related('goods', 'created_by', 'operator', 'current_handler')
        .filter(
            status__in=UNRESOLVED_STOCK_OUT_STATUSES,
            created_at__lte=cutoff_time,
            current_handler=handover_user,
        )
        # 已挂在其他待确认清单中的单据不重复入册
        .exclude(
            id__in=HandoverItem.objects.filter(
                status='pending', ref_type=HandoverItem.TYPE_STOCK_OUT,
            ).values('ref_id')
        )
        .order_by('created_at')
    )


@transaction.atomic
def create_session(*, handover_user, cutoff_time, receiver_user=None, remark=''):
    """发起交班：锁定截点未决事项并生成快照清单。"""
    if cutoff_time > timezone.now():
        raise HandoverError('截止时间点不能晚于当前时间')

    if HandoverSession.objects.filter(
        handover_user=handover_user, status='open'
    ).exists():
        raise HandoverError('您已有待确认的交班会话，请先完成或取消')

    try:
        session = HandoverSession.objects.create(
            handover_user=handover_user,
            receiver_user=receiver_user,
            cutoff_time=cutoff_time,
            remark=remark or '',
        )

        stock_outs = list(collect_pending_stock_outs(handover_user, cutoff_time))
        items = []
        for stock_out in stock_outs:
            items.append(HandoverItem(
                session=session,
                ref_type=HandoverItem.TYPE_STOCK_OUT,
                ref_id=stock_out.id,
                original_handler=handover_user,
                snapshot=HandoverItem.snapshot_stock_out(stock_out),
            ))
        try:
            HandoverItem.objects.bulk_create(items)
        except IntegrityError as exc:
            # 并发建会或单据已挂在其他待确认清单中
            raise HandoverError('存在已在其他交班清单中待确认的事项，请刷新后重试') from exc
    except IntegrityError as exc:
        # 并发发起：唯一约束兜底同一交班人的待确认会话
        raise HandoverError('您已有待确认的交班会话，请先完成或取消') from exc
    return session, stock_outs


@retry_on_locked()
@transaction.atomic
def cancel_session(session, *, user, reason=''):
    """交班人在接班人确认前取消整会：已接受的事项不回滚，其余作废。"""
    now = timezone.now()
    # 首条语句即写操作（直接取 RESERVED 锁），避免与并发接受/取消发生写死锁
    pending_count = HandoverItem.objects.filter(
        session_id=session.pk, status='pending'
    ).update(status='cancelled', resolved_at=now, updated_at=now)

    # 锁内重新核对会话归属与状态
    locked = HandoverSession.objects.filter(
        pk=session.pk, status='open').only('id', 'handover_user_id').first()
    if locked is None:
        raise HandoverError('当前会话状态不允许取消')
    if user.id != locked.handover_user_id:
        raise HandoverError('只有交班人可以取消交班会话')

    accepted_count = HandoverItem.objects.filter(
        session_id=session.pk, status='accepted').count()
    if accepted_count > 0:
        # 已有事项完成责任转移，整会取消必须回滚（上面的批量作废随之撤销）
        raise HandoverError('已有事项被接班人接受，不能取消整会；可逐项处置')

    HandoverSession.objects.filter(pk=session.pk).update(
        status='cancelled', cancelled_at=now,
        cancel_reason=reason or '', updated_at=now)
    session.refresh_from_db()
    return session
