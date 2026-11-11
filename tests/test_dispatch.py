from datetime import date, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from hiclose.agent import ConversationStore, SalesAgent
from hiclose.api.app import create_app
from hiclose.api.service import HiCloseService
from hiclose.channels import WebChannel
from hiclose.logistics import DispatchBoard, DispatchError
from tests.fakes import FakeModel, make_shop
from tests.test_api import FakePayments

FIXTURE = Path(__file__).parent.parent / "fixtures" / "logistics.json"
DAY = date(2026, 11, 12)


def at(hhmm: str) -> datetime:
    return datetime.combine(DAY, datetime.strptime(hhmm, "%H:%M").time())


@pytest.fixture
def board():
    return DispatchBoard.from_json(FIXTURE, day=DAY)


def test_seed_plan_is_valid_and_some_shipments_wait(board):
    waiting = [s.shipment_id for s in board.shipments.values() if s.driver_id is None]
    assert waiting == ["S-105", "S-106", "S-107", "S-108"]


def test_a_fitting_move_is_accepted(board):
    assert board.assign("S-106", "d-lena", at("13:00")).ok
    assert board.shipments["S-106"].driver_id == "d-lena"


@pytest.mark.parametrize("shipment, driver, start, words", [
    ("S-106", "d-lena", at("11:30"), "isn't on shift"),          # runs into Lena's lunch break
    ("S-105", "d-lena", at("13:00"), "holds 8 parcels"),          # 34 parcels on a cargo bike
    ("S-107", "d-marcus", at("14:00"), "based at Brooklyn Hub"),  # Newark shipment, Brooklyn van
    ("S-106", "d-priya", at("12:00"), "isn't packed until"),      # before it's ready
    ("S-108", "d-marcus", at("12:00"), "must be delivered by"),   # finishes after its deadline
    ("S-108", "d-priya", at("09:00"), "already on S-101"),        # Priya is busy
])
def test_moves_that_break_a_rule_are_refused_with_a_reason(board, shipment, driver, start, words):
    placed = board.assign(shipment, driver, start)
    assert not placed.ok and words in placed.reason
    assert board.shipments[shipment].driver_id is None


def test_one_vehicle_cannot_be_out_twice(board):
    # Ana and Marcus share Van 2; Ana's S-102 runs 06:30-09:30, Marcus only starts at 10:00,
    # so put a run on Ana first, then try Marcus on the same van at an overlapping time.
    board.unassign("S-102")
    assert board.assign("S-102", "d-ana", at("07:00")).ok        # 07:00-10:00
    board.drivers["d-marcus"] = board.drivers["d-marcus"].__class__(
        "d-marcus", "Marcus", "van-2", ((at("09:00"), at("18:00")),))
    placed = board.assign("S-108", "d-marcus", at("09:00"))
    assert not placed.ok and "Van 2 is already out on S-102 with Ana" in placed.reason


def test_dock_slots_cap_departures_per_warehouse(board):
    newark = board.warehouses["wh-nj"]
    board.warehouses["wh-nj"] = newark.__class__(newark.warehouse_id, newark.name, newark.city,
                                                 newark.capacity_units, 1, newark.product_ids)
    placed = board.assign("S-107", "d-sam", at("15:30"))
    assert not placed.ok and "no dock slots left" in placed.reason


def test_auto_assign_plans_everything_it_can_with_the_same_rules(board):
    result = board.auto_assign()
    assert sorted(result["assigned"]) == ["S-105", "S-106", "S-107", "S-108"] and result["unassigned"] == []
    for s in board.shipments.values():
        assert board.check(s, board.drivers[s.driver_id], s.start).ok


def test_shipment_too_big_for_any_vehicle_is_left_with_a_reason(board):
    board.shipments["S-105"].parcels = 99
    result = board.auto_assign()
    assert {"shipment_id": "S-105", "reason": "No vehicle at Brooklyn Hub is big enough."} in result["unassigned"]


def test_paid_order_becomes_a_shipment_to_the_paypal_city_once(board):
    ship_to = {"name": {"full_name": "Riya S"}, "address": {"address_line_1": "1 Main St", "admin_area_2": "Austin", "admin_area_1": "TX"}}
    shipment = board.add_order("ORDER-1", "scarf-silk", ship_to, now=at("10:07"))
    assert shipment.warehouse_id == "wh-nj" and shipment.destination == "Austin, TX"
    assert shipment.grams == 150 and shipment.ready_at == at("10:45")
    assert board.add_order("ORDER-1", "scarf-silk", ship_to) is None
    view = board.view()
    assert "1 Main St" not in str(view) and "Riya" not in str(view)  # only the city is shown


