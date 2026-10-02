"""
岗位交班测试

覆盖：
- 截点快照建会（时间点切片、责任人过滤）
- 接班人接受/退回、交班人取消、部分接受
- 原始创建人与操作轨迹保留
- 两个接班人并发接受同一事项仅一人成功
"""
import threading
from datetime import timedelta
from decimal import Decimal

from django.db import connection
from django.test import TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.authentication.backends import generate_token
from apps.authentication.models import User
from apps.warehouse.models import Approval, Category, Goods, StockOut, Unit, Variety

from .models import HandoverItem, HandoverConflict
from . import services


def make_user(username, role='user'):
    return User.objects.create_user(username, 'testpass123', role=role)


def make_goods(owner, code='DEV-001'):
    unit = Unit.objects.create(name='件', created_by=owner)
    category = Category.objects.create(name='受控器材', unit=unit, created_by=owner)
    variety = Variety.objects.create(name='记录终端', category=category, created_by=owner)
    return Goods.objects.create(
        variety=variety, name='执法记录终端', code=code,
        quantity=Decimal('10'), warning_threshold=Decimal('5'))


def make_stock_out(operator, goods, quantity='2', status='pending'):
    return StockOut.objects.create(
        goods=goods, operator=operator,
        receiver='领用人', quantity=Decimal(quantity), status=status)


def auth_client(user):
    client = APIClient()
    client.credentials(HTTP_AUTHORIZATION=f"Bearer {generate_token(user)}")
    return client


class HandoverServiceTest(TransactionTestCase):

    def setUp(self):
        self.shift_a = make_user('shift-a', role='admin')
        self.shift_b = make_user('shift-b', role='admin')
        self.outsider = make_user('outsider', role='user')
        self.goods = make_goods(self.shift_a)

    def test_new_stock_out_initializes_responsibility_from_operator(self):
        doc = make_stock_out(self.shift_a, self.goods)
        doc.refresh_from_db()
        self.assertEqual(doc.created_by_id, self.shift_a.id)
        self.assertEqual(doc.current_handler_id, self.shift_a.id)

    def test_create_session_snapshots_only_pending_owned_at_cutoff(self):
        old_pending = make_stock_out(self.shift_a, self.goods)
        cutoff = timezone.now()
        session, items = services.create_session(
            handover_user=self.shift_a, cutoff_time=cutoff,
            receiver_user=self.shift_b)
        # 截点之后新建的单据不入册
        newer = make_stock_out(self.shift_a, self.goods)
        self.assertEqual([d.id for d in items], [old_pending.id])

        snap = session.items.get().snapshot
        self.assertEqual(snap['id'], old_pending.id)
        self.assertEqual(snap['goods_name'], '执法记录终端')
        self.assertEqual(snap['current_handler'], self.shift_a.id)
        self.assertEqual(snap['created_by'], self.shift_a.id)

        self.assertFalse(HandoverItem.objects.filter(ref_id=newer.id).exists())

    def test_other_users_docs_not_collected(self):
        make_stock_out(self.shift_a, self.goods)
        _, items = services.create_session(
            handover_user=self.outsider, cutoff_time=timezone.now())
        self.assertEqual(len(items), 0)

    def test_unresolved_scope_includes_approved_but_excludes_finished(self):
        approved_unfinished = make_stock_out(self.shift_a, self.goods, status='approved')
        make_stock_out(self.shift_a, self.goods, status='completed')
        make_stock_out(self.shift_a, self.goods, status='rejected')

        _, items = services.create_session(
            handover_user=self.shift_a, cutoff_time=timezone.now())
        collected = {d.status for d in items}
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].id, approved_unfinished.id)
        self.assertEqual(collected, {'approved'})

    def test_only_one_open_session_per_handover_user(self):
        services.create_session(handover_user=self.shift_a, cutoff_time=timezone.now())
        with self.assertRaises(services.HandoverError):
            services.create_session(handover_user=self.shift_a, cutoff_time=timezone.now())

    def test_future_cutoff_rejected(self):
        with self.assertRaises(services.HandoverError):
            services.create_session(
                handover_user=self.shift_a,
                cutoff_time=timezone.now() + timedelta(minutes=5))

    def test_doc_already_pending_in_another_session_is_excluded(self):
        doc = make_stock_out(self.shift_a, self.goods)
        services.create_session(handover_user=self.shift_a, cutoff_time=timezone.now())

        # 责任在单据仍处于待确认清单期间漂移到另一人（如线下调整），
        # 该人发起交班时该单据应被跳过而不是触发唯一约束
        doc.current_handler = self.shift_b
        doc.save(update_fields=['current_handler'])

        session_b, items_b = services.create_session(
            handover_user=self.shift_b, cutoff_time=timezone.now())
        self.assertEqual(len(items_b), 0)
        self.assertEqual(session_b.items.count(), 0)


