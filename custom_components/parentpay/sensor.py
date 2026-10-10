"""Sensor platform: dinner balance + available items count + daily meal spend per child."""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN, MANUFACTURER
from .coordinator import ParentPayCoordinator
from .models import Balance, PaymentItem


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: ParentPayCoordinator = hass.data[DOMAIN][entry.entry_id]
    child_ids = _discover_child_ids(coordinator)
    entities: list[SensorEntity] = []
    for child_id in sorted(child_ids):
        entities.append(DinnerBalanceSensor(coordinator, entry, child_id))
        entities.append(AvailableItemsSensor(coordinator, entry, child_id))
        entities.append(SpentTodaySensor(coordinator, entry, child_id))
    async_add_entities(entities)


def _discover_child_ids(coordinator: ParentPayCoordinator) -> set[str]:
    ids: set[str] = set()
    for b in coordinator.data.get("balances", []) or []:
        ids.add(b.child_id)
    for it in coordinator.data.get("items", []) or []:
        ids.add(it.child_id)
    # Children may appear in meal history even if they currently have no
    # balance row or available items.
    for m in coordinator.data.get("meals", []) or []:
        cid = m.get("child_id")
        if cid:
            ids.add(str(cid))
    return ids


def _child_name_for(coordinator: ParentPayCoordinator, child_id: str) -> str:
    for b in coordinator.data.get("balances", []) or []:
        if b.child_id == child_id and b.child_name:
            return str(b.child_name)
    for it in coordinator.data.get("items", []) or []:
        if it.child_id == child_id and it.child_name:
            return str(it.child_name)
    return child_id


def _device_info(entry: ConfigEntry, child_id: str, child_name: str) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_{child_id}")},
        manufacturer=MANUFACTURER,
        name=child_name,
    )


class _BaseChildSensor(CoordinatorEntity[ParentPayCoordinator], SensorEntity):
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: ParentPayCoordinator,
        entry: ConfigEntry,
        child_id: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._child_id = child_id
        child_name = _child_name_for(coordinator, child_id)
        self._attr_device_info = _device_info(entry, child_id, child_name)


class DinnerBalanceSensor(_BaseChildSensor):
    _attr_translation_key = "dinner_balance"
    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "GBP"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_name = "Dinner balance"

    def __init__(
        self,
        coordinator: ParentPayCoordinator,
        entry: ConfigEntry,
        child_id: str,
    ) -> None:
        super().__init__(coordinator, entry, child_id)
        self._attr_unique_id = f"{entry.entry_id}_{child_id}_dinner_balance"

    @property
    def native_value(self) -> float | None:
        balances: list[Balance] = self.coordinator.data.get("balances") or []
        for b in balances:
            if b.child_id == self._child_id:
                return float(b.amount)
        return None


class AvailableItemsSensor(_BaseChildSensor):
    _attr_translation_key = "available_items_count"
    _attr_name = "Available items"

    def __init__(
        self,
        coordinator: ParentPayCoordinator,
        entry: ConfigEntry,
        child_id: str,
    ) -> None:
        super().__init__(coordinator, entry, child_id)
        self._attr_unique_id = f"{entry.entry_id}_{child_id}_available_items_count"

    @property
    def native_value(self) -> int:
        items: list[PaymentItem] = self.coordinator.data.get("items") or []
        return sum(1 for it in items if it.child_id == self._child_id)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        items: list[PaymentItem] = self.coordinator.data.get("items") or []
        return {
            "items": [
                {
                    "name": it.name,
                    "price": float(it.price),
                    "availability": it.availability,
                    "is_new": it.is_new,
                }
                for it in items
                if it.child_id == self._child_id
            ]
        }


class SpentTodaySensor(_BaseChildSensor):
    """Total meal spend for this child today, with a per-day history in attributes."""

    _attr_device_class = SensorDeviceClass.MONETARY
    _attr_native_unit_of_measurement = "GBP"
    _attr_name = "Spent today"
    _attr_icon = "mdi:cash-clock"

    def __init__(
        self,
        coordinator: ParentPayCoordinator,
        entry: ConfigEntry,
        child_id: str,
    ) -> None:
        super().__init__(coordinator, entry, child_id)
        self._attr_unique_id = f"{entry.entry_id}_{child_id}_spent_today"

    def _meals(self) -> list[dict[str, Any]]:
        meals = self.coordinator.data.get("meals") or []
        return [m for m in meals if str(m.get("child_id")) == self._child_id]

    @property
    def native_value(self) -> float:
        today = dt_util.now().date().isoformat()
        pence = sum(
            int(m.get("amount_pence", 0) or 0)
            for m in self._meals()
            if m.get("date") == today
        )
        return round(pence / 100, 2)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        amt_by_day: dict[str, int] = defaultdict(int)
        items_by_day: dict[str, list[str]] = defaultdict(list)
        for m in self._meals():
            d = str(m.get("date") or "")
            if not d:
                continue
            amt_by_day[d] += int(m.get("amount_pence", 0) or 0)
            if m.get("item"):
                items_by_day[d].append(str(m["item"]))

        days: list[dict[str, Any]] = []
        for d in sorted(amt_by_day.keys(), reverse=True)[:30]:
            try:
                label = date.fromisoformat(d).strftime("%a %-d %b")
            except ValueError:
                label = d
            days.append(
                {
                    "date": d,
                    "label": label,
                    "amount": round(amt_by_day[d] / 100, 2),
                    "items": items_by_day.get(d, []),
                }
            )

        last_7 = round(sum(day["amount"] for day in days[:7]), 2)
        last_30 = round(sum(day["amount"] for day in days), 2)
        return {
            "currency": "GBP",
            "last_7_days_total": last_7,
            "last_30_days_total": last_30,
            "days": days,
        }
