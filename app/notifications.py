"""Notifications consume stored signals; indicator functions never send messages."""
from __future__ import annotations

import asyncio
import logging
from typing import Protocol

import httpx

logger = logging.getLogger(__name__)


class NotificationService(Protocol):
    async def send_signal(self, signal: dict) -> bool: ...


class LogNotificationService:
    async def send_signal(self, signal):
        logger.info('New signal id=%s stage=%s score=%s', signal['id'], signal['stage'], signal['score'])
        return True


class WebhookNotificationService:
    def __init__(self, client: httpx.AsyncClient, url: str):
        self.client, self.url = client, url

    async def send_signal(self, signal):
        payload = {'event':'new_signal', **{k:signal[k] for k in (
            'id','exchange','market','pair','period','stage','score','candle_time','detected_at')}}
        for attempt in range(2):
            try:
                response = await self.client.post(self.url, json=payload, timeout=5,
                                                  headers={'Idempotency-Key':signal['id']})
                response.raise_for_status()
                return True
            except httpx.HTTPError:
                # Never log the URL, exception text or response: webhook tokens can be embedded.
                logger.warning('Signal webhook delivery failed id=%s attempt=%s', signal['id'], attempt+1)
                if attempt == 0:
                    await asyncio.sleep(1)
        return False
