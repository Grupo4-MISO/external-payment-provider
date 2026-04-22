from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import random
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Literal

import httpx
from fastapi import BackgroundTasks, FastAPI
from pydantic import BaseModel, Field, HttpUrl, field_validator

app = FastAPI(
    title="External Payment Provider Simulator",
    version="0.1.0",
    description=(
        "Servicio pequeño para simular un proveedor externo de pagos. "
        "Recibe una solicitud de pago y notifica el resultado al webhook del cliente."
    ),
)


class PaymentRequest(BaseModel):
    """Solicitud de pago para procesar en el proveedor externo."""

    payment_id: str = Field(
        min_length=1, max_length=100, description="ID único de la solicitud de pago en tu sistema"
    )
    amount: Decimal = Field(gt=Decimal("0"), description="Monto del pago (debe ser mayor a 0)")
    webhook_url: HttpUrl = Field(description="URL donde notificar el resultado del pago")

    # Campos opcionales que suelen ser necesarios en integraciones reales.
    currency: str = Field(
        default="COP",
        min_length=3,
        max_length=3,
        description="Código ISO de 3 caracteres (ej. COP, USD, EUR)",
    )
    webhook_secret: str | None = Field(
        default=None,
        min_length=8,
        max_length=200,
        description="Secreto para firmar el webhook (HMAC-SHA256). Mínimo 8 caracteres",
    )
    customer_id: str | None = Field(
        default=None, max_length=100, description="ID del cliente para trazabilidad"
    )
    metadata: dict[str, Any] | None = Field(
        default=None, description="Datos adicionales del negocio (booking_id, channel, etc.)"
    )

    # Control de simulacion para pruebas.
    simulate_outcome: Literal["approved", "rejected", "error", "random"] = Field(
        default="random", description="Resultado del pago a simular: approved, rejected, error, o random"
    )
    callback_delay_seconds: int = Field(
        default=2,
        ge=1,
        le=30,
        description="Segundos de espera antes de notificar al webhook (simula latencia)",
    )

    @field_validator("currency")
    @classmethod
    def normalize_currency(cls, value: str) -> str:
        return value.upper()


class PaymentAcceptedResponse(BaseModel):
    """Respuesta inmediata al recibir la solicitud de pago (HTTP 202)."""

    payment_id: str = Field(description="ID de pago enviado en la solicitud")
    external_payment_id: str = Field(
        description="ID generado por el proveedor externo para correlacionar"
    )
    status: Literal["processing"] = Field(description="Estado: siempre 'processing'")
    callback_delay_seconds: int = Field(
        description="Segundos que tardará en notificar al webhook"
    )
    received_at: str = Field(description="Timestamp UTC ISO 8601 de recepción")


class PaymentWebhookPayload(BaseModel):
    """Payload enviado al webhook del cliente con el resultado del pago."""

    event: Literal["payment_result"] = Field(description="Tipo de evento")
    payment_id: str = Field(description="ID de pago del cliente")
    external_payment_id: str = Field(description="ID generado por el proveedor externo")
    status: Literal["approved", "rejected", "error"] = Field(
        description="Estado final del pago"
    )
    amount: str = Field(description="Monto procesado (como string para precisión)")
    currency: str = Field(description="Código ISO de moneda")
    processed_at: str = Field(description="Timestamp UTC ISO 8601 de procesamiento")
    reason_code: str | None = Field(
        default=None,
        description="Código de rechazo/error si aplica (ej. INSUFFICIENT_FUNDS, PROCESSING_ERROR)",
    )
    metadata: dict[str, Any] | None = Field(
        default=None, description="Metadatos incluidos en la solicitud"
    )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _generate_external_payment_id() -> str:
    return f"ext_{uuid.uuid4().hex[:16]}"


def _resolve_outcome(mode: str) -> Literal["approved", "rejected", "error"]:
    if mode in {"approved", "rejected", "error"}:
        return mode
    return random.choices(
        population=["approved", "rejected", "error"],
        weights=[0.75, 0.2, 0.05],
        k=1,
    )[0]


def _reason_code_for_status(status: str) -> str | None:
    mapping = {
        "approved": None,
        "rejected": "INSUFFICIENT_FUNDS",
        "error": "PROCESSING_ERROR",
    }
    return mapping.get(status)


def _build_signature(secret: str, body: str, timestamp: str) -> str:
    message = f"{timestamp}.{body}".encode("utf-8")
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return digest


async def _send_webhook_with_retries(
    webhook_url: str,
    payload: PaymentWebhookPayload,
    webhook_secret: str | None,
    max_attempts: int = 3,
) -> None:
    payload_json = payload.model_dump_json()
    headers = {
        "Content-Type": "application/json",
        "X-Webhook-Event": payload.event,
        "X-External-Payment-Id": payload.external_payment_id,
    }

    if webhook_secret:
        timestamp = str(int(datetime.now(timezone.utc).timestamp()))
        signature = _build_signature(webhook_secret, payload_json, timestamp)
        headers["X-Webhook-Timestamp"] = timestamp
        headers["X-Webhook-Signature"] = signature

    timeout = httpx.Timeout(10.0)

    async with httpx.AsyncClient(timeout=timeout) as client:
        for attempt in range(1, max_attempts + 1):
            try:
                response = await client.post(webhook_url, content=payload_json, headers=headers)
                response.raise_for_status()
                return
            except Exception:
                if attempt >= max_attempts:
                    return
                await asyncio.sleep(attempt)


async def _process_payment(request: PaymentRequest, external_payment_id: str) -> None:
    await asyncio.sleep(request.callback_delay_seconds)

    status = _resolve_outcome(request.simulate_outcome)
    payload = PaymentWebhookPayload(
        event="payment_result",
        payment_id=request.payment_id,
        external_payment_id=external_payment_id,
        status=status,
        amount=str(request.amount),
        currency=request.currency,
        processed_at=_utc_now_iso(),
        reason_code=_reason_code_for_status(status),
        metadata=request.metadata,
    )

    await _send_webhook_with_retries(
        webhook_url=str(request.webhook_url),
        payload=payload,
        webhook_secret=request.webhook_secret,
    )


@app.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    """Verificar que el servicio está operativo."""
    return {"status": "ok"}


@app.post("/payments", response_model=PaymentAcceptedResponse, status_code=202, tags=["payments"])
async def create_payment(
    request: PaymentRequest, background_tasks: BackgroundTasks
) -> PaymentAcceptedResponse:
    """
    Crear una nueva solicitud de pago.

    Retorna inmediatamente (202 Accepted) con un external_payment_id.
    El procesamiento ocurre en segundo plano y notifica el resultado al webhook.
    """
    external_payment_id = _generate_external_payment_id()

    background_tasks.add_task(_process_payment, request, external_payment_id)

    return PaymentAcceptedResponse(
        payment_id=request.payment_id,
        external_payment_id=external_payment_id,
        status="processing",
        callback_delay_seconds=request.callback_delay_seconds,
        received_at=_utc_now_iso(),
    )
