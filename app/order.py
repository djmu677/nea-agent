"""Ficha canónica de pedidos y evidencia comercial derivada.

Los nombres son genéricos para que la misma Nea sirva a cualquier tenant. La
ficha vive en Parley; este módulo solo normaliza lo que el modelo entrega y
convierte hechos ya guardados en evidencia verificable para el pipeline.
"""
from __future__ import annotations

from typing import Any


ORDER_FIELD_SCHEMAS: dict[str, dict[str, Any]] = {
    "product": {
        "type": "string",
        "description": "Producto o modelo exacto elegido por el cliente.",
    },
    "product_variant": {
        "type": "string",
        "description": "Variante elegida, por ejemplo tamaño o versión.",
    },
    "product_configuration": {
        "type": "string",
        "description": "Configuración confirmada, por ejemplo con o sin brazos.",
    },
    "material": {
        "type": "string",
        "description": "Material o tela confirmada.",
    },
    "color": {"type": "string", "description": "Color confirmado."},
    "legs": {
        "type": "string",
        "description": "Tipo de patas o soporte confirmado.",
    },
    "quantity_confirmed": {
        "type": "string",
        "description": "Cantidad confirmada por el cliente.",
    },
    "delivery_commune": {
        "type": "string",
        "description": "Comuna, ciudad o zona de entrega confirmada.",
    },
    "delivery_address": {
        "type": "string",
        "description": "Dirección completa de entrega confirmada.",
    },
    "recipient_confirmed": {
        "type": "string",
        "description": "Nombre de quien recibe el pedido.",
    },
    "email": {
        "type": "string",
        "description": "Correo entregado voluntariamente por el cliente.",
    },
    "delivery_date_requested": {
        "type": "string",
        "description": "Fecha o plazo de entrega solicitado; no implica reserva.",
    },
    "payment_method": {
        "type": "string",
        "description": "Medio de pago elegido, solo si el cliente lo confirmó.",
    },
    "order_extras": {
        "type": "array",
        "description": (
            "Adicionales confirmados. Guarda nombre y cantidad; no inventes "
            "precios ni totales."
        ),
        "maxItems": 12,
        "items": {
            "type": "object",
            "properties": {
                "label": {"type": "string"},
                "quantity": {"type": "integer", "minimum": 1},
            },
            "required": ["label", "quantity"],
            "additionalProperties": False,
        },
    },
    "order_confirmation": {
        "type": "boolean",
        "description": "Verdadero solo si el cliente confirmó explícitamente comprar.",
    },
    "configuration_complete": {
        "type": "boolean",
        "description": (
            "Verdadero solo cuando todas las opciones necesarias del producto "
            "quedaron definidas."
        ),
    },
}

ORDER_FIELDS = frozenset(ORDER_FIELD_SCHEMAS)


def nonempty(value: Any) -> bool:
    return value not in (None, "", [], {})


def confirmed(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().casefold() in {"sí", "si", "true", "1", "confirmado"}
    return False


def normalize_order_patch(values: dict[str, Any]) -> dict[str, Any]:
    """Conserva solo campos canónicos y elimina espacios accidentales."""
    patch: dict[str, Any] = {}
    for key in ORDER_FIELD_SCHEMAS:
        value = values.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            value = value.strip()
            if not value:
                continue
        patch[key] = value
    return patch


def order_snapshot(ficha: dict[str, Any] | None) -> dict[str, Any]:
    ficha = ficha if isinstance(ficha, dict) else {}
    return {key: ficha[key] for key in ORDER_FIELD_SCHEMAS if nonempty(ficha.get(key))}


def evidence_from_ficha(ficha: dict[str, Any] | None) -> list[str]:
    """Deriva únicamente evidencia respaldada por campos explícitos."""
    ficha = ficha if isinstance(ficha, dict) else {}
    evidence: list[str] = []

    if nonempty(ficha.get("product")):
        evidence.append("product_identified")
    if any(
        nonempty(ficha.get(key))
        for key in (
            "product_variant",
            "product_configuration",
            "material",
            "color",
            "legs",
            "order_extras",
        )
    ):
        evidence.append("product_preference")
    if confirmed(ficha.get("order_confirmation")):
        evidence.extend(("explicit_interest", "order_confirmation"))
    if nonempty(ficha.get("quantity_confirmed")):
        evidence.append("quantity_confirmed")
    if confirmed(ficha.get("configuration_complete")):
        evidence.append("configuration_complete")
    if nonempty(ficha.get("delivery_commune")) or nonempty(ficha.get("geo")):
        evidence.append("delivery_commune")
    if nonempty(ficha.get("delivery_address")):
        evidence.append("delivery_address")
    if nonempty(ficha.get("recipient_confirmed")):
        evidence.append("recipient_confirmed")

    return list(dict.fromkeys(evidence))
