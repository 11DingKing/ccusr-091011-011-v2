"""
岗位交班视图
"""
import logging

from rest_framework.permissions import IsAuthenticated
from rest_framework.views import APIView

from apps.core.response import success_response, error_response

from .models import HandoverSession, HandoverItem, HandoverConflict
from .serializers import (
    HandoverSessionListSerializer, HandoverSessionDetailSerializer,
    HandoverCreateSerializer, HandoverRejectSerializer, HandoverCancelSerializer,
)
from . import services

logger = logging.getLogger('apps')


def _first_error(serializer):
    errors = serializer.errors
    first_error = list(errors.values())[0]
    if isinstance(first_error, list):
        first_error = first_error[0]
    return str(first_error)


class HandoverSessionListView(APIView):
    """交班会话列表 / 发起交班"""
    permission_classes = [IsAuthenticated]

    def get(self, request):
        queryset = HandoverSession.objects.select_related(
            'handover_user', 'receiver_user'
        ).all().order_by('-created_at')

        status = request.query_params.get('status')
        if status in dict(HandoverSession.STATUS_CHOICES):
            queryset = queryset.filter(status=status)

        role = request.query_params.get('role')
        if role == 'given':
            queryset = queryset.filter(handover_user=request.user)
        elif role == 'received':
            queryset = queryset.filter(receiver_user=request.user)

        try:
            page = max(int(request.query_params.get('page', 1)), 1)
            page_size = max(int(request.query_params.get('page_size', 10)), 1)
        except (TypeError, ValueError):
            page, page_size = 1, 10

        total = queryset.count()
        sessions = queryset[(page - 1) * page_size:page * page_size]
        return success_response(data={
            'list': HandoverSessionListSerializer(sessions, many=True).data,
            'total': total,
            'page': page,
            'page_size': page_size,
        })

    def post(self, request):
        """发起交班：按指定时间点把未决事项列入清单"""
        serializer = HandoverCreateSerializer(data=request.data, context={'request': request})
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer))

        receiver = None
        if serializer.validated_data.get('receiver_user'):
            from apps.authentication.models import User
            receiver = User.objects.get(pk=serializer.validated_data['receiver_user'])

        try:
            session, stock_outs = services.create_session(
                handover_user=request.user,
                cutoff_time=serializer.validated_data['cutoff_time'],
                receiver_user=receiver,
                remark=serializer.validated_data.get('remark', ''),
            )
        except services.HandoverError as exc:
            return error_response(message=str(exc))

        logger.info(
            "User %s created handover session %s at %s with %d items",
            request.user.username, session.id, session.cutoff_time, len(stock_outs),
        )
        return success_response(
            data=HandoverSessionDetailSerializer(session).data,
            message=f'交班清单已生成，共 {len(stock_outs)} 项未决事项',
        )


class HandoverSessionDetailView(APIView):
    """交班会话详情"""
    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        session = HandoverSession.objects.select_related(
            'handover_user', 'receiver_user'
        ).filter(pk=pk).first()
        if session is None:
            return error_response(message='交班会话不存在', code=404)
        return success_response(data=HandoverSessionDetailSerializer(session).data)


class HandoverSessionCancelView(APIView):
    """交班人在确认前取消整会"""
    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        session = HandoverSession.objects.filter(pk=pk).first()
        if session is None:
            return error_response(message='交班会话不存在', code=404)

        serializer = HandoverCancelSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer))

        try:
            services.cancel_session(
                session, user=request.user,
                reason=serializer.validated_data.get('reason', ''),
            )
        except services.HandoverError as exc:
            return error_response(message=str(exc))

        logger.info("User %s cancelled handover session %s",
                    request.user.username, session.id)
        return success_response(
            data=HandoverSessionDetailSerializer(session).data, message='交班会话已取消')


class _HandoverItemActionView(APIView):
    """事项处置公共基类：取会话、取事项、校验接班人资格"""
    permission_classes = [IsAuthenticated]

    def load(self, request, session_pk, item_pk):
        session = HandoverSession.objects.filter(pk=session_pk).first()
        if session is None:
            return None, None, error_response(message='交班会话不存在', code=404)

        item = session.items.filter(pk=item_pk).first()
        if item is None:
            return None, None, error_response(message='清单事项不存在', code=404)

        if session.status != 'open' or item.status != 'pending':
            return None, None, error_response(message='该事项已处置，不能重复操作')

        # 指定了接班人时仅本人可处置；未指定时本班其他在岗人员均可接单
        if session.receiver_user_id and session.receiver_user_id != request.user.id:
            return None, None, error_response(message='该交班已指定其他接班人')
        if request.user.id == session.handover_user_id:
            return None, None, error_response(message='交班人不能接受自己交出的事项')

        return session, item, None


class HandoverItemAcceptView(_HandoverItemActionView):
    """接班人确认接受：责任转移至本人"""

    def post(self, request, session_pk, item_pk):
        session, item, err = self.load(request, session_pk, item_pk)
        if err is not None:
            return err

        try:
            accepted = item.accept(request.user)
        except HandoverConflict as exc:
            return error_response(message=str(exc))

        if not accepted:
            # 并发抢占：条件 UPDATE 未命中，说明已被另一接班人先行处置
            return error_response(message='该事项刚被其他接班人确认，请刷新清单', code=409)

        session.refresh_from_db()
        logger.info(
            "User %s accepted handover item %s (stock_out=%s) in session %s",
            request.user.username, item.id, item.ref_id, session.id,
        )
        return success_response(
            data=HandoverSessionDetailSerializer(session).data,
            message='已接受，处理责任已转移至您的名下',
        )


class HandoverItemRejectView(_HandoverItemActionView):
    """接班人退回事项：责任留在交班人名下"""

    def post(self, request, session_pk, item_pk):
        session, item, err = self.load(request, session_pk, item_pk)
        if err is not None:
            return err

        serializer = HandoverRejectSerializer(data=request.data)
        if not serializer.is_valid():
            return error_response(message=_first_error(serializer))

        rejected = item.reject(request.user, serializer.validated_data['reason'])
        if not rejected:
            return error_response(message='该事项刚被其他接班人处置，请刷新清单', code=409)

        session.refresh_from_db()
        logger.info(
            "User %s rejected handover item %s in session %s: %s",
            request.user.username, item.id, session.id,
            serializer.validated_data['reason'],
        )
        return success_response(
            data=HandoverSessionDetailSerializer(session).data,
            message='已退回，该事项责任保留在交班人名下',
        )


class HandoverItemCancelView(APIView):
    """交班人在确认前取消单个事项"""
    permission_classes = [IsAuthenticated]

    def post(self, request, session_pk, item_pk):
        session = HandoverSession.objects.filter(pk=session_pk).first()
        if session is None:
            return error_response(message='交班会话不存在', code=404)
        if request.user.id != session.handover_user_id:
            return error_response(message='只有交班人可以取消清单事项')

        item = session.items.filter(pk=item_pk).first()
        if item is None:
            return error_response(message='清单事项不存在', code=404)
        if session.status != 'open' or item.status != 'pending':
            return error_response(message='该事项已处置，不能取消')

        if not item.cancel():
            return error_response(message='该事项刚被处置，请刷新清单', code=409)

        session.refresh_from_db()
        logger.info("User %s cancelled handover item %s in session %s",
                    request.user.username, item.id, session.id)
        return success_response(
            data=HandoverSessionDetailSerializer(session).data, message='事项已取消')
