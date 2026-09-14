"""P05: matriz de regresión de las decisiones comerciales de NEA."""
from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.tools import ToolRuntime
from tests.conftest import CRM_CONV_ID, CRM_URL, IDENTITY, make_ctx


CONFIRMED_ORDER = [
    "product_identified",
    "order_confirmation",
]
COMPLETE_ORDER = CONFIRMED_ORDER + [
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
                    "anyOf": [
                        "commercial_question",
                        "product_identified",
                        "four_customer_turns",
                    ],
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
                    "allOf": CONFIRMED_ORDER,
                    "anyOf": [],
                    "blockerCodes": ["missing_product", "ambiguous_confirmation"],
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
        None,
        id="intencion-inequivoca-avanza-a-pedido-aunque-falten-detalles",
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


async def test_pedido_solo_rechaza_si_falta_producto_o_confirmacion(respx_mock) -> None:
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
                "evidence": ["product_identified"],
            },
        )
    finally:
        await ctx.crm.aclose()

    assert result["missingEvidence"] == ["order_confirmation"]
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

    assert result == {"ok": True, "stageMoved": True, "stage": "Pedido"}
    assert stage_route.call_count == 3


async def test_respaldo_determinista_lleva_intencion_explicita_hasta_pedido(
    respx_mock,
) -> None:
    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    context = pipeline_context("Nuevo")
    context["contact"] = {"ficha": {"product": "Sofá Catalina"}}
    ficha_route = respx_mock.put(f"{CRM_URL}/api/bot/ficha").mock(
        return_value=httpx.Response(
            200,
            json={
                "ficha": {
                    "product": "Sofá Catalina",
                    "order_confirmation": True,
                }
            },
        )
    )

    def accepted(request: httpx.Request) -> httpx.Response:
        stage = json.loads(request.content)["stage"]
        return httpx.Response(
            200,
            json={"stageMoved": True, "lead": {"stageName": stage}},
        )

    stage_route = respx_mock.post(f"{CRM_URL}/api/bot/stage").mock(
        side_effect=accepted
    )
    runtime = ToolRuntime(ctx, conv, CRM_CONV_ID, context=context)
    try:
        result = await runtime.ensure_pipeline_progress(
            customer_turns=2,
            explicit_purchase_intent=True,
        )
    finally:
        await ctx.crm.aclose()

    assert result == {"ok": True, "stageMoved": True, "stage": "Pedido"}
    assert ficha_route.call_count == 1
    assert [
        json.loads(call.request.content)["stage"] for call in stage_route.calls
    ] == ["En conversación", "Interesado", "Pedido"]


async def test_cuarta_intervencion_avanza_a_en_conversacion_sin_comprar(
    respx_mock,
) -> None:
    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    stage_route = respx_mock.post(f"{CRM_URL}/api/bot/stage").mock(
        return_value=httpx.Response(
            200,
            json={
                "stageMoved": True,
                "lead": {"stageName": "En conversación"},
            },
        )
    )
    runtime = ToolRuntime(
        ctx,
        conv,
        CRM_CONV_ID,
        context=pipeline_context("Nuevo"),
    )
    try:
        result = await runtime.ensure_pipeline_progress(
            customer_turns=4,
            explicit_purchase_intent=False,
        )
    finally:
        await ctx.crm.aclose()

    assert result == {
        "ok": True,
        "stageMoved": True,
        "stage": "En conversación",
    }
    assert json.loads(stage_route.calls[0].request.content)["evidence"] == [
        "four_customer_turns"
    ]
