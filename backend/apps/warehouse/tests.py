from decimal import Decimal
from datetime import timedelta
import threading

from django.db import IntegrityError, connections
from django.test import TestCase, TransactionTestCase
from django.utils import timezone
from rest_framework.test import APIClient

from apps.authentication.backends import generate_token
from apps.authentication.models import User
from . import handover as handover_service
from .models import (
    Approval, Category, Goods, HandoverEvent, HandoverItem, HandoverSession,
    StockIn, StockOut, Unit, Variety, Warning,
)


class WarehouseFixture(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("warehouse-user", "testpass123", role="admin")
        self.client = APIClient()
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {generate_token(self.user)}")
        self.unit = Unit.objects.create(name="件", created_by=self.user)
        self.category = Category.objects.create(name="受控器材", unit=self.unit, created_by=self.user)
        self.variety = Variety.objects.create(name="记录终端", category=self.category, created_by=self.user)
        self.goods = Goods.objects.create(
            variety=self.variety,
            name="执法记录终端",
            code="DEV-001",
            quantity=Decimal("12"),
            warning_threshold=Decimal("5"),
        )


class WarehouseModelTest(WarehouseFixture):
    def test_relationship_flags(self):
        self.assertTrue(self.unit.is_linked)
        self.assertTrue(self.category.is_linked)
        self.assertTrue(self.variety.is_in_stock)
        self.assertFalse(self.goods.is_warning)

    def test_unique_unit_name(self):
        with self.assertRaises(IntegrityError):
            Unit.objects.create(name="件", created_by=self.user)

    def test_stock_records_and_approval(self):
        inbound = StockIn.objects.create(goods=self.goods, operator=self.user, quantity=Decimal("3"))
        outbound = StockOut.objects.create(
            goods=self.goods, operator=self.user, receiver="保管员", quantity=Decimal("2")
        )
        approval = Approval.objects.create(stock_out=outbound, approver=self.user)
        self.assertEqual(inbound.goods_id, self.goods.id)
        self.assertEqual(approval.status, "pending")

    def test_warning_record(self):
        warning = Warning.objects.create(goods=self.goods, type="low_stock", message="库存不足")
        self.assertFalse(warning.is_read)
        self.assertIn("执法记录终端", str(warning))


class WarehouseAPITest(WarehouseFixture):
    def test_list_units(self):
        response = self.client.get("/api/units/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["total"], 1)

    def test_create_unit_and_reject_duplicate(self):
        created = self.client.post("/api/units/", {"name": "箱"}, format="json")
        duplicate = self.client.post("/api/units/", {"name": "箱"}, format="json")
        self.assertEqual(created.status_code, 200)
        self.assertEqual(duplicate.status_code, 400)

    def test_update_linked_unit(self):
        response = self.client.put(f"/api/units/{self.unit.id}/", {"name": "台"}, format="json")
        self.assertEqual(response.status_code, 200)
        self.unit.refresh_from_db()
        self.assertEqual(self.unit.name, "台")

    def test_refuse_delete_linked_unit(self):
        response = self.client.delete(f"/api/units/{self.unit.id}/")
        self.assertEqual(response.status_code, 400)
        self.assertTrue(Unit.objects.filter(pk=self.unit.id).exists())

    def test_create_category_validates_unit(self):
        ok = self.client.post("/api/categories/", {"name": "封存介质", "unit": self.unit.id}, format="json")
        bad = self.client.post("/api/categories/", {"name": "无效分类", "unit": 99999}, format="json")
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(bad.status_code, 400)

    def test_create_variety_and_duplicate_boundary(self):
        ok = self.client.post("/api/varieties/", {"name": "封存硬盘", "category": self.category.id}, format="json")
        duplicate = self.client.post("/api/varieties/", {"name": "封存硬盘", "category": self.category.id}, format="json")
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(duplicate.status_code, 400)

    def test_requires_authentication(self):
        anonymous = APIClient().get("/api/units/")
        self.assertEqual(anonymous.status_code, 401)


# ==================== 岗位交班测试 ====================

class HandoverFixture(WarehouseFixture):
    """交班测试夹具：交班人 self.user，接班人 successor，另有旁观用户 other"""

    def setUp(self):
        super().setUp()
        self.successor = User.objects.create_user("next-shift", "testpass123", role="admin")
        self.other = User.objects.create_user("bystander", "testpass123", role="admin")
        self.successor_client = self._client_for(self.successor)
        self.other_client = self._client_for(self.other)

        now = timezone.now()
        self.out_pending = StockOut.objects.create(
            goods=self.goods, operator=self.user, receiver="张三",
            quantity=Decimal("1"), status="pending",
        )
        self.out_approved = StockOut.objects.create(
            goods=self.goods, operator=self.user, receiver="李四",
            quantity=Decimal("2"), status="approved",
        )
        self.out_completed = StockOut.objects.create(
            goods=self.goods, operator=self.user, receiver="王五",
            quantity=Decimal("3"), status="completed",
        )
        self.in_pending = StockIn.objects.create(
            goods=self.goods, operator=self.user,
            quantity=Decimal("5"), status="pending", supplier="供应商甲",
        )
        self.in_completed = StockIn.objects.create(
            goods=self.goods, operator=self.user,
            quantity=Decimal("7"), status="completed",
        )
        # 交班时间点在全部记录落库之后采集
        self.cutoff = timezone.now()
        self.assertGreaterEqual(self.cutoff, now)

    def _client_for(self, user):
        client = APIClient()
        client.credentials(HTTP_AUTHORIZATION=f"Bearer {generate_token(user)}")
        return client

    def create_handover(self, client=None, handover_at=None, handover_to=None):
        client = client or self.client
        payload = {
            "handover_at": (handover_at or self.cutoff).isoformat(),
            "handover_to": (handover_to or self.successor).id,
            "remark": "节假日轮班交接",
        }
        return client.post("/api/handovers/", payload, format="json")

    def item_for(self, session_data, record):
        item_type = "stock_out" if isinstance(record, StockOut) else "stock_in"
        matches = [
            i for i in session_data["items"]
            if i["item_type"] == item_type and i["object_id"] == record.id
        ]
        self.assertEqual(len(matches), 1)
        return matches[0]


class HandoverCollectionTest(HandoverFixture):
    def test_collects_only_unresolved_at_cutoff(self):
        items = handover_service.collect_pending_items(self.cutoff)
        keys = {(t, r.pk) for t, r, _ in items}
        self.assertIn(("stock_out", self.out_pending.pk), keys)
        self.assertIn(("stock_out", self.out_approved.pk), keys)
        self.assertIn(("stock_in", self.in_pending.pk), keys)
        self.assertNotIn(("stock_out", self.out_completed.pk), keys)
        self.assertNotIn(("stock_in", self.in_completed.pk), keys)

    def test_cutoff_excludes_later_records(self):
        # 用早于全部记录的时间点交班，归集结果应为空
        early = self.cutoff - timedelta(days=1)
        self.assertEqual(handover_service.collect_pending_items(early), [])

    def test_create_without_pending_items_rejected(self):
        StockOut.objects.filter(status__in=["pending", "approved"]).update(status="completed")
        StockIn.objects.filter(status="pending").update(status="completed")
        response = self.create_handover()
        self.assertEqual(response.status_code, 400)
        self.assertIn("没有未决事项", response.json()["message"])

    def test_same_user_handover_rejected(self):
        response = self.create_handover(handover_to=self.user)
        self.assertEqual(response.status_code, 400)

    def test_snapshot_freezes_fields_and_originator(self):
        response = self.create_handover()
        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["total_count"], 3)
        item = self.item_for(data, self.out_pending)
        self.assertEqual(item["originator_name"], self.user.username)
        self.assertEqual(item["snapshot"]["goods_name"], "执法记录终端")
        self.assertEqual(item["snapshot"]["receiver"], "张三")

        # 原始记录随后被修改，快照不受影响
        self.out_pending.receiver = "改名的领用人"
        self.out_pending.save()
        detail = self.client.get(f"/api/handovers/{data['id']}/").json()["data"]
        snapshot_item = self.item_for(detail, self.out_pending)
        self.assertEqual(snapshot_item["snapshot"]["receiver"], "张三")


class HandoverAcceptReturnTest(HandoverFixture):
    def setUp(self):
        super().setUp()
        self.session_id = self.create_handover().json()["data"]["id"]

    def detail(self):
        return self.client.get(f"/api/handovers/{self.session_id}/").json()["data"]

    def test_accept_transfers_operator_but_keeps_creator(self):
        item = self.item_for(self.detail(), self.out_pending)
        response = self.successor_client.post(
            f"/api/handovers/{self.session_id}/items/{item['id']}/accept/"
        )
        self.assertEqual(response.status_code, 200)

        self.out_pending.refresh_from_db()
        self.assertEqual(self.out_pending.operator_id, self.successor.id)
        self.assertEqual(self.out_pending.created_by_id, self.user.id)

        accepted = self.item_for(response.json()["data"], self.out_pending)
        self.assertEqual(accepted["status"], "accepted")
        self.assertEqual(accepted["accepted_by_name"], self.successor.username)

        actions = [e["action"] for e in self.detail()["events"]]
        self.assertEqual(actions, ["create", "accept"])

    def test_inbound_accept_transfers_too(self):
        item = self.item_for(self.detail(), self.in_pending)
        response = self.successor_client.post(
            f"/api/handovers/{self.session_id}/items/{item['id']}/accept/"
        )
        self.assertEqual(response.status_code, 200)
        self.in_pending.refresh_from_db()
        self.assertEqual(self.in_pending.operator_id, self.successor.id)
        self.assertEqual(self.in_pending.created_by_id, self.user.id)

    def test_return_keeps_operator_and_requires_reason(self):
        item = self.item_for(self.detail(), self.out_pending)
        no_reason = self.successor_client.post(
            f"/api/handovers/{self.session_id}/items/{item['id']}/return/", {}, format="json"
        )
        self.assertEqual(no_reason.status_code, 400)

        ok = self.successor_client.post(
            f"/api/handovers/{self.session_id}/items/{item['id']}/return/",
            {"reason": "单据信息存疑，需交班人核实"}, format="json",
        )
        self.assertEqual(ok.status_code, 200)
        self.out_pending.refresh_from_db()
        self.assertEqual(self.out_pending.operator_id, self.user.id)
        returned = self.item_for(ok.json()["data"], self.out_pending)
        self.assertEqual(returned["status"], "returned")
        self.assertIn("存疑", returned["return_reason"])

    def test_non_successor_cannot_accept_or_return(self):
        item = self.item_for(self.detail(), self.out_pending)
        resp = self.other_client.post(
            f"/api/handovers/{self.session_id}/items/{item['id']}/accept/"
        )
        self.assertEqual(resp.status_code, 403)
        resp = self.other_client.post(
            f"/api/handovers/{self.session_id}/items/{item['id']}/return/",
            {"reason": "无权"}, format="json",
        )
        self.assertEqual(resp.status_code, 403)

    def test_duplicate_accept_rejected(self):
        item = self.item_for(self.detail(), self.out_pending)
        url = f"/api/handovers/{self.session_id}/items/{item['id']}/accept/"
        self.assertEqual(self.successor_client.post(url).status_code, 200)
        self.assertEqual(self.successor_client.post(url).status_code, 409)

    def test_accept_after_record_resolved_rejected(self):
        item = self.item_for(self.detail(), self.out_pending)
        self.out_pending.status = "completed"
        self.out_pending.save(update_fields=["status"])
        resp = self.successor_client.post(
            f"/api/handovers/{self.session_id}/items/{item['id']}/accept/"
        )
        self.assertEqual(resp.status_code, 409)
        self.out_pending.refresh_from_db()
        self.assertEqual(self.out_pending.operator_id, self.user.id)

    def test_second_open_session_for_same_record_rejected(self):
        with self.assertRaises(handover_service.HandoverError):
            handover_service.create_session(
                handover_at=self.cutoff,
                handover_from=self.user,
                handover_to=self.other,
            )


class HandoverLifecycleTest(HandoverFixture):
    def setUp(self):
        super().setUp()
        created = self.create_handover().json()["data"]
        self.session_id = created["id"]
        self.item_out = self.item_for(created, self.out_pending)["id"]
        self.item_in = self.item_for(created, self.in_pending)["id"]
        self.item_approved = self.item_for(created, self.out_approved)["id"]

    def test_partial_acceptance_then_confirm(self):
        # 尚有 pending 时不能确认
        self.assertEqual(
            self.client.post(f"/api/handovers/{self.session_id}/confirm/").status_code, 409
        )

        self.successor_client.post(
            f"/api/handovers/{self.session_id}/items/{self.item_out}/accept/"
        )
        self.successor_client.post(
            f"/api/handovers/{self.session_id}/items/{self.item_in}/accept/"
        )
        self.successor_client.post(
            f"/api/handovers/{self.session_id}/items/{self.item_approved}/return/",
            {"reason": "需继续走审批"}, format="json",
        )

        # 接班人不能替交班人确认
        self.assertEqual(
            self.successor_client.post(
                f"/api/handovers/{self.session_id}/confirm/"
            ).status_code, 403
        )

        confirmed = self.client.post(f"/api/handovers/{self.session_id}/confirm/")
        self.assertEqual(confirmed.status_code, 200)
        data = confirmed.json()["data"]
        self.assertEqual(data["status"], "confirmed")
        self.assertEqual(data["accepted_count"], 2)
        self.assertEqual(data["returned_count"], 1)
        self.assertEqual(data["pending_count"], 0)

        # 已结束的会话不能再操作事项
        repeat = self.successor_client.post(
            f"/api/handovers/{self.session_id}/items/{self.item_out}/accept/"
        )
        self.assertEqual(repeat.status_code, 409)

    def test_cancel_before_confirm_rolls_back_operators(self):
        self.successor_client.post(
            f"/api/handovers/{self.session_id}/items/{self.item_out}/accept/"
        )
        self.out_pending.refresh_from_db()
        self.assertEqual(self.out_pending.operator_id, self.successor.id)

        # 只有交班人能取消
        self.assertEqual(
            self.successor_client.post(
                f"/api/handovers/{self.session_id}/cancel/"
            ).status_code, 403
        )

        cancelled = self.client.post(f"/api/handovers/{self.session_id}/cancel/")
        self.assertEqual(cancelled.status_code, 200)
        self.assertEqual(cancelled.json()["data"]["status"], "cancelled")

        self.out_pending.refresh_from_db()
        self.assertEqual(self.out_pending.operator_id, self.user.id)
        self.assertEqual(self.out_pending.created_by_id, self.user.id)

        # 取消后不能再确认或接受
        self.assertEqual(
            self.client.post(f"/api/handovers/{self.session_id}/confirm/").status_code, 409
        )

    def test_chain_handover_after_confirm_keeps_original_creator(self):
        # 第一轮：A 交班给 B，B 接受全部并完成
        for item_id in (self.item_out, self.item_in, self.item_approved):
            self.successor_client.post(
                f"/api/handovers/{self.session_id}/items/{item_id}/accept/"
            )
        self.client.post(f"/api/handovers/{self.session_id}/confirm/")

        # 第二轮：B 交班给 C（同一未决事项可再次列入）
        third = User.objects.create_user("third-shift", "testpass123", role="admin")
        response = self.create_handover(
            client=self.successor_client, handover_to=third
        )
        self.assertEqual(response.status_code, 200)
        second_session = response.json()["data"]
        item = self.item_for(second_session, self.out_pending)
        self.assertEqual(item["originator_name"], self.user.username)
        self.assertEqual(item["previous_operator_name"], self.successor.username)

        third_client = self._client_for(third)
        accepted = third_client.post(
            f"/api/handovers/{second_session['id']}/items/{item['id']}/accept/"
        )
        self.assertEqual(accepted.status_code, 200)
        self.out_pending.refresh_from_db()
        self.assertEqual(self.out_pending.operator_id, third.id)
        self.assertEqual(self.out_pending.created_by_id, self.user.id)

    def test_records_can_rehandover_after_cancel(self):
        self.successor_client.post(
            f"/api/handovers/{self.session_id}/items/{self.item_out}/accept/"
        )
        self.client.post(f"/api/handovers/{self.session_id}/cancel/")

        # 取消后旧事项不再占用 pending 唯一索引，可重新发起
        response = self.create_handover(handover_to=self.other)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["pending_count"], 3)