class HandoverAcceptRejectTest(TransactionTestCase):

    def setUp(self):
        self.shift_a = make_user('shift-a', role='admin')
        self.shift_b = make_user('shift-b', role='admin')
        self.shift_c = make_user('shift-c', role='admin')
        self.goods = make_goods(self.shift_a)
        self.doc1 = make_stock_out(self.shift_a, self.goods)
        self.doc2 = make_stock_out(self.shift_a, self.goods)
        # 交班时点之前对 doc2 已留有一条审批轨迹
        Approval.objects.create(stock_out=self.doc2, approver=self.shift_a,
                                status='rejected', remark='请补充领用部门')
        self.session, _ = services.create_session(
            handover_user=self.shift_a, cutoff_time=timezone.now(),
            receiver_user=self.shift_b)
        self.item1 = self.session.items.get(ref_id=self.doc1.id)
        self.item2 = self.session.items.get(ref_id=self.doc2.id)

    def test_accept_transfers_handler_but_keeps_creator_and_trail(self):
        self.assertTrue(self.item1.accept(self.shift_b))

        self.doc1.refresh_from_db()
        self.assertEqual(self.doc1.current_handler_id, self.shift_b.id)
        # 原始创建人与原操作轨迹不动
        self.assertEqual(self.doc1.created_by_id, self.shift_a.id)
        self.assertEqual(self.doc1.operator_id, self.shift_a.id)

        self.item1.refresh_from_db()
        self.assertEqual(self.item1.status, 'accepted')
        self.assertEqual(self.item1.accepted_by_id, self.shift_b.id)
        # 快照保留交班时的责任归属
        self.assertEqual(self.item1.snapshot['current_handler'], self.shift_a.id)
        # 审批轨迹留存在快照与业务表两处可查
        self.assertEqual(
            self.item2.snapshot['approvals'][0]['remark'], '请补充领用部门')
        self.assertEqual(self.doc2.approvals.count(), 1)

    def test_partial_accept_then_reject_completes_session(self):
        self.assertTrue(self.item1.accept(self.shift_b))
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, 'open')

        self.assertTrue(self.item2.reject(self.shift_b, '单据信息不全，需原班核实'))
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, 'completed')
        self.assertIsNotNone(self.session.completed_at)

        self.doc1.refresh_from_db()
        self.doc2.refresh_from_db()
        self.item2.refresh_from_db()
        # 接受的转移、退回的留在交班人名下
        self.assertEqual(self.doc1.current_handler_id, self.shift_b.id)
        self.assertEqual(self.doc2.current_handler_id, self.shift_a.id)
        self.assertEqual(self.item2.reject_reason, '单据信息不全，需原班核实')

    def test_accept_completed_doc_raises_and_stays_pending(self):
        # 交班期间单据已实物出库完成
        self.doc1.status = 'completed'
        self.doc1.stock_out_time = timezone.now()
        self.doc1.save(update_fields=['status', 'stock_out_time'])

        with self.assertRaises(HandoverConflict):
            self.item1.accept(self.shift_b)
        self.item1.refresh_from_db()
        self.assertEqual(self.item1.status, 'pending')
        self.assertIsNone(self.item1.accepted_by_id)

    def test_cannot_resolve_twice(self):
        self.assertTrue(self.item1.accept(self.shift_b))
        # 已接受的事项不能再被其他人退回
        self.assertFalse(self.item1.reject(self.shift_c, '抢单'))
        self.item1.refresh_from_db()
        self.assertEqual(self.item1.accepted_by_id, self.shift_b.id)
        self.assertEqual(self.item1.status, 'accepted')

    def test_cancel_item_by_handover_user(self):
        self.assertTrue(self.item1.cancel())
        self.item1.refresh_from_db()
        self.assertEqual(self.item1.status, 'cancelled')
        self.doc1.refresh_from_db()
        # 取消不转移责任
        self.assertEqual(self.doc1.current_handler_id, self.shift_a.id)

    def test_cancel_whole_session_before_confirmation(self):
        services.cancel_session(self.session, user=self.shift_a, reason='临时延长值班')
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, 'cancelled')
        self.assertEqual(self.session.cancel_reason, '临时延长值班')
        self.assertEqual(self.session.items.exclude(status='cancelled').count(), 0)

        self.doc1.refresh_from_db()
        self.doc2.refresh_from_db()
        self.assertEqual(self.doc1.current_handler_id, self.shift_a.id)
        self.assertEqual(self.doc2.current_handler_id, self.shift_a.id)

    def test_cancel_session_after_partial_accept_is_rejected(self):
        self.item1.accept(self.shift_b)
        with self.assertRaises(services.HandoverError):
            services.cancel_session(self.session, user=self.shift_a)
        self.session.refresh_from_db()
        self.assertEqual(self.session.status, 'open')

    def test_cancel_session_permission(self):
        with self.assertRaises(services.HandoverError):
            services.cancel_session(self.session, user=self.shift_b)