def test_unknown_shipment_is_an_error(board):
    with pytest.raises(DispatchError):
        board.assign("S-999", "d-sam", at("12:00"))


def build():
    shop = make_shop()
    payments = FakePayments()
    shop.checkout = payments
    web = WebChannel()
    board = DispatchBoard.from_json(FIXTURE, day=DAY)
    service = HiCloseService(SalesAgent(FakeModel([]), shop, ConversationStore()), shop, payments, {"web": web}, dispatch=board)
    return TestClient(create_app(service, web)), service, board


def test_api_serves_the_board_with_live_inventory():
    client, _, _ = build()
    assert client.get("/dispatch").status_code == 200
    data = client.get("/api/dispatch").json()
    brooklyn = next(w for w in data["warehouses"] if w["id"] == "wh-bk")
    assert brooklyn["onHand"] == 13 and brooklyn["capacity"] == 40  # jacket 5 + shirt stock from the catalog
    assert {r["id"] for r in data["resources"]} >= {"unassigned", "d-priya", "d-lena"}
    assert any(r["resourceId"] == "d-lena" and r["startDate"].endswith("12:00") for r in data["offShift"])


def test_api_refuses_a_bad_drop_and_accepts_a_good_one():
    client, _, board = build()
    bad = client.post("/api/dispatch/assign", json={"shipment_id": "S-105", "driver_id": "d-lena", "start": f"{DAY}T13:00"}).json()
    assert bad["ok"] is False and "holds 8 parcels" in bad["reason"]
    good = client.post("/api/dispatch/assign", json={"shipment_id": "S-106", "driver_id": "d-lena", "start": f"{DAY}T13:00"}).json()
    assert good["ok"] is True and board.shipments["S-106"].driver_id == "d-lena"
    back = client.post("/api/dispatch/assign", json={"shipment_id": "S-106", "driver_id": "unassigned"}).json()
    assert back["ok"] is True and board.shipments["S-106"].driver_id is None
    assert client.post("/api/dispatch/assign", json={"shipment_id": "S-106", "driver_id": "d-lena", "start": "soon"}).status_code == 400


def test_api_auto_assign():
    client, _, _ = build()
    result = client.post("/api/dispatch/auto").json()
    assert len(result["assigned"]) == 4 and all(e["assigned"] for e in result["board"]["events"])


def test_paypal_confirmation_puts_the_order_on_the_board():
    import asyncio
    from datetime import timedelta, timezone
    from hiclose.paypal.orders import OrderRecord
    from hiclose.rules import Quote
    _, service, board = build()
    quote = Quote("Q-1", "jacket-denim", 8000, "USD", datetime.now(timezone.utc) + timedelta(minutes=15), "M")
    record = OrderRecord("ORDER-9", quote, "https://pay", status="PAID", ship_to={"address": {"admin_area_2": "Denver", "admin_area_1": "CO"}})
    asyncio.run(service.notify_payment(record))
    [shipment] = [s for s in board.shipments.values() if s.order_id == "ORDER-9"]
    assert shipment.warehouse_id == "wh-bk" and shipment.destination == "Denver, CO"


def test_board_runs_on_the_shops_timezone_not_the_servers_clock(monkeypatch):
    from zoneinfo import ZoneInfo
    import hiclose.logistics.board as board_module

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            # 01:30 UTC on 12 Nov is still the evening of 11 Nov in New York.
            moment = datetime(2026, 11, 12, 1, 30, tzinfo=ZoneInfo("UTC"))
            return moment.astimezone(tz) if tz else moment.replace(tzinfo=None)

    monkeypatch.setattr(board_module, "datetime", Clock)
    monkeypatch.setenv("SHOP_TIMEZONE", "America/New_York")
    board = DispatchBoard.from_json(FIXTURE)
    assert board.day == date(2026, 11, 11)
    view = board.view()
    assert view["now"] == "2026-11-11T20:30" and view["timezone"] == "America/New_York" and view["timezoneLabel"] == "EST"
    shipment = board.add_order("ORDER-TZ", "tote-canvas", None)
    assert shipment.ready_at == datetime(2026, 11, 11, 21, 0)


def test_timezone_can_be_changed(monkeypatch):
    monkeypatch.setenv("SHOP_TIMEZONE", "Asia/Kolkata")
    assert DispatchBoard.from_json(FIXTURE, day=DAY).view()["timezone"] == "Asia/Kolkata"
