"""
Сервисные команды (запускаются внутри контейнера api).

    python -m app.cli add-node --code nl-1 --location nl --host 1.2.3.4
    python -m app.cli list-nodes
    python -m app.cli set-node-host --code nl-1 --host 5.6.7.8
    python -m app.cli grant-days --telegram-id 123456789 --days 30
    python -m app.cli check-nodes
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import typer
import yaml
from sqlalchemy import select

from app.config import CONFIG_DIR
from app.db import session_scope
from app.models import Node, NodeStatus, Subscription, SubscriptionStatus, utcnow
from app.services import healing
from app.services import subscriptions as sub_service
from app.services import users as user_service

cli = typer.Typer(help="Сервисные команды VPN-сервиса", no_args_is_help=True)


def _load_node_defaults(location: str) -> tuple[list[str], list[int]]:
    """Пулы SNI и портов из config/nodes.example.yml."""
    path = CONFIG_DIR / "nodes.example.yml"
    if not path.exists():
        return [], [443, 8443, 2053, 2083]
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    sni = (data.get("sni_pool") or {}).get(location, [])
    ports = data.get("backup_ports", [443, 8443, 2053, 2083])
    return sni, ports


@cli.command("add-node")
def add_node(
    code: str = typer.Option(..., help="Уникальный код ноды, например nl-1"),
    location: str = typer.Option(..., help="Локация: nl или ru"),
    host: str = typer.Option(..., help="IP или домен ноды"),
    port: int = typer.Option(443, help="Боевой порт"),
    marzban_node_id: int = typer.Option(None, help="ID ноды в панели Marzban"),
) -> None:
    """Добавить ноду в сервис."""

    async def _run() -> None:
        sni_pool, backup_ports = _load_node_defaults(location)
        async with session_scope() as session:
            exists = (
                await session.execute(select(Node).where(Node.code == code))
            ).scalar_one_or_none()
            if exists:
                typer.echo(f"Нода {code} уже существует")
                raise typer.Exit(code=1)

            session.add(
                Node(
                    code=code,
                    location=location,
                    host=host,
                    port=port,
                    marzban_node_id=marzban_node_id,
                    current_sni=sni_pool[0] if sni_pool else None,
                    sni_pool=sni_pool,
                    backup_ports=backup_ports,
                )
            )
        typer.echo(f"✅ Нода {code} ({location}) {host}:{port} добавлена")

    asyncio.run(_run())


@cli.command("list-nodes")
def list_nodes() -> None:
    """Показать все ноды и их состояние."""

    async def _run() -> None:
        async with session_scope() as session:
            nodes = (await session.execute(select(Node).order_by(Node.code))).scalars()
            for node in nodes:
                typer.echo(
                    f"{node.code:8} {node.location:3} {node.host}:{node.port:<5} "
                    f"{node.status:9} сбоев={node.fail_count} sni={node.current_sni}"
                )

    asyncio.run(_run())


@cli.command("set-node-host")
def set_node_host(
    code: str = typer.Option(..., help="Код ноды"),
    host: str = typer.Option(..., help="Новый IP или домен"),
) -> None:
    """Сменить адрес ноды (например, после заказа нового IP)."""

    async def _run() -> None:
        async with session_scope() as session:
            node = (
                await session.execute(select(Node).where(Node.code == code))
            ).scalar_one_or_none()
            if node is None:
                typer.echo(f"Нода {code} не найдена")
                raise typer.Exit(code=1)
            node.host = host
            node.status = NodeStatus.HEALTHY
            node.fail_count = 0
        typer.echo(f"✅ Нода {code} переехала на {host}")

    asyncio.run(_run())


@cli.command("check-nodes")
def check_nodes() -> None:
    """Разово проверить доступность всех нод."""

    async def _run() -> None:
        async with session_scope() as session:
            nodes = list((await session.execute(select(Node))).scalars())
            for node in nodes:
                healthy = await healing.check_node(session, node)
                mark = "🟢" if healthy else "🔴"
                typer.echo(
                    f"{mark} {node.code} {node.host}:{node.port} ({node.last_latency_ms} мс)"
                )

    asyncio.run(_run())


@cli.command("grant-days")
def grant_days(
    telegram_id: int = typer.Option(..., help="Telegram ID пользователя"),
    days: int = typer.Option(..., help="Сколько дней начислить"),
    tariff: str = typer.Option("multi", help="Тариф, если подписки ещё нет"),
) -> None:
    """Выдать дни вручную (компенсация, тест, подарок)."""

    async def _run() -> None:
        async with session_scope() as session:
            user = await user_service.get_by_telegram_id(session, telegram_id)
            if user is None:
                typer.echo("Пользователь не найден")
                raise typer.Exit(code=1)

            subscription = user.subscription
            if subscription is None:
                subscription = Subscription(
                    user_id=user.id,
                    tariff_code=tariff,
                    status=SubscriptionStatus.ACTIVE,
                    expires_at=utcnow() + timedelta(days=days),
                    is_trial=False,
                )
                session.add(subscription)
                await session.flush()
            else:
                base = subscription.expires_at if subscription.expires_at > utcnow() else utcnow()
                subscription.expires_at = base + timedelta(days=days)
                subscription.status = SubscriptionStatus.ACTIVE

            await sub_service.sync_to_marzban(session, user, subscription)
            typer.echo(f"✅ Выдано {days} дн. до {subscription.expires_at:%d.%m.%Y}")

    asyncio.run(_run())


if __name__ == "__main__":
    cli()
