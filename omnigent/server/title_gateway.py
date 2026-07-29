"""Server-side session-title generation via an OpenAI-compatible gateway.

FORK-OWNED. Upstream has no file here, so it can never conflict on a merge.

Upstream generates titles by asking the session's RUNNER, which spawns an
isolated tool-free harness process using host-local credentials
(``BackgroundTitleContext.spawn_env``). That works, but it ties titling to the
runner in three ways this module removes:

- the runner build must carry ``/v1/sessions/{id}/background-title`` (an older
  host daemon answers 404, so no title is ever produced);
- a runner must be LIVE, so a finished session can never be re-titled; and
- the harness must have a registered title generator, excluding the rest.

Pointing the server at a gateway instead makes titling depend only on the
server, which already holds the conversation. The generator satisfies the same
``BackgroundTitleGenerator`` protocol, so it drops into the existing
coordinator with no other changes.

Configuration (all optional; absent API key disables it and the server falls
back to upstream's runner generator):

- ``OMNIGENT_TITLE_GATEWAY_API_KEY`` — gateway credential.
- ``OMNIGENT_TITLE_GATEWAY_MODEL`` — model id, default
  :data:`DEFAULT_TITLE_MODEL`.
- ``OMNIGENT_TITLE_GATEWAY_BASE_URL`` — OpenAI-compatible base URL, default
  :data:`DEFAULT_GATEWAY_BASE_URL` (Vercel AI Gateway).
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from omnigent.runner.background_titles.service import (
    BACKGROUND_TITLE_INSTRUCTIONS,
    BACKGROUND_TITLE_MAX_OUTPUT_TOKENS,
    BACKGROUND_TITLE_MAX_PROMPT_CHARS,
)
from omnigent.server.background_session_titles import BackgroundTitleRequest

_logger = logging.getLogger(__name__)

DEFAULT_GATEWAY_BASE_URL = "https://ai-gateway.vercel.sh/v1"

# Deliberately a NON-REASONING model. A title is capped at
# BACKGROUND_TITLE_MAX_OUTPUT_TOKENS (32), and a reasoning model spends that
# whole budget thinking and returns empty content — measured against this
# gateway, ``openai/gpt-5-mini`` returned 0 completion tokens at 32 and needed
# ~73 to answer at all, while this model answered in 7. Override with
# OMNIGENT_TITLE_GATEWAY_MODEL only after checking the model returns content
# within the cap.
DEFAULT_TITLE_MODEL = "anthropic/claude-haiku-4.5"

# Titles are a few hundred input tokens and ~10 output; a slow gateway must not
# hold a generation slot for the full runner-path timeout.
DEFAULT_TIMEOUT_SECONDS = 30.0


class GatewayBackgroundTitleGenerator:
    """Generate session titles from a hosted model instead of the runner.

    Satisfies the ``BackgroundTitleGenerator`` protocol
    (``async (BackgroundTitleRequest) -> str | None``), so it is a drop-in for
    :class:`~omnigent.server.background_session_titles.RunnerBackgroundTitleGenerator`.

    Every failure path returns ``None`` rather than raising: a title is
    best-effort metadata and must never surface as a turn error. The caller
    (the coordinator) treats ``None`` as "leave the existing title alone".

    :param api_key: Gateway credential sent as a bearer token.
    :param model: Model id, e.g. ``"anthropic/claude-haiku-4.5"``.
    :param base_url: OpenAI-compatible base URL, without a trailing slash.
    :param timeout_seconds: Per-request timeout.
    """

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_TITLE_MODEL,
        base_url: str = DEFAULT_GATEWAY_BASE_URL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not api_key:
            raise ValueError("api_key must be non-empty")
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    @property
    def model(self) -> str:
        """The configured model id (for startup logging)."""
        return self._model

    async def __call__(self, request: BackgroundTitleRequest) -> str | None:
        """Ask the gateway to name a session.

        :param request: The prepared title request; only ``prompt`` is used —
            the harness/model fields describe the SESSION's runtime, which is
            irrelevant to a server-side call.
        :returns: The raw model output, or ``None`` when unusable. The caller
            still normalizes and length-checks it.
        """
        prompt = (request.prompt or "").strip()
        if not prompt:
            return None

        payload: dict[str, Any] = {
            "model": self._model,
            "max_tokens": BACKGROUND_TITLE_MAX_OUTPUT_TOKENS,
            "messages": [
                # Same instruction the runner path uses, so titles read
                # consistently whichever generator produced them. It also tells
                # the model to treat the wrapped text as data, which is what
                # keeps session content from acting as instructions.
                {"role": "system", "content": BACKGROUND_TITLE_INSTRUCTIONS},
                {
                    "role": "user",
                    "content": (
                        f"<user_message>\n{prompt[:BACKGROUND_TITLE_MAX_PROMPT_CHARS]}\n"
                        "</user_message>"
                    ),
                },
            ],
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout_seconds) as client:
                response = await client.post(
                    f"{self._base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json=payload,
                )
                response.raise_for_status()
                body = response.json()
        except Exception:  # noqa: BLE001 - titles must never fail a turn
            _logger.warning(
                "gateway session title failed session=%s model=%s",
                request.session_id,
                self._model,
                exc_info=True,
            )
            return None

        content = _first_choice_content(body)
        if not content:
            # Most likely cause: a reasoning model consumed the whole output
            # budget before emitting text. Say so, since the fix is a config
            # change rather than a code bug.
            _logger.info(
                "gateway session title empty session=%s model=%s "
                "(a reasoning model may exhaust max_tokens=%d before answering)",
                request.session_id,
                self._model,
                BACKGROUND_TITLE_MAX_OUTPUT_TOKENS,
            )
            return None
        return content


def _first_choice_content(body: Any) -> str | None:
    """Pull the assistant text out of an OpenAI-compatible completion body.

    Defensive because the gateway fronts many providers: any shape surprise
    yields ``None`` (no title) instead of an exception.

    :param body: Parsed JSON response.
    :returns: The message content, or ``None`` when absent/blank.
    """
    if not isinstance(body, dict):
        return None
    choices = body.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        return None
    return content


def gateway_title_generator_from_env() -> GatewayBackgroundTitleGenerator | None:
    """Build the gateway generator from the environment, if configured.

    :returns: A generator when ``OMNIGENT_TITLE_GATEWAY_API_KEY`` is set,
        otherwise ``None`` so the caller falls back to the runner generator.
    """
    api_key = (os.environ.get("OMNIGENT_TITLE_GATEWAY_API_KEY") or "").strip()
    if not api_key:
        return None
    model = (os.environ.get("OMNIGENT_TITLE_GATEWAY_MODEL") or "").strip() or DEFAULT_TITLE_MODEL
    base_url = (
        os.environ.get("OMNIGENT_TITLE_GATEWAY_BASE_URL") or ""
    ).strip() or DEFAULT_GATEWAY_BASE_URL
    return GatewayBackgroundTitleGenerator(api_key, model=model, base_url=base_url)
