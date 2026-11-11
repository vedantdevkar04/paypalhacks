"""The dispatch board: warehouses, vehicles, drivers and shipments for one day.

Like the pricing rules, this is plain code: the page (Bryntum) only proposes a move, and
`assign()` decides. A move is refused, with a reason a person can read, when it breaks any of:
the driver's shift, the shipment's delivery window, the vehicle's home warehouse, the vehicle's
parcel or weight capacity, a double booking of the driver or the vehicle, or the warehouse's dock
slots for the day. Weights are integer grams; times are shop-local on the board's day.

"Shop-local" is SHOP_TIMEZONE (default America/New_York, where the demo warehouses are), not the
server's clock: Render runs on UTC, which would put the board on tomorrow every US evening."""

import itertools
import json
import os
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

SLOT = timedelta(minutes=15)
DAY_START, DAY_END = time(6, 0), time(22, 0)
DEFAULT_TIMEZONE = "America/New_York"


def shop_timezone() -> ZoneInfo:
    return ZoneInfo(os.environ.get("SHOP_TIMEZONE") or DEFAULT_TIMEZONE)


class DispatchError(Exception):
    pass


@dataclass(frozen=True)
class Warehouse:
    warehouse_id: str
    name: str
    city: str
    capacity_units: int
    dock_slots: int
    product_ids: tuple[str, ...]


@dataclass(frozen=True)
class Vehicle:
    vehicle_id: str
    name: str
    warehouse_id: str
    capacity_parcels: int
    capacity_grams: int


@dataclass(frozen=True)
class Driver:
    driver_id: str
    name: str
    vehicle_id: str
    shifts: tuple[tuple[datetime, datetime], ...]

    def on_shift(self, start: datetime, end: datetime) -> bool:
        return any(s <= start and end <= e for s, e in self.shifts)


@dataclass
class Shipment:
    shipment_id: str
    warehouse_id: str
    destination: str
    parcels: int
    grams: int
    ready_at: datetime
    due_by: datetime
    minutes: int
    driver_id: str | None = None
    start: datetime | None = None
    order_id: str | None = None  # set when the shipment came from a paid PayPal order

    @property
    def duration(self) -> timedelta:
        return timedelta(minutes=self.minutes)

    def end_if(self, start: datetime) -> datetime:
        return start + self.duration


def _overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    return a_start < b_end and b_start < a_end


@dataclass
class Placement:
    ok: bool
    reason: str = ""


