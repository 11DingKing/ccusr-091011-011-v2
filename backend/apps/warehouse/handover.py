"""
岗位交班领域服务

责任边界约定：
- StockOut.created_by / StockIn.created_by 为原始创建人，永不因交班改变；
- operator 为“当前处理责任人”，事项被接班人接受后才更新为接班人；
- HandoverItem.snapshot 冻结交班时间点的关键字段，操作轨迹写入 HandoverEvent。
"""
from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.utils import timezone

from .models import HandoverEvent, HandoverItem, HandoverSession, StockIn, StockOut


def _annotate(queryset):
    """附加事项数量统计。"""
    return queryset.annotate(
        total_count=Count('items'),
        accepted_count=Count('items', filter=Q(items__status='accepted')),
        returned_count=Count('items', filter=Q(items__status='returned')),
        pending_count=Count('items', filter=Q(items__status='pending')),
    )


def get_session_detail(session_id):
    """装配会话详情：统计 + 事项清单 + 操作轨迹。"""
    return (
        _annotate(
            HandoverSession.objects
            .select_related('handover_from', 'handover_to')
            .prefetch_related(
                'items__originator', 'items__previous_operator',
                'items__accepted_by', 'items__returned_by',
                'items__content_type',
                'events__actor', 'events__item',
            )
        )
        .filter(pk=session_id)
        .first()
    )


# 事项归集规则：状态 -> 查询集
def _pending_stock_outs(handover_at):
    """待审批或已批准但尚未完成出库的记录。"""
    return StockOut.objects.filter(
        status__in=['pending', 'approved'], created_at__lte=handover_at
    ).select_related('goods', 'goods__variety', 'created_by', 'operator')


def _pending_stock_ins(handover_at):
    """登记但尚未完成入库的记录。"""
    return StockIn.objects.filter(
        status='pending', created_at__lte=handover_at
    ).select_related('goods', 'goods__variety', 'created_by', 'operator')


def _snapshot_stock_out(obj):
    return {
        'goods_name': obj.goods.name,
        'goods_code': obj.goods.code,
        'variety_name': getattr(obj.goods.variety, 'name', ''),
        'quantity': str(obj.quantity),
        'receiver': obj.receiver,
        'receiver_dept': obj.receiver_dept,
        'status': obj.status,
        'remark': obj.remark,
        'created_at': obj.created_at.isoformat(),
    }


def _snapshot_stock_in(obj):
    return {
        'goods_name': obj.goods.name,
        'goods_code': obj.goods.code,
        'variety_name': getattr(obj.goods.variety, 'name', ''),
        'quantity': str(obj.quantity),
        'batch_no': obj.batch_no,
        'supplier': obj.supplier,
        'status': obj.status,
        'remark': obj.remark,
        'created_at': obj.created_at.isoformat(),
    }


def collect_pending_items(handover_at):
    """归集指定时间点的全部未决事项，返回 [(类型, 记录, 快照), ...]。"""
    items = []
    for record in _pending_stock_outs(handover_at):
        items.append(('stock_out', record, _snapshot_stock_out(record)))
    for record in _pending_stock_ins(handover_at):
        items.append(('stock_in', record, _snapshot_stock_in(record)))
    return items


class HandoverError(Exception):
    """交班业务错误（消息可直接返回前端）。"""

    def __init__(self, message, code=400):
        self.message = message
        self.code = code
        super().__init__(message)


@transaction.atomic
def create_session(*, handover_at, handover_from, handover_to, remark=''):
    """创建交班会话并冻结未决事项清单。"""
    if handover_from == handover_to:
        raise HandoverError('交班人与接班人不能为同一人')
    if handover_at > timezone.now():
        raise HandoverError('交班时间点不能晚于当前时间')

    pending = collect_pending_items(handover_at)
    if not pending:
        raise HandoverError('指定时间点没有未决事项，无需交班')

    session = HandoverSession.objects.create(
        handover_at=handover_at,
        handover_from=handover_from,
        handover_to=handover_to,
        remark=remark,
    )

    stock_out_ct = ContentType.objects.get_for_model(StockOut)
    stock_in_ct = ContentType.objects.get_for_model(StockIn)
    rows = []
    for item_type, record, snapshot in pending:
        ct = stock_out_ct if item_type == 'stock_out' else stock_in_ct
        rows.append(HandoverItem(
            session=session,
            item_type=item_type,
            content_type=ct,
            object_id=record.pk,
            snapshot=snapshot,
            originator=record.created_by,
            previous_operator=record.operator,
        ))
    try:
        HandoverItem.objects.bulk_create(rows)
    except IntegrityError as exc:  # 部分唯一索引冲突：同一记录全局只能有一个待确认事项
        raise HandoverError('存在未决事项已列入其他交班，待处理后再发起') from exc

    HandoverEvent.objects.create(
        session=session, action='create', actor=handover_from,
        detail=f'创建交班，列入{len(rows)}项未决事项',
    )
    return session


def _assert_open(session):
    if not session.is_open:
        raise HandoverError('该交班会话已结束，不能再操作', code=409)


def _assert_successor(session, user):
    if user != session.handover_to:
        raise HandoverError('只有指定接班人可以处理交班事项', code=403)


def _assert_handover_from(session, user):
    if user != session.handover_from:
        raise HandoverError('只有交班人可以执行此操作', code=403)


