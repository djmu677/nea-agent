"""Regresión del pedido: comprar no equivale a handoff y una entrega es agenda real."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from app.profile import BusinessProfile
from app.prompt import build_system_prompt
from app.state import Conversation, OfferedSlot
from app.tools import ToolRuntime, tool_schemas
from tests.conftest import CRM_CONV_ID, CRM_URL, IDENTITY, make_ctx


def test_sin_calendario_continua_el_pedido_en_vez_de_escalarlo() -> None:
    prompt = build_system_prompt(
        profile=BusinessProfile(agent_name="Globo"),
        context={"lead": {"stageName": "Interesado"}},
        conv=Conversation(id=1, wa_identity=IDENTITY),
        agenda=False,
    )
    assert "Esto NO justifica handoff" in prompt
    assert "cuando el lead quiera avanzar, haz handoff" not in prompt
    assert "dirección, receptor y la fecha solicitada" in prompt


@pytest.mark.parametrize(
    "reason",
    [
        "cliente quiere avanzar",
        "intención de compra clara",
        "quiere confirmar el pedido",
        "agenda apagada para coordinar entrega",
    ],
)
async def test_intencion_de_compra_no_puede_activar_handoff(reason: str) -> None:
    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    runtime = ToolRuntime(
        ctx,
        conv,
        CRM_CONV_ID,
        user_text="Sí, quiero comprarlo y recibirlo el sábado",
    )
    result = await runtime.execute("handoff", {"reason": reason})
    assert result["ok"] is False
    assert result["error"] == "handoff_not_justified"
    assert runtime.handoff_reason is None
    await ctx.crm.aclose()


async def test_solicitud_explicita_de_asesor_sigue_activando_handoff() -> None:
    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    runtime = ToolRuntime(
        ctx,
        conv,
        CRM_CONV_ID,
        user_text="Quiero hablar con un asesor humano",
    )
    result = await runtime.execute("handoff", {"reason": "pidió humano"})
    assert result["ok"] is True
    assert runtime.handoff_reason == "cliente"
    await ctx.crm.aclose()


async def test_entrega_reserva_el_slot_con_tipo_delivery(respx_mock) -> None:
    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    slot = OfferedSlot(
        conversation_id=conv.id,
        start_utc=datetime(2026, 9, 19, 15, 0, tzinfo=timezone.utc),
        end_utc=None,
        label="sábado 19 de septiembre, 12:00",
    )
    await ctx.store.replace_offered_slots(conv.id, [slot])
    booking = respx_mock.post(f"{CRM_URL}/api/bot/bookings").mock(
        return_value=httpx.Response(
            201,
            json={
                "bookingId": "bk_delivery",
                "meetingLink": None,
                "linkPending": False,
                "label": slot.label,
            },
        )
    )
    respx_mock.put(f"{CRM_URL}/api/bot/ficha").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    runtime = ToolRuntime(
        ctx,
        conv,
        CRM_CONV_ID,
        context={
            "lead": {"stageName": "Pedido"},
            "pipelineStages": [
                {"name": "Pedido", "botStageKey": "order"},
            ],
        },
    )
    result = await runtime.execute(
        "book_delivery",
        {
            "start_utc": "2026-09-19T15:00:00Z",
            "dia_confirmado": "Sí, el sábado 19 a las 12",
        },
    )
    assert result["ok"] is True
    assert "ENTREGA" in result["instrucciones"]
    assert json.loads(booking.calls[0].request.content)["kind"] == "delivery"
    await ctx.crm.aclose()


async def test_entrega_no_puede_saltarse_el_validador_de_pedido(respx_mock) -> None:
    ctx = make_ctx()
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    booking = respx_mock.post(f"{CRM_URL}/api/bot/bookings").mock(
        return_value=httpx.Response(201, json={})
    )
    runtime = ToolRuntime(
        ctx,
        conv,
        CRM_CONV_ID,
        context={"lead": {"stageName": "Interesado"}},
    )
    result = await runtime.execute(
        "book_delivery",
        {"start_utc": "2026-09-19T15:00:00Z", "dia_confirmado": "sí"},
    )
    assert result["ok"] is False
    assert result["error"] == "order_stage_required"
    assert booking.call_count == 0
    await ctx.crm.aclose()


def test_book_delivery_solo_existe_con_agenda_activa() -> None:
    with_agenda = {tool["function"]["name"] for tool in tool_schemas(True)}
    without_agenda = {tool["function"]["name"] for tool in tool_schemas(False)}
    assert "book_delivery" in with_agenda
    assert "book_delivery" not in without_agenda


async def test_pedido_completo_desde_nuevo_avanza_sin_saltar_columnas(
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
        context={
            "lead": {"stageName": "Nuevo"},
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
                    },
                },
                {
                    "name": "Interesado",
                    "position": 2,
                    "botMoveEnabled": True,
                    "evidenceRule": {
                        "allOf": ["product_identified"],
                        "anyOf": ["product_preference", "explicit_interest"],
                    },
                },
                {
                    "name": "Pedido",
                    "position": 3,
                    "botMoveEnabled": True,
                    "evidenceRule": {
                        "allOf": [
                            "product_identified",
                            "order_confirmation",
                            "quantity_confirmed",
                            "configuration_complete",
                            "delivery_commune",
                            "delivery_address",
                            "recipient_confirmed",
                        ],
                        "anyOf": [],
                    },
                },
            ],
        },
    )
    evidence = [
        "product_identified",
        "explicit_interest",
        "order_confirmation",
        "quantity_confirmed",
        "configuration_complete",
        "delivery_commune",
        "delivery_address",
        "recipient_confirmed",
    ]
    result = await runtime.execute(
        "move_stage", {"stage": "Pedido", "evidence": evidence}
    )

    assert result == {"ok": True, "stageMoved": True, "stage": "Pedido"}
    assert [
        json.loads(call.request.content)["stage"] for call in stage_route.calls
    ] == ["En conversación", "Interesado", "Pedido"]
    await ctx.crm.aclose()
