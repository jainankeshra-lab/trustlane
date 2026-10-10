"""Product catalog: prices, unit weights, theft risk and where each product sits."""
from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path


@dataclass(frozen=True)
class Product:
    sku: str
    barcode: str
    name: str
    price: int  # paise; per kg when sold_by == "weight"
    unit_weight_g: float
    theft: str  # low | medium | high
    aisle: str
    shelf_id: str | None
    lane: int | None
    sold_by: str = "unit"  # unit | weight (loose produce: the shelf weight sets the quantity)
    age_restricted: bool = False

    @property
    def monitored(self) -> bool:
        return self.shelf_id is not None

    @property
    def by_weight(self) -> bool:
        return self.sold_by == "weight"

    def price_for(self, grams: float | None) -> int:
        if not self.by_weight:
            return self.price
        return round(self.price * (grams or 0) / 1000)

    def public(self) -> dict:
        return {**asdict(self), "monitored": self.monitored}


class Catalog:
    def __init__(self, products: list[Product]):
        self._set(products)

    def _set(self, products: list[Product]) -> None:
        self._by_sku = {p.sku: p for p in products}
        self._by_barcode = {p.barcode: p for p in products}
        self._by_lane = {(p.shelf_id, p.lane): p for p in products if p.monitored}

    @classmethod
    def load(cls, path: Path) -> "Catalog":
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls([Product(**p) for p in raw["products"]])

    def get(self, sku: str) -> Product | None:
        return self._by_sku.get(sku)

    def by_barcode(self, barcode: str) -> Product | None:
        return self._by_barcode.get(barcode)

    def by_lane(self, shelf_id: str, lane: int) -> Product | None:
        return self._by_lane.get((shelf_id, lane))

    def all(self) -> list[Product]:
        return list(self._by_sku.values())

    def apply_prices(self, rows: list[dict]) -> list[str]:
        """ERP price update: rows with sku and price (rupees). Returns the SKUs changed."""
        changed, products = [], dict(self._by_sku)
        for row in rows:
            sku, price = row.get("sku", "").strip(), row.get("price_rupees", row.get("price", "")).strip()
            if sku in products and price:
                paise = round(float(price) * 100)
                if paise != products[sku].price:
                    products[sku] = replace(products[sku], price=paise)
                    changed.append(sku)
        self._set(list(products.values()))
        return changed


def read_price_csv(text: str) -> list[dict]:
    return list(csv.DictReader(text.splitlines()))
