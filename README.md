# external-payment-provider
Proyecto para simular el comportamiento de un proveedor de pagos externo para el sistema de TravelHub.

## Objetivo
Este servicio recibe una solicitud de pago, genera un `external_payment_id`, responde de inmediato al cliente y luego notifica de forma asíncrona el resultado del pago al webhook indicado.

## Stack
- Python 3.11+
- FastAPI
- Uvicorn

## Ejecutar localmente
1. Crear y activar entorno virtual.
2. Instalar dependencias:

```bash
pip install -r requirements.txt
```

3. Levantar el servicio:

```bash
uvicorn app:app --reload --port 8086
```

4. Validar salud:

```bash
curl http://localhost:8086/health
```

## Swagger / OpenAPI
FastAPI incluye documentación interactiva automática. Accede a:

- **Swagger UI**: http://localhost:8086/docs
- **ReDoc**: http://localhost:8086/redoc
- **OpenAPI JSON**: http://localhost:8086/openapi.json

En Swagger puedes probar todos los endpoints directamente desde el navegador.

## Endpoint principal
### `POST /payments`

### Entrada mínima requerida
- `payment_id`
- `amount`
- `webhook_url`

### Entrada recomendada
Además de los 3 campos iniciales, conviene incluir:
- `currency`: evita ambigüedades en el monto.
- `webhook_secret`: permite firmar el webhook y validar autenticidad.
- `customer_id`: útil para trazabilidad.
- `metadata`: datos adicionales del negocio (ej. `booking_id`, `channel`).
- `simulate_outcome`: controlar resultado en pruebas (`approved`, `rejected`, `error`, `random`).
- `callback_delay_seconds`: simular latencia de procesamiento.

### Ejemplo request

```json
{
	"payment_id": "pay_1001",
	"amount": 120000.50,
	"webhook_url": "https://tu-sistema.com/webhooks/payments",
	"currency": "COP",
	"webhook_secret": "mi-secreto-webhook",
	"customer_id": "cust_42",
	"metadata": {
		"booking_id": "bk_9090",
		"channel": "web"
	},
	"simulate_outcome": "random",
	"callback_delay_seconds": 2
}
```

### Respuesta inmediata (202 Accepted)

```json
{
	"payment_id": "pay_1001",
	"external_payment_id": "ext_4fe9a3b941a64216",
	"status": "processing",
	"callback_delay_seconds": 2,
	"received_at": "2026-04-21T16:00:00.000000+00:00"
}
```

### Campos recomendados en la respuesta
Además de `external_payment_id`, sugiero mantener:
- `status`: estado inicial (`processing`).
- `callback_delay_seconds`: ayuda a pruebas de integración.
- `received_at`: trazabilidad temporal.

## Notificación al webhook
Luego del procesamiento interno, el servicio envía `POST` al `webhook_url` con el resultado final.

### Body enviado al webhook

```json
{
	"event": "payment_result",
	"payment_id": "pay_1001",
	"external_payment_id": "ext_4fe9a3b941a64216",
	"status": "approved",
	"amount": "120000.50",
	"currency": "COP",
	"processed_at": "2026-04-21T16:00:03.000000+00:00",
	"reason_code": null,
	"metadata": {
		"booking_id": "bk_9090",
		"channel": "web"
	}
}
```

### Headers enviados al webhook
- `Content-Type: application/json`
- `X-Webhook-Event: payment_result`
- `X-External-Payment-Id: <external_payment_id>`
- `X-Webhook-Timestamp: <epoch-seconds>` (si se envió `webhook_secret`)
- `X-Webhook-Signature: <hmac_sha256(timestamp.body)>` (si se envió `webhook_secret`)

### Campos recomendados en webhook
Sí, conviene agregar al menos:
- `event`: para distinguir tipos de notificación.
- `status`: estado final (`approved`, `rejected`, `error`).
- `reason_code`: motivo estandarizado para rechazos/errores.
- `processed_at`: auditoría y orden de eventos.
- `external_payment_id` y `payment_id`: correlación entre sistemas.

## Comportamiento de simulación
- Genera `external_payment_id` aleatorio con prefijo `ext_`.
- Simula resultado con distribución por defecto:
	- `approved`: 75%
	- `rejected`: 20%
	- `error`: 5%
- Reintenta envío de webhook hasta 3 veces en caso de error temporal.
