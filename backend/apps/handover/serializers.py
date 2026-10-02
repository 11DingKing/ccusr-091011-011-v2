"""
岗位交班序列化器
"""
from rest_framework import serializers

from apps.authentication.models import User
from .models import HandoverSession, HandoverItem


class HandoverItemSerializer(serializers.ModelSerializer):
    """交班清单事项"""
    ref_type_display = serializers.CharField(source='get_ref_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    original_handler_name = serializers.CharField(
        source='original_handler.username', read_only=True)
    accepted_by_name = serializers.CharField(
        source='accepted_by.username', read_only=True)

    class Meta:
        model = HandoverItem
        fields = [
            'id', 'ref_type', 'ref_type_display', 'ref_id',
            'original_handler', 'original_handler_name',
            'accepted_by', 'accepted_by_name',
            'status', 'status_display', 'snapshot',
            'reject_reason', 'accepted_at', 'resolved_at',
            'created_at', 'updated_at',
        ]
        read_only_fields = fields


class HandoverSessionListSerializer(serializers.ModelSerializer):
    """交班会话列表项"""
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    handover_user_name = serializers.CharField(
        source='handover_user.username', read_only=True)
    receiver_user_name = serializers.CharField(
        source='receiver_user.username', read_only=True)
    pending_count = serializers.IntegerField(read_only=True)
    accepted_count = serializers.IntegerField(read_only=True)
    rejected_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = HandoverSession
        fields = [
            'id', 'handover_user', 'handover_user_name',
            'receiver_user', 'receiver_user_name',
            'cutoff_time', 'status', 'status_display', 'remark',
            'pending_count', 'accepted_count', 'rejected_count',
            'created_at', 'completed_at', 'cancelled_at',
        ]


class HandoverSessionDetailSerializer(HandoverSessionListSerializer):
    """交班会话详情（含清单）"""
    items = HandoverItemSerializer(many=True, read_only=True)

    class Meta(HandoverSessionListSerializer.Meta):
        fields = HandoverSessionListSerializer.Meta.fields + ['items', 'cancel_reason']


class HandoverCreateSerializer(serializers.Serializer):
    """发起交班"""
    cutoff_time = serializers.DateTimeField(required=True, error_messages={
        'required': '请指定交班截止时间点',
        'invalid': '截止时间点格式不正确',
    })
    receiver_user = serializers.IntegerField(required=False, allow_null=True)
    remark = serializers.CharField(required=False, allow_blank=True, max_length=500)

    def validate_receiver_user(self, value):
        if value is not None and not User.objects.filter(pk=value, is_active=True).exists():
            raise serializers.ValidationError('指定的接班人不存在或已停用')
        return value

    def validate(self, data):
        request = self.context.get('request')
        if request and data.get('receiver_user') == request.user.id:
            raise serializers.ValidationError('接班人不能是交班人本人')
        return data


class HandoverRejectSerializer(serializers.Serializer):
    """退回事项"""
    reason = serializers.CharField(
        required=True, max_length=200,
        error_messages={'required': '退回时必须填写退回原因', 'blank': '退回原因不能为空'})


class HandoverCancelSerializer(serializers.Serializer):
    """取消会话"""
    reason = serializers.CharField(required=False, allow_blank=True, max_length=200)