class HandoverConcurrentAcceptTest(TransactionTestCase):
    """两个接班人并发接受同一事项，只能有一人成功。"""

    def setUp(self):
        self.shift_a = make_user('shift-a', role='admin')
        self.shift_b = make_user('shift-b', role='admin')
        self.shift_c = make_user('shift-c', role='admin')
        goods = make_goods(self.shift_a)
        self.doc = make_stock_out(self.shift_a, goods)
        self.session, _ = services.create_session(
            handover_user=self.shift_a, cutoff_time=timezone.now(),
            receiver_user=None)  # 不指定接班人，两名接班人都可接单

    def test_two_receivers_only_one_wins(self):
        item_id = self.session.items.get().id
        results = {}
        errors = []
        barrier = threading.Barrier(2)

        def accept(user, key):
            try:
                item = HandoverItem.objects.get(pk=item_id)
                barrier.wait(timeout=10)
                results[key] = item.accept(user)
            except Exception as exc:  # noqa: BLE001 - 收集后在主线程断言
                errors.append(exc)
            finally:
                # 只关闭当前线程自己的连接，不能用 close_all()：
                # 那会把另一线程仍在事务中的连接一起关掉，诱发假性锁错误
                connection.close()

        t1 = threading.Thread(target=accept, args=(self.shift_b, 'b'))
        t2 = threading.Thread(target=accept, args=(self.shift_c, 'c'))
        t1.start(); t2.start()
        t1.join(30); t2.join(30)

        self.assertEqual(errors, [], f'并发接受出现异常: {errors}')
        winners = [k for k, v in results.items() if v is True]
        self.assertEqual(len(winners), 1, f'期望唯一赢家，实际: {results}')

        item = HandoverItem.objects.get(pk=item_id)
        self.assertEqual(item.status, 'accepted')
        winner = self.shift_b if winners[0] == 'b' else self.shift_c
        self.assertEqual(item.accepted_by_id, winner.id)
        self.doc.refresh_from_db()
        self.assertEqual(self.doc.current_handler_id, winner.id)


