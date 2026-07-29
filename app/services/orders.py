from __future__ import annotations

from sqlalchemy import Select, select
from sqlalchemy.orm import Session, selectinload

from app.errors import NotFoundError
from app.models import Inventory, Order
from app.schemas import InventoryRead, OrderRead, ShipmentRead
from app.utils import utcnow


class OrderService:
    @staticmethod
    def _owned_order_query(
        customer_id: str,
        order_id: str,
    ) -> Select[tuple[Order]]:
        return (
            select(Order)
            .where(Order.id == order_id, Order.customer_id == customer_id)
            .options(selectinload(Order.items), selectinload(Order.shipment))
        )

    def list_orders(self, session: Session, *, customer_id: str) -> list[OrderRead]:
        orders = session.scalars(
            select(Order)
            .where(Order.customer_id == customer_id)
            .order_by(Order.created_at.desc())
            .options(selectinload(Order.items), selectinload(Order.shipment))
        ).all()
        return [OrderRead.model_validate(order) for order in orders]

    def get_order(self, session: Session, *, customer_id: str, order_id: str) -> OrderRead:
        order = session.scalar(self._owned_order_query(customer_id, order_id))
        if order is None:
            # Deliberately do not reveal whether another customer owns this ID.
            raise NotFoundError(
                "ORDER_NOT_FOUND",
                "未找到该订单。",
                status_code=404,
            )
        return OrderRead.model_validate(order)

    def get_order_model(self, session: Session, *, customer_id: str, order_id: str) -> Order:
        order = session.scalar(self._owned_order_query(customer_id, order_id))
        if order is None:
            raise NotFoundError(
                "ORDER_NOT_FOUND",
                "未找到该订单。",
                status_code=404,
            )
        return order


    def get_shipment(
        self,
        session: Session,
        *,
        customer_id: str,
        order_id: str,
    ) -> ShipmentRead:
        order = self.get_order_model(
            session,
            customer_id=customer_id,
            order_id=order_id,
        )
        if order.shipment is None:
            raise NotFoundError(
                "SHIPMENT_NOT_FOUND",
                "该订单暂无物流记录。",
                status_code=404,
            )
        return ShipmentRead.model_validate(order.shipment)

    def get_inventory(self, session: Session, *, sku: str, size: str) -> InventoryRead:
        inventory = session.scalar(
            select(Inventory).where(Inventory.sku == sku, Inventory.size == size)
        )
        if inventory is None:
            return InventoryRead(
                sku=sku,
                size=size,
                available_qty=0,
                updated_at=utcnow(),
            )
        return InventoryRead.model_validate(inventory)