@dataclass
class DispatchBoard:
    day: date
    warehouses: dict[str, Warehouse]
    vehicles: dict[str, Vehicle]
    drivers: dict[str, Driver]
    shipments: dict[str, Shipment]
    parcel_grams: dict[str, int]
    tz: ZoneInfo = field(default_factory=shop_timezone)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1), repr=False)

    # ---- loading -------------------------------------------------------------------------------

    @classmethod
    def from_json(cls, path: str | Path, day: date | None = None, tz: ZoneInfo | None = None) -> "DispatchBoard":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        tz = tz or shop_timezone()
        day = day or datetime.now(tz).date()

        def at(hhmm: str) -> datetime:
            return datetime.combine(day, time.fromisoformat(hhmm))

        warehouses = {w["warehouse_id"]: Warehouse(w["warehouse_id"], w["name"], w["city"], w["capacity_units"],
                                                    w["dock_slots"], tuple(w["product_ids"])) for w in data["warehouses"]}
        vehicles = {v["vehicle_id"]: Vehicle(v["vehicle_id"], v["name"], v["warehouse_id"], v["capacity_parcels"],
                                             v["capacity_kg"] * 1000) for v in data["vehicles"]}
        drivers = {d["driver_id"]: Driver(d["driver_id"], d["name"], d["vehicle_id"],
                                          tuple((at(s), at(e)) for s, e in d["shifts"])) for d in data["drivers"]}
        board = cls(day, warehouses, vehicles, drivers, {}, dict(data.get("parcel_grams", {})), tz)
        for s in data["shipments"]:
            shipment = Shipment(s["shipment_id"], s["warehouse_id"], s["destination"], s["parcels"], s["kg"] * 1000,
                                at(s["ready"]), at(s["due"]), s["minutes"])
            board.shipments[shipment.shipment_id] = shipment
            if s.get("driver_id"):
                placed = board.assign(shipment.shipment_id, s["driver_id"], at(s["start"]))
                if not placed.ok:
                    raise DispatchError(f"seed shipment {shipment.shipment_id}: {placed.reason}")
        for vehicle in vehicles.values():
            if vehicle.warehouse_id not in warehouses:
                raise DispatchError(f"{vehicle.vehicle_id}: unknown warehouse {vehicle.warehouse_id}")
        for driver in drivers.values():
            if driver.vehicle_id not in vehicles:
                raise DispatchError(f"{driver.driver_id}: unknown vehicle {driver.vehicle_id}")
        return board

    def local_now(self) -> datetime:
        """The shop's wall-clock time, without tzinfo, to compare with the board's times."""
        return datetime.now(self.tz).replace(tzinfo=None, second=0, microsecond=0)

    # ---- the rules -----------------------------------------------------------------------------

    def check(self, shipment: Shipment, driver: Driver, start: datetime) -> Placement:
        vehicle = self.vehicles[driver.vehicle_id]
        end = shipment.end_if(start)
        clock = lambda t: t.strftime("%H:%M")  # noqa: E731
        if start.date() != self.day or end.date() != self.day:
            return Placement(False, "This board only plans today's deliveries.")
        if not driver.on_shift(start, end):
            shifts = ", ".join(f"{clock(s)}–{clock(e)}" for s, e in driver.shifts)
            return Placement(False, f"{driver.name} isn't on shift for all of {clock(start)}–{clock(end)} (shift: {shifts}).")
        if start < shipment.ready_at:
            return Placement(False, f"{shipment.shipment_id} isn't packed until {clock(shipment.ready_at)}.")
        if end > shipment.due_by:
            return Placement(False, f"{shipment.shipment_id} must be delivered by {clock(shipment.due_by)}; this run ends {clock(end)}.")
        if vehicle.warehouse_id != shipment.warehouse_id:
            return Placement(False, f"{vehicle.name} is based at {self.warehouses[vehicle.warehouse_id].name}, "
                                    f"but {shipment.shipment_id} leaves from {self.warehouses[shipment.warehouse_id].name}.")
        if shipment.parcels > vehicle.capacity_parcels:
            return Placement(False, f"{vehicle.name} holds {vehicle.capacity_parcels} parcels; {shipment.shipment_id} has {shipment.parcels}.")
        if shipment.grams > vehicle.capacity_grams:
            return Placement(False, f"{vehicle.name} carries {vehicle.capacity_grams // 1000} kg; "
                                    f"{shipment.shipment_id} weighs {shipment.grams / 1000:g} kg.")
        for other in self.shipments.values():
            if other is shipment or other.driver_id is None or other.start is None:
                continue
            if not _overlaps(start, end, other.start, other.end_if(other.start)):
                continue
            if other.driver_id == driver.driver_id:
                return Placement(False, f"{driver.name} is already on {other.shipment_id} then.")
            if self.drivers[other.driver_id].vehicle_id == vehicle.vehicle_id:
                return Placement(False, f"{vehicle.name} is already out on {other.shipment_id} with {self.drivers[other.driver_id].name}.")
        warehouse = self.warehouses[shipment.warehouse_id]
        booked = sum(1 for s in self.shipments.values()
                     if s is not shipment and s.driver_id and s.warehouse_id == warehouse.warehouse_id)
        if booked >= warehouse.dock_slots:
            return Placement(False, f"{warehouse.name} has no dock slots left today ({warehouse.dock_slots} used).")
        return Placement(True)

    # ---- changes -------------------------------------------------------------------------------

    def _shipment(self, shipment_id: str) -> Shipment:
        try:
            return self.shipments[shipment_id]
        except KeyError:
            raise DispatchError(f"unknown shipment {shipment_id}") from None

    def assign(self, shipment_id: str, driver_id: str, start: datetime) -> Placement:
        with self._lock:
            shipment = self._shipment(shipment_id)
            driver = self.drivers.get(driver_id)
            if driver is None:
                return Placement(False, f"unknown driver {driver_id}")
            start = start.replace(second=0, microsecond=0, tzinfo=None)
            placed = self.check(shipment, driver, start)
            if placed.ok:
                shipment.driver_id, shipment.start = driver_id, start
            return placed

    def unassign(self, shipment_id: str) -> None:
        with self._lock:
            shipment = self._shipment(shipment_id)
            shipment.driver_id, shipment.start = None, None

    def auto_assign(self) -> dict:
        """Plan every unassigned shipment, earliest deadline first, on the smallest vehicle that fits,
        at the earliest start the rules allow. Uses the same check() as a manual move."""
        placed, left = [], []
        with self._lock:
            waiting = sorted((s for s in self.shipments.values() if s.driver_id is None), key=lambda s: (s.due_by, s.ready_at))
            for shipment in waiting:
                best = None
                drivers = sorted(self.drivers.values(), key=lambda d: (self.vehicles[d.vehicle_id].capacity_parcels, d.name))
                for driver in drivers:
                    start = shipment.ready_at
                    while shipment.end_if(start) <= shipment.due_by:
                        if self.check(shipment, driver, start).ok:
                            if best is None or start < best[1]:
                                best = (driver, start)
                            break
                        start += SLOT
                if best:
                    shipment.driver_id, shipment.start = best[0].driver_id, best[1]
                    placed.append(shipment.shipment_id)
                else:
                    left.append({"shipment_id": shipment.shipment_id, "reason": self._why_not(shipment)})
        return {"assigned": placed, "unassigned": left}

    def _why_not(self, shipment: Shipment) -> str:
        fits = [v for v in self.vehicles.values() if v.warehouse_id == shipment.warehouse_id
                and shipment.parcels <= v.capacity_parcels and shipment.grams <= v.capacity_grams]
        if not fits:
            return f"No vehicle at {self.warehouses[shipment.warehouse_id].name} is big enough."
        return "No driver with that vehicle is free inside the delivery window, or the dock is full."

    def add_order(self, order_id: str, product_id: str, ship_to: dict | None, now: datetime | None = None) -> Shipment | None:
        """A paid PayPal order becomes a one-parcel shipment from the warehouse that stocks it, to the
        address on the PayPal transaction (hard rule 5). Only the city is shown on the board."""
        warehouse = next((w for w in self.warehouses.values() if product_id in w.product_ids), None)
        if warehouse is None:
            return None
        with self._lock:
            if any(s.order_id == order_id for s in self.shipments.values()):
                return None
            now = (now or self.local_now()).replace(second=0, microsecond=0, tzinfo=None)
            day_open = datetime.combine(self.day, DAY_START)
            ready = max(day_open, now + (SLOT - timedelta(minutes=now.minute % 15)) % SLOT + SLOT * 2)
            address = (ship_to or {}).get("address", {})
            city = ", ".join(p for p in (address.get("admin_area_2"), address.get("admin_area_1")) if p)
            shipment = Shipment(
                shipment_id=f"P-{next(self._ids):03d}", warehouse_id=warehouse.warehouse_id,
                destination=city or "PayPal shipping address", parcels=1,
                grams=self.parcel_grams.get(product_id, 500), ready_at=ready,
                due_by=datetime.combine(self.day, DAY_END), minutes=45, order_id=order_id,
            )
            self.shipments[shipment.shipment_id] = shipment
            return shipment

    # ---- what the page shows -------------------------------------------------------------------

    def view(self, stock_of=None) -> dict:
        """Everything the Bryntum page needs. stock_of(product_id) gives live units on hand."""
        iso = lambda t: t.isoformat(timespec="minutes")  # noqa: E731
        day_open, day_close = datetime.combine(self.day, DAY_START), datetime.combine(self.day, DAY_END)
        resources = [{"id": "unassigned", "name": "Unassigned", "vehicle": "Drag a shipment onto a driver",
                      "capacity": "", "shift": "", "cls": "unassigned-row"}]
        off = []
        for d in self.drivers.values():
            v = self.vehicles[d.vehicle_id]
            resources.append({
                "id": d.driver_id, "name": d.name, "vehicle": v.name,
                "warehouse": self.warehouses[v.warehouse_id].name,
                "capacity": f"{v.capacity_parcels} parcels · {v.capacity_grams // 1000} kg",
                "shift": ", ".join(f"{s:%H:%M}–{e:%H:%M}" for s, e in d.shifts),
            })
            cursor = day_open
            for s, e in sorted(d.shifts):
                if s > cursor:
                    off.append({"id": f"{d.driver_id}-off-{len(off)}", "resourceId": d.driver_id, "name": "Off shift",
                                "startDate": iso(cursor), "endDate": iso(s), "timeRangeColor": "gray"})
                cursor = max(cursor, e)
            if cursor < day_close:
                off.append({"id": f"{d.driver_id}-off-{len(off)}", "resourceId": d.driver_id, "name": "Off shift",
                            "startDate": iso(cursor), "endDate": iso(day_close), "timeRangeColor": "gray"})
        events = []
        for s in self.shipments.values():
            start = s.start or s.ready_at
            events.append({
                "id": s.shipment_id, "resourceId": s.driver_id or "unassigned",
                "name": f"{s.shipment_id} · {s.destination}",
                "startDate": iso(start), "endDate": iso(s.end_if(start)),
                "parcels": s.parcels, "kg": s.grams / 1000, "warehouse": self.warehouses[s.warehouse_id].name,
                "warehouseId": s.warehouse_id, "readyAt": iso(s.ready_at), "dueBy": iso(s.due_by),
                "fromOrder": bool(s.order_id), "assigned": s.driver_id is not None,
            })
        warehouses = []
        for w in self.warehouses.values():
            on_hand = sum(stock_of(p) for p in w.product_ids) if stock_of else 0
            outbound = [s for s in self.shipments.values() if s.warehouse_id == w.warehouse_id]
            docks = sum(1 for s in outbound if s.driver_id)
            warehouses.append({
                "id": w.warehouse_id, "name": w.name, "city": w.city,
                "onHand": on_hand, "capacity": w.capacity_units,
                "fill": round(100 * on_hand / w.capacity_units) if w.capacity_units else 0,
                "docksUsed": docks, "docks": w.dock_slots,
                "dockFill": round(100 * docks / w.dock_slots) if w.dock_slots else 0,
                "outboundParcels": sum(s.parcels for s in outbound),
                "waiting": sum(1 for s in outbound if not s.driver_id),
            })
        now = self.local_now()
        return {"day": self.day.isoformat(), "start": iso(day_open), "end": iso(day_close),
                "now": iso(now), "timezone": self.tz.key,
                "timezoneLabel": now.replace(tzinfo=self.tz).strftime("%Z"),
                "resources": resources, "events": events, "offShift": off, "warehouses": warehouses}
