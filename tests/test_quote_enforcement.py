"""El LLM nunca puede sustituir con aritmética la cotización de Parley."""
from __future__ import annotations

import httpx

from app.llm import LlmReply, ToolCall
from app.tools import ToolRuntime
from app.turn import _asks_for_quote, _tool_loop
from tests.conftest import CRM_CONV_ID, CRM_URL, FakeLLM, IDENTITY, make_ctx


def _official_quote(total: int = 23_000_000) -> dict:
    return {
        "available": True,
        "quote": {
            "ok": True,
            "currency": "CLP",
            "basePriceCents": 16_500_000,
            "subtotalCents": 22_000_000,
            "shippingCents": 1_000_000,
            "totalCents": total,
            "applied": ["Patas de madera", "Puff adicional", "Cojín adicional"],
        },
    }


async def _runtime(llm: FakeLLM, *, enabled: bool = True):
    ctx = make_ctx(llm=llm)
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    runtime = ToolRuntime(
        ctx,
        conv,
        CRM_CONV_ID,
        context={
            "quote": {"enabled": enabled},
            "contact": {"ficha": {"order_confirmation": True}},
        },
    )
    return ctx, runtime


async def test_descarta_total_del_modelo_y_fuerza_cotizacion_oficial(respx_mock):
    llm = FakeLLM(
        [
            LlmReply(content="El total estimado es $220.000."),
            LlmReply(content="El total oficial, despacho incluido, es $230.000."),
        ]
    )
    ctx, runtime = await _runtime(llm)
    quote = respx_mock.post(f"{CRM_URL}/api/bot/quote").mock(
        return_value=httpx.Response(200, json=_official_quote())
    )

    result = await _tool_loop(
        ctx,
        [{"role": "user", "content": "¿Cuánto voy a pagar con el despacho?"}],
        runtime,
    )

    assert result == "El total oficial, despacho incluido, es $230.000."
    assert quote.call_count == 1
    assert len(llm.calls) == 2
    assert '"totalCents": 23000000' in llm.calls[1]["messages"][-1]["content"]
    await ctx.crm.aclose()


async def test_recalcula_despues_de_actualizar_un_adicional(respx_mock):
    llm = FakeLLM(
        [
            LlmReply(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="update-1",
                        name="update_ficha",
                        arguments={
                            "legs": "madera",
                            "order_extras": [
                                {"label": "puff adicional", "quantity": 2},
                                {"label": "cojín adicional", "quantity": 4},
                            ],
                        },
                    )
                ],
            ),
            LlmReply(content="Quedaría en $220.000."),
            LlmReply(content="El total oficial es $230.000 con despacho."),
        ]
    )
    ctx, runtime = await _runtime(llm)
    respx_mock.put(f"{CRM_URL}/api/bot/ficha").mock(
        return_value=httpx.Response(200, json={"ficha": {}})
    )
    quote = respx_mock.post(f"{CRM_URL}/api/bot/quote").mock(
        return_value=httpx.Response(200, json=_official_quote())
    )

    result = await _tool_loop(
        ctx,
        [{"role": "user", "content": "Sí, con patas de madera y dos puffs más."}],
        runtime,
    )

    assert result == "El total oficial es $230.000 con despacho."
    assert quote.call_count == 1
    await ctx.crm.aclose()


async def test_no_cotiza_por_elegir_color_sin_intencion_de_compra(respx_mock):
    llm = FakeLLM(
        [
            LlmReply(
                content=None,
                tool_calls=[
                    ToolCall(
                        id="update-color",
                        name="update_ficha",
                        arguments={"color": "mostaza"},
                    )
                ],
            ),
            LlmReply(content="Sí, el mostaza está disponible."),
        ]
    )
    ctx, runtime = await _runtime(llm)
    runtime._context["contact"]["ficha"] = {}
    respx_mock.put(f"{CRM_URL}/api/bot/ficha").mock(
        return_value=httpx.Response(200, json={"ficha": {"color": "mostaza"}})
    )
    quote = respx_mock.post(f"{CRM_URL}/api/bot/quote")

    result = await _tool_loop(
        ctx,
        [{"role": "user", "content": "Me gusta el mostaza."}],
        runtime,
    )

    assert result == "Sí, el mostaza está disponible."
    assert quote.call_count == 0
    await ctx.crm.aclose()


async def test_cotizador_apagado_conserva_compatibilidad(respx_mock):
    llm = FakeLLM([LlmReply(content="El equipo debe confirmar el precio.")])
    ctx, runtime = await _runtime(llm, enabled=False)
    quote = respx_mock.post(f"{CRM_URL}/api/bot/quote")

    result = await _tool_loop(
        ctx,
        [{"role": "user", "content": "¿Cuál es el precio total?"}],
        runtime,
    )

    assert result == "El equipo debe confirmar el precio."
    assert quote.call_count == 0
    await ctx.crm.aclose()


def test_reconoce_continuacion_corta_de_una_consulta_de_despacho():
    messages = [
        {"role": "user", "content": "¿Cuánto cuesta el envío a Viña del Mar?"},
        {"role": "assistant", "content": "Déjame revisarlo."},
        {"role": "user", "content": "¿Y a Pudahuel?"},
    ]
    assert _asks_for_quote(messages) is True
