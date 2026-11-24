"""An empty turn never reaches the provider (a 400: "text blocks cannot have empty text").

It happens when an attachment's upload fails and the chat sends the message anyway, or when
the message holds only attachments the API path can't read.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app import chainlit_app


class _Sent(list):
    def message(self, content: str = "", **_kw):
        sent = self

        class _Msg:
            async def send(self_inner):
                sent.append(content)

        return _Msg()


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> _Sent:
    out = _Sent()
    monkeypatch.setattr(chainlit_app.cl, "Message", out.message)
    session: dict = {"messages": []}
    monkeypatch.setattr(chainlit_app.cl, "user_session",
                        SimpleNamespace(get=lambda k, d=None: session.get(k, d), set=session.__setitem__))
    monkeypatch.setattr(chainlit_app, "load_user_settings", lambda: {"provider": "anthropic"})
    out.session = session  # type: ignore[attr-defined]
    return out


def _turn(content: str, elements=None):
    msg = SimpleNamespace(content=content, elements=elements or [])
    asyncio.run(chainlit_app._run_message_turn(msg, None))


@pytest.mark.parametrize("content", ["", "   \n"])
def test_empty_message_is_not_sent(sent, content):
    _turn(content)
    assert sent == [chainlit_app._EMPTY_MESSAGE_NOTICE]
    assert sent.session["messages"] == []


def test_unreadable_attachment_only_is_not_sent(sent):
    _turn("", [SimpleNamespace(mime="application/pdf", name="rules.pdf")])
    assert len(sent) == 1 and "rules.pdf" in sent[0] and "Nothing to send" in sent[0]
    assert sent.session["messages"] == []
