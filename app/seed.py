from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from app.config import Settings
from app.database import Database
from app.enums import OrderStatus
from app.models import Customer, Inventory, Order, OrderItem, Shipment
from app.services.auth import hash_verification_code
from app.utils import utcnow


def seed_demo_data(database: Database, settings: Settings) -> None:
    """Insert synthetic demo data once.

    All identities, orders, tracking numbers, and policies are fictional.
    """

    with database.session() as session:
        if session.scalar(select(Customer.id).limit(1)) is not None:
            return

        now = utcnow()
        code_hash = hash_verification_code(settings.demo_verification_code)
        customer_1 = Customer(
            id="CUST-001",
            name="林帆",
            email="linfan@example.com",
            verification_code_hash=code_hash,
        )
        customer_2 = Customer(
            id="CUST-002",
            name="陈澄",
            email="chencheng@example.com",
            verification_code_hash=code_hash,
        )
        session.add_all([customer_1, customer_2])

        orders = [
            Order(
                id="ORD-1001",
                customer_id=customer_1.id,
                status=OrderStatus.PAID.value,
                created_at=now - timedelta(hours=2),
                version=1,
            ),
            Order(
                id="ORD-1002",
                customer_id=customer_1.id,
                status=OrderStatus.SHIPPED.value,
                created_at=now - timedelta(days=2),
                shipped_at=now - timedelta(hours=20),
                version=2,
            ),
            Order(
                id="ORD-1003",
                customer_id=customer_1.id,
                status=OrderStatus.DELIVERED.value,
                created_at=now - timedelta(days=6),
                shipped_at=now - timedelta(days=5),
                delivered_at=now - timedelta(days=3),
                version=3,
            ),
            Order(
                id="ORD-1004",
                customer_id=customer_1.id,
                status=OrderStatus.DELIVERED.value,
                created_at=now - timedelta(days=15),
                shipped_at=now - timedelta(days=13),
                delivered_at=now - timedelta(days=10),
                version=3,
            ),
            Order(
                id="ORD-1005",
                customer_id=customer_1.id,
                status=OrderStatus.DELIVERED.value,
                created_at=now - timedelta(days=4),
                shipped_at=now - timedelta(days=3),
                delivered_at=now - timedelta(days=2),
                version=3,
            ),
            Order(
                id="ORD-2001",
                customer_id=customer_2.id,
                status=OrderStatus.PAID.value,
                created_at=now - timedelta(hours=1),
                version=1,
            ),
        ]
        session.add_all(orders)

        items = [
            OrderItem(
                id="ITEM-1001-A",
                order_id="ORD-1001",
                sku="GAT-WHITE",
                product_name="RIVET GAT 德训鞋 白色",
                size="42",
                quantity=1,
                unit_price_cents=89900,
                is_final_sale=False,
            ),
            OrderItem(
                id="ITEM-1002-A",
                order_id="ORD-1002",
                sku="HOODIE-GRAY",
                product_name="RIVET 重磅连帽衫 灰色",
                size="M",
                quantity=1,
                unit_price_cents=59900,
                is_final_sale=False,
            ),
            OrderItem(
                id="ITEM-1003-A",
                order_id="ORD-1003",
                sku="GAT-WHITE",
                product_name="RIVET GAT 德训鞋 白色",
                size="42",
                quantity=1,
                unit_price_cents=89900,
                is_final_sale=False,
            ),
            OrderItem(
                id="ITEM-1004-A",
                order_id="ORD-1004",
                sku="TEE-BLACK",
                product_name="RIVET 基础短袖 黑色",
                size="L",
                quantity=1,
                unit_price_cents=29900,
                is_final_sale=False,
            ),
            OrderItem(
                id="ITEM-1005-A",
                order_id="ORD-1005",
                sku="ARCHIVE-JACKET",
                product_name="RIVET Archive 限定夹克",
                size="M",
                quantity=1,
                unit_price_cents=129900,
                is_final_sale=True,
            ),
            OrderItem(
                id="ITEM-2001-A",
                order_id="ORD-2001",
                sku="GAT-BLACK",
                product_name="RIVET GAT 德训鞋 黑色",
                size="41",
                quantity=1,
                unit_price_cents=89900,
                is_final_sale=False,
            ),
        ]
        session.add_all(items)

        shipments = [
            Shipment(
                id="SHP-1002",
                order_id="ORD-1002",
                carrier="顺达快递（虚构）",
                tracking_number="DEMO10020001",
                status="IN_TRANSIT",
                estimated_delivery_at=now + timedelta(days=1),
                updated_at=now - timedelta(hours=1),
            ),
            Shipment(
                id="SHP-1003",
                order_id="ORD-1003",
                carrier="顺达快递（虚构）",
                tracking_number="DEMO10030001",
                status="DELIVERED",
                estimated_delivery_at=now - timedelta(days=3),
                updated_at=now - timedelta(days=3),
            ),
        ]
        session.add_all(shipments)

        inventory = [
            Inventory(sku="GAT-WHITE", size="41", available_qty=4, updated_at=now),
            Inventory(sku="GAT-WHITE", size="42", available_qty=3, updated_at=now),
            Inventory(sku="GAT-WHITE", size="43", available_qty=2, updated_at=now),
            Inventory(sku="GAT-WHITE", size="44", available_qty=0, updated_at=now),
            Inventory(sku="HOODIE-GRAY", size="L", available_qty=5, updated_at=now),
        ]
        session.add_all(inventory)