class HandoverConcurrencyTest(TransactionTestCase):
    """两个接班人（同账号两台终端）并发接受同一事项，只能一人成功"""

    def setUp(self):
        self.handover_from = User.objects.create_user("shift-a", "testpass123", role="admin")
        self.successor = User.objects.create_user("shift-b", "testpass123", role="admin")
        unit = Unit.objects.create(name="件", created_by=self.handover_from)
        category = Category.objects.create(
            name="器材", unit=unit, created_by=self.handover_from
        )
        variety = Variety.objects.create(
            name="终端", category=category, created_by=self.handover_from
        )
        goods = Goods.objects.create(
            variety=variety, name="并发终端", code="RACE-1", quantity=Decimal("1")
        )
        self.stock_out = StockOut.objects.create(
            goods=goods, operator=self.handover_from,
            receiver="赵六", quantity=Decimal("1"),
        )
        self.session = handover_service.create_session(
            handover_at=timezone.now(),
            handover_from=self.handover_from,
            handover_to=self.successor,
        )
        self.item = HandoverItem.objects.get(session=self.session)
        self.barrier = threading.Barrier(2)

    def _race_accept(self, results, index):
        client = APIClient()
        client.credentials(
            HTTP_AUTHORIZATION=f"Bearer {generate_token(self.successor)}"
        )
        self.barrier.wait()
        try:
            response = client.post(
                f"/api/handovers/{self.session.id}/items/{self.item.id}/accept/"
            )
            results[index] = response.status_code
        finally:
            connections.close_all()

    def test_only_one_accept_wins(self):
        results = [None, None]
        threads = [
            threading.Thread(target=self._race_accept, args=(results, i))
            for i in range(2)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        self.assertEqual(sorted(results), [200, 409])
        self.stock_out.refresh_from_db()
        self.assertEqual(self.stock_out.operator_id, self.successor.id)
        self.assertEqual(self.stock_out.created_by_id, self.handover_from.id)
        accepted = HandoverItem.objects.get(pk=self.item.pk)
        self.assertEqual(accepted.status, "accepted")
        self.assertEqual(accepted.accepted_by_id, self.successor.id)
        self.assertEqual(
            HandoverEvent.objects.filter(action="accept").count(), 1
        )


class HandoverModelGuardTest(HandoverFixture):
    def test_created_by_defaults_to_operator_on_create(self):
        fresh = StockOut.objects.create(
            goods=self.goods, receiver="无创建人场景",
            quantity=Decimal("1"), operator=self.other,
        )
        self.assertEqual(fresh.created_by_id, self.other.id)

    def test_pending_item_partial_unique_index(self):
        self.create_handover()
        ct = HandoverItem.objects.first().content_type
        duplicate = HandoverItem(
            session=HandoverSession.objects.first(),
            item_type="stock_out",
            content_type=ct,
            object_id=self.out_pending.id,
        )
        with self.assertRaises(IntegrityError):
            duplicate.save()
