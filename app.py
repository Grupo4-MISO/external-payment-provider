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
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
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
    simulate_outcome: Literal["success", "failed", "error", "random"] = Field(
        default="random", description="Resultado del pago a simular: success, failed, error, o random"
    )
    callback_delay_seconds: int = Field(
        default=2,
        ge=1,
        le=900,
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
    status: Literal["success", "failed", "error"] = Field(
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


class PaymentSessionCreateResponse(BaseModel):
    """Respuesta al crear una sesión de checkout."""

    session_id: str = Field(description="ID de la sesión de checkout")
    payment_id: str = Field(description="ID de pago original del sistema cliente")
    checkout_url: str = Field(description="URL a la que se redirige al usuario")
    status: Literal["created"] = Field(description="Estado inicial de la sesión")
    created_at: str = Field(description="Timestamp UTC ISO 8601 de creación")


class PaymentSessionDetailsResponse(BaseModel):
    """Resumen que la página de checkout usa para renderizar la vista."""

    session_id: str = Field(description="ID de la sesión de checkout")
    payment_id: str = Field(description="ID de pago original del sistema cliente")
    amount: str = Field(description="Monto a pagar como string")
    currency: str = Field(description="Moneda del pago")
    customer_id: str | None = Field(default=None, description="ID del cliente")
    metadata: dict[str, Any] | None = Field(default=None, description="Metadatos de negocio")
    status: Literal["created", "confirmed"] = Field(description="Estado actual de la sesión")
    created_at: str = Field(description="Timestamp UTC ISO 8601 de creación")


class PaymentSessionConfirmRequest(BaseModel):
    """Datos que ingresa el usuario en la página de checkout."""

    customer_email: str = Field(
        min_length=5,
        max_length=254,
        description="Correo electrónico ingresado por el usuario",
    )


class PaymentSessionRecord(BaseModel):
    """Registro interno de una sesión de pago."""

    session_id: str
    payment_request: PaymentRequest
    created_at: str
    status: Literal["created", "confirmed"] = "created"
    customer_email: str | None = None
    confirmed_at: str | None = None


_PAYMENT_SESSIONS: dict[str, PaymentSessionRecord] = {}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _generate_external_payment_id() -> str:
    return f"ext_{uuid.uuid4().hex[:16]}"


def _generate_payment_session_id() -> str:
    return f"ps_{uuid.uuid4().hex[:16]}"


def _create_payment_acceptance(
    request: PaymentRequest, background_tasks: BackgroundTasks
) -> PaymentAcceptedResponse:
    external_payment_id = _generate_external_payment_id()

    background_tasks.add_task(_process_payment, request, external_payment_id)

    return PaymentAcceptedResponse(
        payment_id=request.payment_id,
        external_payment_id=external_payment_id,
        status="processing",
        callback_delay_seconds=request.callback_delay_seconds,
        received_at=_utc_now_iso(),
    )


def _get_payment_session(session_id: str) -> PaymentSessionRecord:
    session = _PAYMENT_SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="payment session not found")
    return session


def _checkout_url(request: Request, session_id: str) -> str:
    return str(request.url_for("get_checkout_page", session_id=session_id))


def _resolve_outcome(mode: str) -> Literal["success", "failed", "error"]:
    if mode in {"success", "failed", "error"}:
        return mode
    return random.choices(
        population=["success", "failed", "error"],
        weights=[0.75, 0.2, 0.05],
        k=1,
    )[0]


def _reason_code_for_status(status: str) -> str | None:
    mapping = {
        "success": None,
        "failed": "INSUFFICIENT_FUNDS",
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

    # Print del payload que se envía al webhook
    print("\n=== WEBHOOK A ENVIAR ===")
    print(f"URL: {webhook_url}")
    print(f"Payload:\n{json.dumps(json.loads(payload_json), indent=2)}")
    print("=" * 30)

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


@app.post(
        "/payment-sessions",
        response_model=PaymentSessionCreateResponse,
        status_code=201,
        tags=["payments"],
)
async def create_payment_session(
        request: PaymentRequest, http_request: Request
) -> PaymentSessionCreateResponse:
        """
        Crear una sesión de checkout para redirigir al usuario a la página de pago.

        El payment_id creado por el sistema cliente se conserva en la sesión y luego se reutiliza
        cuando el usuario confirma el pago desde la página estática.
        """
        if any(
                session.payment_request.payment_id == request.payment_id
                for session in _PAYMENT_SESSIONS.values()
        ):
                raise HTTPException(status_code=409, detail="payment_id already has an active session")

        session_id = _generate_payment_session_id()
        created_at = _utc_now_iso()
        _PAYMENT_SESSIONS[session_id] = PaymentSessionRecord(
                session_id=session_id,
                payment_request=request,
                created_at=created_at,
        )

        return PaymentSessionCreateResponse(
                session_id=session_id,
                payment_id=request.payment_id,
                checkout_url=_checkout_url(http_request, session_id),
                status="created",
                created_at=created_at,
        )


@app.get(
        "/payment-sessions/{session_id}",
        response_model=PaymentSessionDetailsResponse,
        tags=["payments"],
)
async def get_payment_session(session_id: str) -> PaymentSessionDetailsResponse:
        """Obtener el resumen de una sesión de checkout."""
        session = _get_payment_session(session_id)
        payment_request = session.payment_request

        return PaymentSessionDetailsResponse(
                session_id=session.session_id,
                payment_id=payment_request.payment_id,
                amount=str(payment_request.amount),
                currency=payment_request.currency,
                customer_id=payment_request.customer_id,
                metadata=payment_request.metadata,
                status=session.status,
                created_at=session.created_at,
        )


@app.get("/checkout/{session_id}", response_class=HTMLResponse, tags=["checkout"])
async def get_checkout_page(session_id: str) -> HTMLResponse:
        """Renderizar una página de pago mínima que confirma la sesión."""
        html = f"""<!doctype html>
<html lang="es">
<head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Checkout simulado</title>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 0; min-height: 100vh; display: grid; place-items: center; background: #f4f6fb; color: #162033; }}
        .card {{ width: min(92vw, 520px); background: #fff; border-radius: 16px; padding: 24px; box-shadow: 0 14px 40px rgba(16, 24, 40, 0.12); }}
        h1 {{ margin-top: 0; }}
        .muted {{ color: #667085; }}
        .row {{ margin: 12px 0; }}
        label {{ display: block; margin-bottom: 6px; font-weight: 600; }}
        input {{ width: 100%; box-sizing: border-box; padding: 12px 14px; border: 1px solid #d0d5dd; border-radius: 10px; }}
        button {{ width: 100%; margin-top: 16px; padding: 12px 14px; border: 0; border-radius: 10px; background: #155eef; color: white; font-weight: 700; cursor: pointer; }}
        button:disabled {{ background: #9bb8ff; cursor: wait; }}
        .status {{ margin-top: 16px; font-size: 0.95rem; }}
    </style>
</head>
<body>
    <main class="card">
        <p class="muted">Checkout simulado</p>
        <h1>Confirma tu pago</h1>
        <div class="row" id="summary">Cargando resumen...</div>
        <div class="row">
            <label for="customer_email">Correo electrónico</label>
            <input id="customer_email" name="customer_email" type="email" placeholder="cliente@correo.com" />
        </div>
        <button id="pay_button" type="button">Pagar</button>
        <div class="status" id="status"></div>
    </main>
    <script>
        const sessionId = {json.dumps(session_id)};
        const summary = document.getElementById('summary');
        const statusBox = document.getElementById('status');
        const payButton = document.getElementById('pay_button');

        async function loadSession() {{
            const response = await fetch(`/payment-sessions/${{sessionId}}`);
            if (!response.ok) {{
                summary.textContent = 'No se pudo cargar la sesión.';
                payButton.disabled = true;
                return;
            }}
            const data = await response.json();
            summary.innerHTML = `
                <strong>Payment ID:</strong> ${{data.payment_id}}<br />
                <strong>Monto:</strong> ${{data.amount}} ${{data.currency}}<br />
                <strong>Customer ID:</strong> ${{data.customer_id ?? '-'}}<br />
            `;
        }}

        payButton.addEventListener('click', async () => {{
            const customerEmail = document.getElementById('customer_email').value.trim();
            if (!customerEmail) {{
                statusBox.textContent = 'Ingresa un correo electrónico.';
                return;
            }}

            payButton.disabled = true;
            statusBox.textContent = 'Procesando pago...';

            const response = await fetch(`/payment-sessions/${{sessionId}}/confirm`, {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ customer_email: customerEmail }})
            }});

            const data = await response.json();
            if (!response.ok) {{
                statusBox.textContent = data.detail ?? 'No fue posible procesar el pago.';
                payButton.disabled = false;
                return;
            }}

            statusBox.textContent = `Pago aceptado. External payment ID: ${{data.external_payment_id}}`;
        }});

        loadSession();
    </script>
</body>
</html>"""
        return HTMLResponse(content=html)


@app.post(
        "/payment-sessions/{session_id}/confirm",
        response_model=PaymentAcceptedResponse,
        status_code=202,
        tags=["checkout"],
)
async def confirm_payment_session(
        session_id: str,
        payload: PaymentSessionConfirmRequest,
        background_tasks: BackgroundTasks,
) -> PaymentAcceptedResponse:
        """Confirmar una sesión de checkout y ejecutar el pago con el payment_id original."""
        session = _get_payment_session(session_id)
        payment_request = session.payment_request.model_copy(
                update={
                        "metadata": {
                                **(session.payment_request.metadata or {}),
                                "customer_email": payload.customer_email,
                        }
                }
        )
        _PAYMENT_SESSIONS[session_id] = session.model_copy(
                update={
                        "status": "confirmed",
                        "customer_email": payload.customer_email,
                        "confirmed_at": _utc_now_iso(),
                }
        )
        return _create_payment_acceptance(payment_request, background_tasks)


@app.post("/payments", response_model=PaymentAcceptedResponse, status_code=202, tags=["payments"])
async def create_payment(
    request: PaymentRequest, background_tasks: BackgroundTasks
) -> PaymentAcceptedResponse:
    """
    Crear una nueva solicitud de pago.

    Retorna inmediatamente (202 Accepted) con un external_payment_id.
    El procesamiento ocurre en segundo plano y notifica el resultado al webhook.
    """
    return _create_payment_acceptance(request, background_tasks)
