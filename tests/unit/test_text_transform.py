"""Focused tests for automatic readable word compression."""

from __future__ import annotations

import pytest

from app.bot.handlers.text_transform import transform_message_text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Anyone providing Apple Pay GPT accounts with full warranty, please reply or DM me.",
            "AnyoneProvidingApplePayGPTaccountsWithFullWarranty,PleaseReplyOrDMme.",
        ),
        ("Hello world!", "HelloWorld!"),
        ("Hello, world!", "Hello,World!"),
        ("Apple Pay GPT accounts.", "ApplePayGPTaccounts."),
        ("Hello   world", "HelloWorld"),
        ("Anyone providing\nApple Pay\nGPT accounts", "AnyoneProvidingApplePayGPTaccounts"),
        ("Hello world 👋", "HelloWorld👋"),
        ("Order 123 items in 2026", "Order123ItemsIn2026"),
        ("Message @Example user", "Message@ExampleUser"),
        ("Follow #Telegram news", "Follow#TelegramNews"),
        (
            "Visit https://Example.com/SomePath?x=One now",
            "Visithttps://Example.com/SomePath?x=OneNow",
        ),
        ("Hello - world / test: (ok)", "Hello-World/Test:(Ok)"),
        ("HelloWorld", "Helloworld"),
        ("", ""),
        (" \t\n\r\u00a0\u2003\u2028", ""),
        ("হ্যালো বিশ্ব 你好 世界", "হ্যালোবিশ্ব你好世界"),
        ("apple pay GPT Accounts", "ApplePayGPTaccounts"),
        ("Hello, world! How are (you)?", "Hello,World!HowAre(You)?"),
        ("<b> Hello & goodbye", "<B>Hello&Goodbye"),
        ("Apple Pay 🇧🇩 GPT", "ApplePay🇧🇩GPT"),
        ("GPT accounts with DM me", "GPTaccountsWithDMme"),
        ("GPT - Accounts", "GPT-accounts"),
        ("HELLO WORLD again", "HELLOWORLDagain"),
    ],
)
def test_transform_message_text_applies_readable_capitalization(
    text: str,
    expected: str,
) -> None:
    assert transform_message_text(text) == expected
