"""
岗位交班URL配置
"""
from django.urls import path
from .views import (
    HandoverSessionListView, HandoverSessionDetailView, HandoverSessionCancelView,
    HandoverItemAcceptView, HandoverItemRejectView, HandoverItemCancelView,
)

urlpatterns = [
    # 交班会话
    path('handovers/', HandoverSessionListView.as_view(), name='handover-list'),
    path('handovers/<int:pk>/', HandoverSessionDetailView.as_view(), name='handover-detail'),
    path('handovers/<int:pk>/cancel/', HandoverSessionCancelView.as_view(),
         name='handover-cancel'),

    # 清单事项处置
    path('handovers/<int:session_pk>/items/<int:item_pk>/accept/',
         HandoverItemAcceptView.as_view(), name='handover-item-accept'),
    path('handovers/<int:session_pk>/items/<int:item_pk>/reject/',
         HandoverItemRejectView.as_view(), name='handover-item-reject'),
    path('handovers/<int:session_pk>/items/<int:item_pk>/cancel/',
         HandoverItemCancelView.as_view(), name='handover-item-cancel'),
]
