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
import json
from datetime import timedelta

import typer
import yaml
from sqlalchemy import select

from app.config import CONFIG_DIR, get_settings
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


@cli.command("reality-keys")
def reality_keys(
    short_id_bytes: int = typer.Option(4, help="Длина shortId в байтах (4 = 8 символов)"),
) -> None:
    """
    Сгенерировать ключи Reality (X25519) и shortId.

    Reality шифрует рукопожатие парой ключей: приватный лежит на сервере,
    публичный попадает в конфиг клиента. Xray умеет это сам (xray x25519),
    но своя реализация избавляет от необходимости держать бинарник рядом.
    """
    import base64
    import secrets

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    private = X25519PrivateKey.generate()
    raw_private = private.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    raw_public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )

    def b64(data: bytes) -> str:
        # Xray ждёт base64url без padding
        return base64.urlsafe_b64encode(data).decode().rstrip("=")

    typer.echo(
        json.dumps(
            {
                "private_key": b64(raw_private),
                "public_key": b64(raw_public),
                "short_id": secrets.token_hex(short_id_bytes),
            }
        )
    )


@cli.command("setup-marzban")
def setup_marzban(
    node_code: str = typer.Option("nl-1", help="Код ноды"),
    location: str = typer.Option("nl", help="Локация: nl или ru"),
    host: str = typer.Option(..., help="IP или домен, по которому клиенты идут на ноду"),
    port: int = typer.Option(8443, help="Порт inbound"),
    sni: str = typer.Option("www.nvidia.com", help="Маскировочный домен Reality"),
) -> None:
    """
    Прописать ноду в панели и в базе сервиса.

    Делает две вещи, которые иначе пришлось бы делать руками в интерфейсе:
    создаёт host для inbound (адрес, порт, SNI — то, что видит клиент)
    и заводит запись ноды в нашей базе, чтобы работали тарифы и Auto-Healing.
    """

    async def _run() -> None:
        from app.marzban import MarzbanClient, MarzbanError

        tag = f"VLESS_REALITY_{node_code}"

        try:
            async with MarzbanClient() as mz:
                available = await mz.list_inbounds()
                all_tags = [t for tags in available.values() for t in tags]
                if tag not in all_tags:
                    typer.echo(f"В панели нет inbound с тегом {tag}. Найдено: {all_tags}")
                    raise typer.Exit(code=1)

                hosts = await mz.get_hosts()
                hosts[tag] = [
                    {
                        "remark": f"{location.upper()} · {{USERNAME}}",
                        "address": host,
                        "port": port,
                        "sni": sni,
                        "host": sni,
                        "path": "",
                        "security": "inbound_default",
                        "alpn": "",
                        "fingerprint": "chrome",
                        "allowinsecure": False,
                        "is_disabled": False,
                    }
                ]
                await mz.update_hosts(hosts)
                typer.echo(f"✅ Host для {tag} настроен: {host}:{port}, SNI {sni}")
        except MarzbanError as err:
            typer.echo(f"Панель недоступна: {err}")
            raise typer.Exit(code=1) from err

        sni_pool, backup_ports = _load_node_defaults(location)
        async with session_scope() as session:
            node = (
                await session.execute(select(Node).where(Node.code == node_code))
            ).scalar_one_or_none()
            if node is None:
                session.add(
                    Node(
                        code=node_code,
                        location=location,
                        host=host,
                        port=port,
                        current_sni=sni,
                        sni_pool=sni_pool or [sni],
                        backup_ports=backup_ports,
                    )
                )
                typer.echo(f"✅ Нода {node_code} добавлена в базу сервиса")
            else:
                node.host, node.port, node.current_sni = host, port, sni
                node.status = NodeStatus.HEALTHY
                node.fail_count = 0
                typer.echo(f"✅ Нода {node_code} обновлена: {host}:{port}")

    asyncio.run(_run())


@cli.command("expire-user")
def expire_user(
    telegram_id: int = typer.Option(..., help="Telegram ID пользователя"),
) -> None:
    """
    Завершить подписку прямо сейчас.

    Нужна для двух вещей: проверить, как выглядит экран «подписка
    закончилась», не дожидаясь реального срока, и вручную отключить
    доступ, если это понадобится.
    """

    async def _run() -> None:
        async with session_scope() as session:
            user = await user_service.get_by_telegram_id(session, telegram_id)
            if user is None or user.subscription is None:
                typer.echo("Пользователь или подписка не найдены")
                raise typer.Exit(code=1)

            user.subscription.expires_at = utcnow() - timedelta(hours=1)
            user.subscription.status = SubscriptionStatus.EXPIRED
            user.subscription.paused_until = None
            await sub_service.expire(session, user, user.subscription)
            typer.echo(f"✅ Подписка tg={telegram_id} завершена. Напишите боту /start")

    asyncio.run(_run())


