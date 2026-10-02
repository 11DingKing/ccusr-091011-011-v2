# 监管物资保管服务

该项目为监管仓、证物室和受控物资保管点提供服务端 API，覆盖人员授权、物资分类、批次登记、收发记录、审批、预警、审计日志与统计报表。数据保存在 SQLite，所有测试和接口验收均可在单个 Linux 应用容器内离线完成。

## 运行环境

- Python 3.11
- Django REST Framework
- SQLite

## 安装与初始化

```bash
python -m pip install -r backend/requirements.txt
cd backend
python manage.py migrate --run-syncdb
```

## 测试

```bash
cd backend
pytest -q
```

## 编译检查

```bash
python -m compileall -q backend
```

## 岗位交班

节假日轮班时，上一班挂起的待审批/未完成出库单可通过交班会话移交处理责任。
责任转移只改写出库单的 `current_handler`（当前处理责任人）；`created_by`
（原始创建人）、`operator`（历史操作人）与审批轨迹始终保留，清单事项另带
交班时点的单据快照。

- `POST /api/handovers/`：发起交班，按 `cutoff_time` 截取该时间点仍在本人
  责任范围内的未决单据（`pending`/`approved`）生成清单，可指定 `receiver_user`。
- `GET /api/handovers/{id}/`：查看会话及清单（含快照、处置状态与原因）。
- `POST /api/handovers/{id}/items/{item_id}/accept/`：接班人确认接受，责任转移
  至本人。两个接班人并发接受同一事项时仅一人成功，另一人收到 409。
- `POST /api/handovers/{id}/items/{item_id}/reject/`：退回单项（须填 `reason`），
  责任保留在交班人名下；可部分接受、部分退回。
- `POST /api/handovers/{id}/items/{item_id}/cancel/`：交班人在确认前取消单项。
- `POST /api/handovers/{id}/cancel/`：确认前取消整会（已有事项被接受时不允许）。
- 全部事项处置后会话自动完成；事项全部被取消时会话自动取消。

## API 验收

```bash
cd backend
python manage.py migrate --run-syncdb
python manage.py shell -c "from rest_framework.test import APIClient; from apps.authentication.models import User; u=User.objects.create_user('smoke','safe-pass',role='admin'); c=APIClient(); r=c.post('/api/auth/login/',{'username':'smoke','password':'safe-pass'},format='json'); print(r.status_code, bool(r.json()['data']['token']))"
```

## 容器

```bash
docker build -t custody-service .
docker run --rm custody-service
```
