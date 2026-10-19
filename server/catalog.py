"""Product catalog: prices, unit weights, theft risk and where each product sits."""
from __future__ import annotations

import csv
import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

PRODUCT_IMAGES = Path(__file__).resolve().parent.parent / "web" / "products"


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
    hsn: str | None = None          # HSN code printed on the GST invoice
    gst_rate: float | None = 18.0   # GST % included in the price; None = outside GST (e.g. alcohol)

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
        image = PRODUCT_IMAGES / f"{self.sku}.svg"
        return {**asdict(self), "monitored": self.monitored,
                "image": f"/static/products/{self.sku}.svg" if image.exists() else None}


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

    # ---------------------------------------------------------------- editing (ops console)
    def validate(self, product: Product, replacing: str | None = None) -> None:
        """Raises ValueError if the product is malformed or clashes with another one."""
        if not product.sku or not product.sku.replace("-", "").replace("_", "").isalnum():
            raise ValueError("SKU: letters, digits, dash or underscore")
        if not product.barcode.isdigit() or not 4 <= len(product.barcode) <= 14:
            raise ValueError("Barcode: 4 to 14 digits (EAN/UPC, or a 4-5 digit produce PLU)")
        if product.price < 0 or product.unit_weight_g <= 0:
            raise ValueError("Price can't be negative and unit weight must be positive")
        if product.theft not in ("low", "medium", "high"):
            raise ValueError("Theft risk: low, medium or high")
        if product.sold_by not in ("unit", "weight"):
            raise ValueError("Sold by: unit or weight")
        if product.gst_rate is not None and product.gst_rate not in (0, 0.25, 3, 5, 12, 18, 28, 40):
            raise ValueError("GST rate must be a GST slab (0, 0.25, 3, 5, 12, 18, 28, 40) or empty for outside GST")
        if (product.shelf_id is None) != (product.lane is None):
            raise ValueError("Set both shelf and lane for a monitored product, or neither")
        for other in self.all():
            if other.sku in (replacing, product.sku):
                continue
            if other.barcode == product.barcode:
                raise ValueError(f"Barcode {product.barcode} already belongs to {other.sku}")
            if product.monitored and (other.shelf_id, other.lane) == (product.shelf_id, product.lane):
                raise ValueError(f"Shelf {product.shelf_id} lane {product.lane} already holds {other.sku}")

    def upsert(self, product: Product, replacing: str | None = None) -> None:
        self.validate(product, replacing)
        products = {k: v for k, v in self._by_sku.items() if k != replacing}
        products[product.sku] = product
        self._set(list(products.values()))

    def remove(self, sku: str) -> None:
        if sku not in self._by_sku:
            raise KeyError(sku)
        self._set([p for p in self.all() if p.sku != sku])

    def save(self, path: Path) -> None:
        """Atomic write, so a crash never leaves a half-written catalog."""
        path = Path(path)
        raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        raw["products"] = [asdict(p) for p in self.all()]
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(raw, indent=1, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)

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
