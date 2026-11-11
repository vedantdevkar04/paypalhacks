"""What the shop sells, and the owner's limits. The floor price never leaves this module's
private side: the agent only ever sees `Product.public()`."""

import json
from dataclasses import dataclass, field
from pathlib import Path

from hiclose.rules.negotiation import Policy


class CatalogError(Exception):
    pass


@dataclass(frozen=True)
class Product:
    product_id: str
    name: str
    description: str
    currency: str
    list_cents: int
    floor_cents: int
    sizes: tuple[str, ...]
    post_ids: tuple[str, ...] = ()
    max_rounds: int = 3
    image: str | None = None  # file name under hiclose/api/static/products/, served at /products/<image>
    policy: Policy = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if len(self.currency) != 3 or not self.currency.isupper():
            raise CatalogError(f"{self.product_id}: currency must be a 3-letter code like USD")
        object.__setattr__(self, "policy", Policy(self.list_cents, self.floor_cents, self.max_rounds))

    def public(self, stock: dict[str, int]) -> dict:
        return {
            "product_id": self.product_id,
            "name": self.name,
            "description": self.description,
            "currency": self.currency,
            "list_cents": self.list_cents,
            "sizes_in_stock": [size for size in self.sizes if stock.get(size, 0) > 0],
        }


class Catalog:
    def __init__(self, products: list[Product], stock: dict[str, dict[str, int]]):
        self._products: dict[str, Product] = {}
        self._by_post: dict[str, str] = {}
        for product in products:
            if product.product_id in self._products:
                raise CatalogError(f"duplicate product {product.product_id}")
            self._products[product.product_id] = product
            for post_id in product.post_ids:
                if post_id in self._by_post:
                    raise CatalogError(f"post {post_id} is linked to two products")
                self._by_post[post_id] = product.product_id
        self._stock = {pid: dict(stock.get(pid, {})) for pid in self._products}
        for pid, sizes in self._stock.items():
            if any(count < 0 for count in sizes.values()):
                raise CatalogError(f"{pid}: stock can't be negative")

    @classmethod
    def from_json(cls, path: str | Path) -> "Catalog":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        products, stock = [], {}
        for item in data["products"]:
            stock[item["product_id"]] = item.pop("stock", {})
            item["sizes"] = tuple(item["sizes"])
            item["post_ids"] = tuple(item.get("post_ids", ()))
            products.append(Product(**item))
        return cls(products, stock)

    def products(self) -> list[Product]:
        return list(self._products.values())

    def get(self, product_id: str) -> Product:
        try:
            return self._products[product_id]
        except KeyError:
            raise CatalogError(f"unknown product {product_id}") from None

    def for_post(self, post_id: str | None) -> Product | None:
        if post_id is None:
            return None
        product_id = self._by_post.get(post_id)
        return self._products[product_id] if product_id else None

    def public(self, product_id: str) -> dict:
        return self.get(product_id).public(self._stock[product_id])

    def in_stock(self, product_id: str, size: str) -> int:
        self.get(product_id)
        return self._stock[product_id].get(size, 0)

    def reserve(self, product_id: str, size: str) -> None:
        if self.in_stock(product_id, size) <= 0:
            raise CatalogError(f"{product_id} size {size} is out of stock")
        self._stock[product_id][size] -= 1

    def release(self, product_id: str, size: str) -> None:
        self.get(product_id)
        self._stock[product_id][size] = self._stock[product_id].get(size, 0) + 1