def _get_item(session, item_id):
    qs = HandoverItem.objects.select_related('content_type').select_for_update()
    try:
        item = qs.get(pk=item_id, session=session)
    except HandoverItem.DoesNotExist:
        raise HandoverError('交班事项不存在', code=404)
    if item.status != 'pending':
        raise HandoverError('该事项已处理，请勿重复操作', code=409)
    return item


def _assert_still_unresolved(item):
    """接受前复查原始记录确实仍未决，避免把已办结事项的责任改挂出去。"""
    model = item.content_type.model_class()
    unresolved_status = {'stock_out': ('pending', 'approved'), 'stock_in': ('pending',)}
    status = model.objects.filter(pk=item.object_id).values_list('status', flat=True).first()
    if status is None:
        raise HandoverError('事项对应的收发记录已不存在', code=409)
    if status not in unresolved_status[item.item_type]:
        raise HandoverError('该事项对应记录已办结，不能再转移处理责任', code=409)


def _transfer_operator(item, new_operator):
    """将事项对应收发记录的当前处理责任人改为接班人。"""
    model = item.content_type.model_class()
    model.objects.filter(pk=item.object_id).update(operator=new_operator)


@transaction.atomic
def accept_item(session_id, item_id, *, acceptee):
    """接班人确认接受单项：原子抢占，并发时仅一人成功。"""
    session = HandoverSession.objects.select_for_update().get(pk=session_id)
    _assert_open(session)
    _assert_successor(session, acceptee)

    item = _get_item(session, item_id)
    _assert_still_unresolved(item)

    # 单条条件更新充当原子“抢占锁”：UPDATE ... WHERE status='pending'
    won = HandoverItem.objects.filter(pk=item.pk, status='pending').update(
        status='accepted',
        accepted_by=acceptee,
        accepted_at=timezone.now(),
    )
    if won == 0:
        raise HandoverError('该事项刚被其他接班人接受，请刷新后重试', code=409)

    _transfer_operator(item, acceptee)
    HandoverEvent.objects.create(
        session=session, item=item, action='accept', actor=acceptee,
        detail=f'接受{item.get_item_type_display()}事项#{item.object_id}，处理责任转移',
    )
    return item


@transaction.atomic
def return_item(session_id, item_id, *, actor, reason):
    """退回单项：责任不转移，留在原处理人名下。"""
    if not reason or not reason.strip():
        raise HandoverError('退回时必须填写退回原因')

    session = HandoverSession.objects.select_for_update().get(pk=session_id)
    _assert_open(session)
    _assert_successor(session, actor)

    item = _get_item(session, item_id)
    updated = HandoverItem.objects.filter(pk=item.pk, status='pending').update(
        status='returned',
        returned_by=actor,
        returned_at=timezone.now(),
        return_reason=reason.strip(),
    )
    if updated == 0:
        raise HandoverError('该事项状态已变化，退回失败', code=409)

    HandoverEvent.objects.create(
        session=session, item=item, action='return', actor=actor,
        detail=f'退回{item.get_item_type_display()}事项#{item.object_id}，原因：{reason.strip()}',
    )
    return item


@transaction.atomic
def confirm_session(session_id, *, confirmer):
    """交班人确认完成：所有事项必须已有定论（接受或退回）。"""
    session = HandoverSession.objects.select_for_update().get(pk=session_id)
    _assert_open(session)
    _assert_handover_from(session, confirmer)

    pending_ids = list(
        session.items.filter(status='pending').values_list('pk', flat=True)
    )
    if pending_ids:
        raise HandoverError(
            f'尚有{len(pending_ids)}项未确认（接受或退回），不能完成交班', code=409
        )

    session.status = 'confirmed'
    session.confirmed_at = timezone.now()
    session.save(update_fields=['status', 'confirmed_at', 'updated_at'])

    accepted = session.items.filter(status='accepted').count()
    returned = session.items.filter(status='returned').count()
    HandoverEvent.objects.create(
        session=session, action='confirm', actor=confirmer,
        detail=f'交班完成：接受{accepted}项，退回{returned}项',
    )
    return session


@transaction.atomic
def cancel_session(session_id, *, actor):
    """确认前取消交班：已接受事项的处理责任回滚到原处理人。"""
    session = HandoverSession.objects.select_for_update().get(pk=session_id)
    _assert_open(session)
    _assert_handover_from(session, actor)

    # 回滚已接受事项：operator 恢复为交班时的处理人
    accepted_items = list(
        session.items.filter(status='accepted').select_related('content_type')
    )
    for item in accepted_items:
        item.content_type.model_class().objects.filter(pk=item.object_id).update(
            operator=item.previous_operator
        )

    # 全部未终局事项随会话作废（已退回的保留退回轨迹）
    terminal_ids = list(
        session.items.exclude(status='returned').values_list('pk', flat=True)
    )
    HandoverItem.objects.filter(pk__in=terminal_ids).update(status='cancelled')

    session.status = 'cancelled'
    session.cancelled_at = timezone.now()
    session.save(update_fields=['status', 'cancelled_at', 'updated_at'])

    HandoverEvent.objects.create(
        session=session, action='cancel', actor=actor,
        detail=f'取消交班，{len(accepted_items)}项已接受事项的处理责任已回滚',
    )
    return session
