"""Тесты deep links и конвертации в Telegram Stars."""

from app.payments.stars import StarsProvider
from app.utils.deeplink import all_links, happ, hiddify

SUB_URL = "https://vpn.example.com/sub/abc123"


def test_happ_deeplink_format():
    assert happ(SUB_URL) == f"happ://import/{SUB_URL}"


def test_hiddify_deeplink_is_encoded():
    link = hiddify(SUB_URL)
    assert link.startswith("hiddify://import/")
    assert "https%3A%2F%2F" in link


def test_all_links_contain_supported_apps():
    links = all_links(SUB_URL)
    assert set(links) == {"happ", "hiddify", "v2raytun", "streisand"}


def test_stars_conversion_rounds_up():
    # 249 руб при курсе 1.7 -> 147 звёзд (округление вверх)
    assert StarsProvider.rub_to_stars(249) == 147
    assert StarsProvider.rub_to_stars(1) == 1


def test_stars_never_zero():
    assert StarsProvider.rub_to_stars(0) >= 1


def test_import_page_replaces_sub_path():
    """Кнопка Telegram должна вести на https-страницу, а не на happ://."""
    from app.utils.deeplink import import_page

    assert import_page(SUB_URL) == "https://vpn.example.com/i/abc123"


def test_import_page_replaces_only_first_occurrence():
    url = "https://vpn.example.com/sub/tok_sub_1"
    from app.utils.deeplink import import_page

    assert import_page(url) == "https://vpn.example.com/i/tok_sub_1"
