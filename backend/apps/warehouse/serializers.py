"""
仓库管理序列化器
"""
from rest_framework import serializers
from .models import (
    Unit, Category, Variety, Goods, StockIn, StockOut, Warning, Approval,
    HandoverSession, HandoverItem, HandoverEvent,
)


class UnitSerializer(serializers.ModelSerializer):
    """单位序列化器"""
    is_linked = serializers.BooleanField(read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    
    class Meta:
        model = Unit
        fields = [
            'id', 'name', 'is_linked', 'is_active',
            'created_by', 'created_by_name', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class UnitCreateSerializer(serializers.Serializer):
    """单位创建序列化器"""
    name = serializers.CharField(min_length=1, max_length=5, required=True, error_messages={
        'required': '请输入单位名称',
        'blank': '单位名称不能为空',
        'min_length': '单位名称至少1个字',
        'max_length': '单位名称最多5个字',
    })
    
    def validate_name(self, value):
        instance = self.context.get('instance')
        if instance:
            if Unit.objects.filter(name=value).exclude(pk=instance.pk).exists():
                raise serializers.ValidationError('单位名称已存在')
        else:
            if Unit.objects.filter(name=value).exists():
                raise serializers.ValidationError('单位名称已存在')
        return value


class CategorySerializer(serializers.ModelSerializer):
    """品类序列化器"""
    is_linked = serializers.BooleanField(read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    unit_name = serializers.CharField(source='unit.name', read_only=True)
    
    class Meta:
        model = Category
        fields = [
            'id', 'name', 'unit', 'unit_name', 'is_linked', 'is_active',
            'created_by', 'created_by_name', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class CategoryCreateSerializer(serializers.Serializer):
    """品类创建序列化器"""
    name = serializers.CharField(min_length=1, max_length=10, required=True, error_messages={
        'required': '请输入品类名称',
        'blank': '品类名称不能为空',
        'min_length': '品类名称至少1个字',
        'max_length': '品类名称最多10个字',
    })
    unit = serializers.IntegerField(required=True, error_messages={
        'required': '请选择单位',
    })
    
    def validate_name(self, value):
        instance = self.context.get('instance')
        if instance:
            if Category.objects.filter(name=value).exclude(pk=instance.pk).exists():
                raise serializers.ValidationError('品类名称已存在')
        else:
            if Category.objects.filter(name=value).exists():
                raise serializers.ValidationError('品类名称已存在')
        return value
    
    def validate_unit(self, value):
        if not Unit.objects.filter(pk=value).exists():
            raise serializers.ValidationError('单位不存在')
        return value


class VarietySerializer(serializers.ModelSerializer):
    """品种序列化器"""
    is_in_stock = serializers.BooleanField(read_only=True)
    unit_name = serializers.CharField(read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    category_name = serializers.CharField(source='category.name', read_only=True)
    
    class Meta:
        model = Variety
        fields = [
            'id', 'name', 'category', 'category_name', 'unit_name',
            'is_in_stock', 'is_active',
            'created_by', 'created_by_name', 'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']


class VarietyCreateSerializer(serializers.Serializer):
    """品种创建序列化器"""
    name = serializers.CharField(min_length=1, max_length=20, required=True, error_messages={
        'required': '请输入品种名称',
        'blank': '品种名称不能为空',
        'min_length': '品种名称至少1个字',
        'max_length': '品种名称最多20个字',
    })
    category = serializers.IntegerField(required=True, error_messages={
        'required': '请选择品类',
    })
    
    def validate_category(self, value):
        if not Category.objects.filter(pk=value).exists():
            raise serializers.ValidationError('品类不存在')
        return value
    
    def validate(self, data):
        instance = self.context.get('instance')
        name = data['name']
        category_id = data['category']
        
        if instance:
            if Variety.objects.filter(name=name, category_id=category_id).exclude(pk=instance.pk).exists():
                raise serializers.ValidationError('该品类下已存在同名品种')
        else:
            if Variety.objects.filter(name=name, category_id=category_id).exists():
                raise serializers.ValidationError('该品类下已存在同名品种')
        return data


class GoodsSerializer(serializers.ModelSerializer):
    """货物序列化器"""
    variety_name = serializers.CharField(source='variety.name', read_only=True)
    category_name = serializers.CharField(source='variety.category.name', read_only=True)
    unit_name = serializers.CharField(source='variety.category.unit.name', read_only=True)
    is_warning = serializers.BooleanField(read_only=True)
    
    class Meta:
        model = Goods
        fields = [
            'id', 'name', 'code', 'variety', 'variety_name',
            'category_name', 'unit_name', 'specification',
            'quantity', 'warning_threshold', 'location',
            'remark', 'is_active', 'is_warning',
            'created_at', 'updated_at'
        ]


class StockInSerializer(serializers.ModelSerializer):
    """入库记录序列化器"""
    goods_name = serializers.CharField(source='goods.name', read_only=True)
    operator_name = serializers.CharField(source='operator.username', read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = StockIn
        fields = [
            'id', 'goods', 'goods_name',
            'created_by', 'created_by_name',
            'operator', 'operator_name',
            'quantity', 'status', 'status_display',
            'batch_no', 'supplier', 'stock_in_time', 'remark', 'created_at'
        ]


class StockOutSerializer(serializers.ModelSerializer):
    """出库记录序列化器"""
    goods_name = serializers.CharField(source='goods.name', read_only=True)
    operator_name = serializers.CharField(source='operator.username', read_only=True)
    created_by_name = serializers.CharField(source='created_by.username', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = StockOut
        fields = [
            'id', 'goods', 'goods_name',
            'created_by', 'created_by_name',
            'operator', 'operator_name',
            'receiver', 'receiver_dept', 'quantity', 'status', 'status_display',
            'stock_out_time', 'remark', 'created_at'
        ]


class WarningSerializer(serializers.ModelSerializer):
    """预警记录序列化器"""
    goods_name = serializers.CharField(source='goods.name', read_only=True)
    type_display = serializers.CharField(source='get_type_display', read_only=True)
    
    class Meta:
        model = Warning
        fields = [
            'id', 'goods', 'goods_name', 'type', 'type_display',
            'message', 'is_read', 'created_at'
        ]


class ApprovalSerializer(serializers.ModelSerializer):
    """审批记录序列化器"""
    approver_name = serializers.CharField(source='approver.username', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)

    class Meta:
        model = Approval
        fields = [
            'id', 'stock_out', 'approver', 'approver_name',
            'status', 'status_display', 'remark', 'created_at', 'updated_at'
        ]


# ==================== 岗位交班序列化器 ====================

class HandoverEventSerializer(serializers.ModelSerializer):
    """交班操作轨迹序列化器"""
    actor_name = serializers.CharField(source='actor.username', read_only=True)
    action_display = serializers.CharField(source='get_action_display', read_only=True)

    class Meta:
        model = HandoverEvent
        fields = [
            'id', 'item_id', 'action', 'action_display',
            'actor', 'actor_name', 'detail', 'created_at',
        ]


class HandoverItemSerializer(serializers.ModelSerializer):
    """交班事项序列化器"""
    item_type_display = serializers.CharField(source='get_item_type_display', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    originator_name = serializers.CharField(source='originator.username', read_only=True)
    previous_operator_name = serializers.CharField(source='previous_operator.username', read_only=True)
    accepted_by_name = serializers.CharField(source='accepted_by.username', read_only=True)
    returned_by_name = serializers.CharField(source='returned_by.username', read_only=True)

    class Meta:
        model = HandoverItem
        fields = [
            'id', 'item_type', 'item_type_display', 'object_id', 'snapshot',
            'originator', 'originator_name',
            'previous_operator', 'previous_operator_name',
            'accepted_by', 'accepted_by_name', 'accepted_at',
            'returned_by', 'returned_by_name', 'returned_at', 'return_reason',
            'status', 'status_display', 'created_at',
        ]


class HandoverSessionListSerializer(serializers.ModelSerializer):
    """交班会话列表序列化器"""
    handover_from_name = serializers.CharField(source='handover_from.username', read_only=True)
    handover_to_name = serializers.CharField(source='handover_to.username', read_only=True)
    status_display = serializers.CharField(source='get_status_display', read_only=True)
    total_count = serializers.SerializerMethodField()
    accepted_count = serializers.IntegerField(read_only=True)
    returned_count = serializers.IntegerField(read_only=True)
    pending_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = HandoverSession
        fields = [
            'id', 'handover_at', 'handover_from', 'handover_from_name',
            'handover_to', 'handover_to_name', 'status', 'status_display',
            'total_count', 'accepted_count', 'returned_count', 'pending_count',
            'remark', 'confirmed_at', 'cancelled_at', 'created_at',
        ]

    def get_total_count(self, obj):
        return obj.items.count() if hasattr(obj, 'items') else 0


class HandoverSessionDetailSerializer(HandoverSessionListSerializer):
    """交班会话详情序列化器（含事项清单与操作轨迹）"""
    items = HandoverItemSerializer(many=True, read_only=True)
    events = HandoverEventSerializer(many=True, read_only=True)

    class Meta(HandoverSessionListSerializer.Meta):
        fields = HandoverSessionListSerializer.Meta.fields + ['items', 'events']


class HandoverSessionCreateSerializer(serializers.Serializer):
    """发起交班序列化器"""
    handover_at = serializers.DateTimeField(
        required=True,
        error_messages={'required': '请选择交班时间点', 'invalid': '交班时间点格式不正确'},
    )
    handover_to = serializers.IntegerField(
        required=True,
        error_messages={'required': '请选择接班人'},
    )
    remark = serializers.CharField(required=False, allow_blank=True, max_length=1000)

    def validate_handover_to(self, value):
        from apps.authentication.models import User
        if not User.objects.filter(pk=value, is_active=True).exists():
            raise serializers.ValidationError('接班人不存在或已停用')
        return value


class HandoverReturnSerializer(serializers.Serializer):
    """退回事项序列化器"""
    reason = serializers.CharField(
        required=True, allow_blank=False, max_length=500,
        error_messages={'required': '请填写退回原因', 'blank': '退回原因不能为空'},
    )
