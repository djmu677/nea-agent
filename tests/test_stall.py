"""Candado de cierre: conversación que no va a ningún lado.

El agente se despide amable UNA vez y después calla. El conteo es determinista
(app/stall.py); aquí se fija tanto el detector como el silencio posterior.
"""
from __future__ import annotations

import asyncio

import pytest

from app.stall import es_relleno, racha_vacia, sin_rumbo
from app.state import utcnow
from tests.conftest import IDENTITY, mock_crm_basics, wa_body


# ------------------------------------------------------------- detector ---


@pytest.mark.parametrize(
    "texto",
    ["ok", "va", "ajá", "jaja", "jajaja", "jejeje", "👍", "🙏🙏", "  ", "Gracias"],
)
def test_relleno_no_aporta(texto):
    assert es_relleno(texto) is True


@pytest.mark.parametrize(
    "texto",
    [
        "tengo una clínica dental",
        "no me interesa",  # un "no" es una respuesta clarísima, no relleno
        "ahorita no puedo, la otra semana",
        "somos 12",
        "ok pero cuánto cuesta",
        "hola",  # un saludo es jugada legítima, no vacío
        "buenas tardes",
    ],
)
def test_contenido_real_no_es_relleno(texto):
    assert es_relleno(texto) is False


def test_un_mensaje_con_contenido_corta_la_racha():
    assert racha_vacia(["ok", "va", "tengo una taquería", "ok"]) == 1
    assert racha_vacia(["tengo una taquería", "ok", "va", "ajá"]) == 3


def test_sin_rumbo_por_racha_de_vacios():
    assert sin_rumbo(["hola", "ok", "va", "ajá"], "descubrimiento") is True
    # Dos seguidos no bastan: el saludo NO cuenta como vacío.
    assert sin_rumbo(["hola", "ok", "va"], "descubrimiento") is False


def test_sin_rumbo_por_conversacion_larga_sin_avance():
    largos = [f"mensaje con contenido numero {i}" for i in range(14)]
    assert sin_rumbo(largos, "descubrimiento") is True


def test_agendando_nunca_se_cierra_por_el_candado():
    """Ya hay rumbo: el candado no se mete aunque el lead conteste corto."""
    assert sin_rumbo(["ok", "va", "ajá"], "agendando") is False
    assert sin_rumbo([f"m{i}" * 10 for i in range(20)], "cerrada") is False


# ----------------------------------------------------------- en el turno ---


async def _tres_vacios(ctx, client):
    for i, texto in enumerate(("ok", "va", "ajá")):
        await client.post(
            "/webhook", content=wa_body(text=texto, wamid=f"wamid.v{i}")
        )
        await asyncio.sleep(0.35)


async def test_por_defecto_nunca_cierra_ni_calla_por_falta_de_rumbo(
    ctx, client, respx_mock
):
    routes = mock_crm_basics(respx_mock)
    await _tres_vacios(ctx, client)

    # Atiende todos los mensajes, incluso una racha que el detector marcaría.
    assert routes["messages"].call_count == 3
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    assert conv.stalled_at is None

    await client.post("/webhook", content=wa_body(text="oye", wamid="wamid.v9"))
    await asyncio.sleep(0.35)
    assert routes["messages"].call_count == 4
    assert len(ctx.llm.calls) == 4


async def test_un_cierre_historico_se_libera_en_el_siguiente_mensaje(
    ctx, client, respx_mock
):
    routes = mock_crm_basics(respx_mock)
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    await ctx.store.update_conversation(
        conv.id,
        crm_conversation_id="cv_test1",
        stalled_at=utcnow(),
        phase="cerrada",
    )

    await client.post(
        "/webhook", content=wa_body(text="oye, ya lo pensé mejor", wamid="wamid.r1")
    )
    await asyncio.sleep(0.35)

    assert routes["messages"].call_count == 1  # volvió a atenderlo
    recuperada = await ctx.store.get_or_create_conversation(IDENTITY)
    assert recuperada.stalled_at is None
    assert recuperada.phase == "descubrimiento"


async def test_el_cierre_historico_sigue_disponible_si_se_activa(
    ctx, client, respx_mock
):
    ctx.settings.stall_enabled = True
    routes = mock_crm_basics(respx_mock)
    await _tres_vacios(ctx, client)

    assert routes["messages"].call_count == 3
    assert (await ctx.store.get_or_create_conversation(IDENTITY)).stalled_at is not None

    llamadas_previas = len(ctx.llm.calls)
    await client.post("/webhook", content=wa_body(text="oye", wamid="wamid.v9"))
    await asyncio.sleep(0.35)
    assert routes["messages"].call_count == 3
    assert len(ctx.llm.calls) == llamadas_previas


async def test_el_candado_no_pisa_el_handoff_por_hostilidad(
    ctx, client, respx_mock
):
    """Tres groserías seguidas son cortas y podrían leerse como "relleno": el
    handoff por hostilidad manda, porque el dueño tiene que ver esa conversación."""
    routes = mock_crm_basics(respx_mock)
    for i, texto in enumerate(("son unos rateros", "puro humo", "pinches estafadores")):
        await client.post(
            "/webhook", content=wa_body(text=texto, wamid=f"wamid.h{i}")
        )
        await asyncio.sleep(0.35)

    assert routes["handoff"].call_count == 1
    assert (await ctx.store.get_or_create_conversation(IDENTITY)).stalled_at is None


async def test_un_cierre_historico_no_impide_mostrar_escribiendo_y_responder(
    ctx, client, respx_mock
):
    routes = mock_crm_basics(respx_mock)
    conv = await ctx.store.get_or_create_conversation(IDENTITY)
    await ctx.store.update_conversation(
        conv.id, crm_conversation_id="cv_test1", stalled_at=utcnow()
    )
    previos = routes["typing"].call_count

    await client.post("/webhook", content=wa_body(text="sigues ahi?", wamid="wamid.t1"))
    await asyncio.sleep(0.35)

    assert routes["typing"].call_count == previos + 1
    assert routes["messages"].call_count == 1