class HandoverAPITest(TransactionTestCase):

    def setUp(self):
        self.shift_a = make_user('shift-a', role='admin')
        self.shift_b = make_user('shift-b', role='admin')
        self.goods = make_goods(self.shift_a)
        self.doc = make_stock_out(self.shift_a, self.goods)

    def _create_session(self, client_a, receiver=True):
        payload = {'cutoff_time': timezone.now().isoformat(),
                   'remark': '节假日轮班交接'}
        if receiver:
            payload['receiver_user'] = self.shift_b.id
        resp = client_a.post('/api/handovers/', payload, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()['data']

    def test_full_flow_via_api(self):
        client_a = auth_client(self.shift_a)
        client_b = auth_client(self.shift_b)

        session_data = self._create_session(client_a)
        self.assertEqual(len(session_data['items']), 1)
        self.assertEqual(session_data['pending_count'], 1)
        session_id = session_data['id']
        item_id = session_data['items'][0]['id']

        # 交班人不能接受自己交出的事项
        self.assertEqual(client_a.post(
            f'/api/handovers/{session_id}/items/{item_id}/accept/').status_code, 400)

        # 非指定接班人不能处置
        outsider = auth_client(make_user('outsider'))
        self.assertEqual(outsider.post(
            f'/api/handovers/{session_id}/items/{item_id}/accept/').status_code, 400)

        # 接班人接受
        accepted = client_b.post(
            f'/api/handovers/{session_id}/items/{item_id}/accept/')
        self.assertEqual(accepted.status_code, 200, accepted.content)
        self.assertEqual(accepted.json()['data']['accepted_count'], 1)
        self.assertEqual(accepted.json()['data']['status'], 'completed')

        # 重复接受被拒绝（并发抢占语义的串行表现）
        self.assertEqual(client_b.post(
            f'/api/handovers/{session_id}/items/{item_id}/accept/').status_code, 400)

        self.doc.refresh_from_db()
        self.assertEqual(self.doc.current_handler_id, self.shift_b.id)
        self.assertEqual(self.doc.created_by_id, self.shift_a.id)

    def test_reject_requires_reason_and_keeps_handler(self):
        client_a = auth_client(self.shift_a)
        client_b = auth_client(self.shift_b)
        session_data = self._create_session(client_a)
        session_id = session_data['id']
        item_id = session_data['items'][0]['id']

        no_reason = client_b.post(
            f'/api/handovers/{session_id}/items/{item_id}/reject/', {}, format='json')
        self.assertEqual(no_reason.status_code, 400)

        rejected = client_b.post(
            f'/api/handovers/{session_id}/items/{item_id}/reject/',
            {'reason': '缺少审批材料'}, format='json')
        self.assertEqual(rejected.status_code, 200)
        self.assertEqual(rejected.json()['data']['rejected_count'], 1)

        self.doc.refresh_from_db()
        self.assertEqual(self.doc.current_handler_id, self.shift_a.id)

    def test_cancel_item_api_auto_cancels_empty_session(self):
        client_a = auth_client(self.shift_a)
        data = self._create_session(client_a, receiver=False)
        resp = client_a.post(
            f"/api/handovers/{data['id']}/items/{data['items'][0]['id']}/cancel/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['data']['status'], 'cancelled')

        # 接班人不能取消交班人清单
        client_b = auth_client(self.shift_b)
        again = client_b.post(
            f"/api/handovers/{data['id']}/items/{data['items'][0]['id']}/cancel/")
        self.assertEqual(again.status_code, 400)

    def test_cancel_session_api(self):
        client_a = auth_client(self.shift_a)
        data = self._create_session(client_a)
        resp = client_a.post(f"/api/handovers/{data['id']}/cancel/",
                             {'reason': '排班调整'}, format='json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()['data']['status'], 'cancelled')

    def test_requires_authentication(self):
        self.assertEqual(APIClient().get('/api/handovers/').status_code, 401)

    def test_create_validates_cutoff_and_receiver(self):
        client_a = auth_client(self.shift_a)

        bad_time = client_a.post('/api/handovers/', {
            'cutoff_time': (timezone.now() + timedelta(hours=1)).isoformat(),
        }, format='json')
        self.assertEqual(bad_time.status_code, 400)

        bad_receiver = client_a.post('/api/handovers/', {
            'cutoff_time': timezone.now().isoformat(),
            'receiver_user': 99999,
        }, format='json')
        self.assertEqual(bad_receiver.status_code, 400)

        self_handover = client_a.post('/api/handovers/', {
            'cutoff_time': timezone.now().isoformat(),
            'receiver_user': self.shift_a.id,
        }, format='json')
        self.assertEqual(self_handover.status_code, 400)
