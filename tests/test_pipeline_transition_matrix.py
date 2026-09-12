"""P05: matriz de regresión de las decisiones comerciales de NEA."""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.tools import ToolRuntime
from tests.conftest import CRM_CONV_ID, CRM_URL, IDENTITY, make_ctx


COMPLETE_ORDER = [
    "product_identified",
    "order_confirmation",
    "quantity_confirmed",
    "configuration_complete",
    "delivery_commune",
    "delivery_address",
    "recipient_confirmed",
]


def pipeline_context(current: str) -> dict[str, Any]:
    return {
        "lead": {"stageName": current},
        # Parley expone únicamente etapas abiertas. Cliente y Perdido no deben
        # estar disponibles para el modelo ni poder llegar a su API.
        "pipelineStages": [
            {
                "name": "Nuevo",
                "position": 0,
                "botMoveEnabled": True,
                "evidenceRule": None,
            },
            {
                "name": "En conversación",
                "position": 1,
                "botMoveEnabled": True,
                "evidenceRule": {
                    "allOf": [],
                    "anyOf": ["commercial_question", "product_identified"],
                    "blockerCodes": ["greeting_only", "generic_question_only"],
                },
            },
            {
                "name": "Interesado",
                "position": 2,
                "botMoveEnabled": True,
                "evidenceRule": {
                    "allOf": ["product_identified"],
                    "anyOf": [
                        "product_preference",
                        "explicit_interest",
                        "price_question",
                        "delivery_question",
                    ],
                    "blockerCodes": ["missing_product", "missing_buying_signal"],
                },
            },
            {
                "name": "Pedido",
                "position": 3,
                "botMoveEnabled": True,
                "evidenceRule": {
                    "allOf": COMPLETE_ORDER,
                    "anyOf": [],
                    "blockerCodes": [
                        "missing_product",
                        "ambiguous_confirmation",
                        "missing_quantity",
                        "missing_configuration",
                        "missing_delivery_commune",
                        "missing_delivery_address",
                        "missing_recipient",
                    ],
                },
            },
        ],
    }


CASES = [
    pytest.param(
        "Nuevo",
        "En conversación",
        [],
        "insufficient_evidence",
        id="saludo-permanece-en-nuevo",
    ),
    pytest.param(
        "Nuevo",
        "En conversación",
        ["commercial_question"],
        None,
        id="pregunta-real-inicia-conversacion",
    ),
    pytest.param(
        "En conversación",
        "Interesado",
        ["product_identified", "explicit_interest"],
        None,
        id="interes-fuerte-avanza-a-interesado",
    ),
    pytest.param(
        "Interesado",
        "Pedido",
        ["product_identified", "order_confirmation", "delivery_commune"],
        "insufficient_evidence",
        id="pedido-incompleto-permanece-en-interesado",
    ),
    pytest.param(
        "Interesado",
        "Pedido",
        COMPLETE_ORDER,
        None,
        id="pedido-completo-avanza-a-pedido",
    ),
    pytest.param(
        "Interesado",
        "En conversación",
        ["commercial_question"],
        "backward_stage",
        id="retroceso-rechazado",
    ),
    pytest.param(
        "Pedido",
        "Cliente",
        COMPLETE_ORDER,
        "stage_not_available",
        id="cliente-no-disponible-para-nea",
    ),
    pytest.param(
        "Pedido",
        "Perdido",
        [],
        "stage_not_available",
        id="perdido-no-disponible-para-nea",
    ),
]


@pytest.mark.parametrize("current,target,evidence,error", CASES)
async def test_matriz_completa_antes_de_escribir_en_parley(
    current: str,
    target: str,
    evidence: list[str],
    error: str | None,
    respx_mock,
) -> None:
    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    stage_route = respx_mock.post(f"{CRM_URL}/api/bot/stage").mock(
        return_value=httpx.Response(
            200,
            json={"stageMoved": True, "lead": {"stageName": target}},
        )
    )
    runtime = ToolRuntime(
        ctx,
        conv,
        CRM_CONV_ID,
        context=pipeline_context(current),
    )

    try:
        result = await runtime.execute(
            "move_stage", {"stage": target, "evidence": evidence}
        )
    finally:
        await ctx.crm.aclose()

    if error is None:
        assert result == {"ok": True, "stageMoved": True, "stage": target}
        assert stage_route.call_count == 1
    else:
        assert result["ok"] is False
        assert result["error"] == error
        assert stage_route.call_count == 0


async def test_pedido_incompleto_devuelve_los_datos_que_faltan(respx_mock) -> None:
    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    stage_route = respx_mock.post(f"{CRM_URL}/api/bot/stage").mock(
        return_value=httpx.Response(200, json={})
    )
    runtime = ToolRuntime(
        ctx,
        conv,
        CRM_CONV_ID,
        context=pipeline_context("Interesado"),
    )

    try:
        result = await runtime.execute(
            "move_stage",
            {
                "stage": "Pedido",
                "evidence": [
                    "product_identified",
                    "order_confirmation",
                    "delivery_commune",
                ],
            },
        )
    finally:
        await ctx.crm.aclose()

    assert result["missingEvidence"] == [
        "quantity_confirmed",
        "configuration_complete",
        "delivery_address",
        "recipient_confirmed",
    ]
    assert stage_route.call_count == 0


async def test_solicitud_lejana_avanza_solo_hasta_la_evidencia_disponible(
    respx_mock,
) -> None:
    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)

    def accepted(request: httpx.Request) -> httpx.Response:
        stage = json.loads(request.content)["stage"]
        return httpx.Response(
            200,
            json={"stageMoved": True, "lead": {"stageName": stage}},
        )

    stage_route = respx_mock.post(f"{CRM_URL}/api/bot/stage").mock(
        side_effect=accepted
    )
    runtime = ToolRuntime(
        ctx,
        conv,
        CRM_CONV_ID,
        context=pipeline_context("Nuevo"),
    )
    try:
        result = await runtime.execute(
            "move_stage",
            {
                "stage": "Pedido",
                "evidence": [
                    "product_identified",
                    "explicit_interest",
                    "order_confirmation",
                ],
            },
        )
    finally:
        await ctx.crm.aclose()

    assert result["ok"] is False
    assert result["error"] == "insufficient_evidence"
    assert result["stageMoved"] is True
    assert result["stage"] == "Interesado"
    assert result["requestedStage"] == "Pedido"
    assert stage_route.call_count == 2
