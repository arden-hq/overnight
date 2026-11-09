from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from overnight.domain.contracts import JsonObject, Nonnegative, Text
from overnight.domain.models import Actor, Context
from overnight.services.common import find_row, next_id
from overnight.services.operations import NoArguments, OperationSpec, Parameters, Service
from overnight.services.state import State, distinct


class ShopItem(State):
    id: Text
    title: Text
    price_cents: Nonnegative
    available: Nonnegative | None = None


class OrderStatus(StrEnum):
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"


class ShopOrder(State):
    id: Text
    item_id: Text
    buyer_id: Text
    quantity: Annotated[int, Field(ge=1)]
    total_cents: Nonnegative
    status: Literal[OrderStatus.CONFIRMED, OrderStatus.CANCELLED]
    placed_at: int


class ShopState(State):
    catalog: list[ShopItem] = Field(default_factory=list)
    orders: list[ShopOrder] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_ids(self) -> Self:
        distinct([item.id for item in self.catalog], "item")
        distinct([row.id for row in self.orders], "order")
        item_ids = {item.id for item in self.catalog}
        for order in self.orders:
            if order.item_id not in item_ids:
                raise ValueError("Order references unknown item")
        return self


class ShopBuy(Parameters):
    item_id: Text
    quantity: Annotated[int, Field(ge=1)] = 1


class ShopCancel(Parameters):
    order_id: Text


class Shop(Service[ShopState]):
    def __init__(self) -> None:
        super().__init__(
            ShopState,
            [
                OperationSpec(NoArguments, self.list_items, read_only=True),
                OperationSpec(ShopBuy, self.buy),
                OperationSpec(NoArguments, self.list_orders, read_only=True),
                OperationSpec(ShopCancel, self.cancel_order),
            ],
        )

    def validate_actors(self, state: ShopState, actors: Mapping[str, Actor]) -> None:
        if any(row.buyer_id not in actors for row in state.orders):
            raise ValueError("Order buyer must be a known actor")

    def visible(self, state: ShopState, actor: Actor, context: Context) -> JsonObject:
        return {
            "as_of": context.now,
            "catalog": [item.view_object() for item in state.catalog],
            "orders": [row.view_object() for row in state.orders if row.buyer_id == actor.id],
        }

    def list_items(self, state: ShopState, args: NoArguments, context: Context) -> JsonObject:
        """List the simulated shop catalog and stock."""
        return {"as_of": context.now, "items": [item.view_object() for item in state.catalog]}

    def list_orders(self, state: ShopState, args: NoArguments, context: Context) -> JsonObject:
        """List orders bought by the actor."""
        return {
            "as_of": context.now,
            "orders": [
                row.view_object() for row in state.orders if row.buyer_id == context.actor.id
            ],
        }

    def buy(self, state: ShopState, args: ShopBuy, context: Context) -> JsonObject:
        """Buy an item at its current integer-cent price."""
        item = find_row(state.catalog, lambda item: item.id == args.item_id, "item")
        if item.available is not None:
            if item.available < args.quantity:
                raise ValueError("Insufficient stock")
            item.available -= args.quantity
        row = ShopOrder(
            id=next_id("order", state.orders),
            item_id=item.id,
            buyer_id=context.actor.id,
            quantity=args.quantity,
            total_cents=item.price_cents * args.quantity,
            status=OrderStatus.CONFIRMED,
            placed_at=context.now,
        )
        state.orders = [*state.orders, row]
        return {"order_id": row.id, "total_cents": row.total_cents, "status": row.status.value}

    def cancel_order(self, state: ShopState, args: ShopCancel, context: Context) -> JsonObject:
        """Cancel an order bought by the actor."""
        row = find_row(
            state.orders,
            lambda row: row.id == args.order_id and row.buyer_id == context.actor.id,
            "order",
        )
        if row.status != OrderStatus.CONFIRMED:
            raise ValueError("Order is not active")
        row.status = OrderStatus.CANCELLED
        item = find_row(state.catalog, lambda item: item.id == row.item_id, "item")
        if item.available is not None:
            item.available += row.quantity
        return {"order_id": row.id, "status": row.status.value}
