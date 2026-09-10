"""P01: caracterización ejecutable de la conversación real del sofá Napoleón."""
from __future__ import annotations

import json
from pathlib import Path


FIXTURE = Path(__file__).parent / "fixtures" / "napoleon_pipeline_case.json"


def load_case() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_conversacion_napoleon_conserva_el_historial_observado_exacto() -> None:
    case = load_case()
    events = case["events"]

    assert [event["sequence"] for event in events] == list(range(1, 24))
    assert events[0]["text"] == "/reset"
    assert events[4]["text"] == "Me gustaría saber más de napoleon"
    assert events[8]["text"] == "Me gusta la felpa, tienes muestras de colores?"
    assert events[11]["kind"] == "media"
    assert events[13]["text"] == (
        "Primero quisiera saber cuánto cuesta el envío para conchalí, "
        "me interesaría mucho tener ese mueble"
    )
    assert events[-2]["text"] == "Si"
    assert events[-1]["actor"] == "assistant"


def test_conversacion_napoleon_demuestra_el_fallo_de_movimiento() -> None:
    case = load_case()
    events = case["events"]
    mismatches = [
        event for event in events
        if event["observedStageAfter"] != event["expectedStageAfter"]
    ]

    assert case["observedFinalStage"] == "Nuevo"
    assert case["expectedFinalStage"] == "Interesado"
    assert mismatches[0]["sequence"] == 5
    assert mismatches[0]["expectedStageAfter"] == "En conversación"
    assert any(
        event["sequence"] == 9 and event["expectedStageAfter"] == "Interesado"
        for event in mismatches
    )
    assert all(event["observedStageAfter"] == "Nuevo" for event in events)


def test_conversacion_napoleon_no_cumple_aun_un_pedido_completo() -> None:
    case = load_case()
    expected_stages = {event["expectedStageAfter"] for event in case["events"]}

    assert "Pedido" not in expected_stages
    assert case["missingForPedido"] == [
        "color definitivo",
        "dirección completa de entrega",
        "datos de contacto o receptor confirmados",
    ]