@cli.command("doctor")
def doctor(
    telegram_id: int = typer.Option(..., help="Telegram ID пользователя для проверки"),
) -> None:
    """
    Пройти весь путь выдачи ключа и показать, где он рвётся.

    Проверяет по шагам: пользователь в базе → подписка → связь с панелью →
    inbounds → аккаунт в панели → путь подписки → сами конфиги.
    Вместо чтения логов сразу видно место поломки.
    """

    async def _run() -> None:
        from app.marzban import MarzbanClient, MarzbanError

        ok = "\033[1;32m✓\033[0m"
        bad = "\033[1;31m✗\033[0m"

        async with session_scope() as session:
            user = await user_service.get_by_telegram_id(session, telegram_id)
            if user is None:
                typer.echo(f"{bad} Пользователь tg={telegram_id} не найден в базе")
                raise typer.Exit(code=1)
            typer.echo(f"{ok} Пользователь: tg={telegram_id}, аккаунт {user.marzban_username}")

            sub = user.subscription
            if sub is None:
                typer.echo(f"{bad} Подписки нет. Нажмите «Попробовать» в боте")
                raise typer.Exit(code=1)
            typer.echo(
                f"{ok} Подписка: {sub.tariff_code}, {sub.status}, " f"осталось {sub.days_left} дн."
            )

            nodes = await sub_service.nodes_for_tariff(session, sub.tariff_code)
            if not nodes:
                typer.echo(f"{bad} Под тариф {sub.tariff_code} нет ни одной ноды в базе")
                raise typer.Exit(code=1)
            typer.echo(f"{ok} Ноды под тариф: {', '.join(n.code for n in nodes)}")

            try:
                async with MarzbanClient() as mz:
                    inbounds = await mz.list_inbounds()
                    typer.echo(f"{ok} Панель отвечает, inbounds: {inbounds}")

                    account = await mz.get_user(user.marzban_username)
                    if account is None:
                        typer.echo(f"{bad} Аккаунта нет в панели — создаю…")
                        await sub_service.sync_to_marzban(session, user, sub)
                        account = await mz.get_user(user.marzban_username)
                    if account is None:
                        typer.echo(f"{bad} Создать аккаунт не удалось")
                        raise typer.Exit(code=1)
                    typer.echo(
                        f"{ok} Аккаунт в панели: статус {account.get('status')}, "
                        f"inbounds {account.get('inbounds')}"
                    )

                    path = await mz.get_subscription_path(user.marzban_username)
                    typer.echo(f"{ok} Путь подписки в панели: {path}")

                    if not path:
                        typer.echo(f"{bad} Панель не вернула ссылку подписки")
                        raise typer.Exit(code=1)

                    url = f"{get_settings().marzban_base_url.rstrip('/')}{path}"
                    resp = await mz._client.get(url, headers={"User-Agent": "Happ"})
                    body = resp.text.strip()
                    typer.echo(f"{ok} Конфиги: HTTP {resp.status_code}, длина {len(body)}")

                    if resp.status_code == 200 and body:
                        import base64 as b64

                        try:
                            decoded = b64.b64decode(body + "=" * (-len(body) % 4)).decode()
                        except Exception:
                            decoded = body
                        lines = [ln for ln in decoded.splitlines() if ln.strip()]
                        typer.echo(f"{ok} Серверов в подписке: {len(lines)}")
                        for line in lines[:3]:
                            typer.echo(f"    {line[:90]}")
                        if not lines:
                            typer.echo(
                                f"{bad} Подписка пустая: в панели нет host для inbound. "
                                f"Запустите setup-marzban"
                            )
                    else:
                        typer.echo(f"{bad} Панель вернула пустой ответ")

            except MarzbanError as err:
                typer.echo(f"{bad} Панель недоступна: {err}")
                raise typer.Exit(code=1) from err

        typer.echo("\nПроверка завершена")

    asyncio.run(_run())


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
