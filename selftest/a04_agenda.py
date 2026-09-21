"""E2E A04: herramientas de agenda de NEA contra un Parley real aislado.

Requiere que el operador haya levantado Parley con AGENDA=on y creado dos
conversaciones reales. No toca Meta, Google ni producción.
"""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace
from typing import Any

from app.crm import CrmClient
from app.state import MemoryStore
from app.tools import ToolRuntime


CRM_BASE_URL = os.environ["CRM_BASE_URL"]
CRM_BOT_API_KEY = os.environ["CRM_BOT_API_KEY"]
CONVERSATION_1 = os.environ["A04_CONVERSATION_1"]
CONVERSATION_2 = os.environ["A04_CONVERSATION_2"]


def check(name: str, condition: bool, detail: Any = None) -> None:
    if not condition:
        suffix = f" — {detail!r}" if detail is not None else ""
        raise AssertionError(f"FAIL A04: {name}{suffix}")
    print(f"  OK  A04: {name}")


async def runtime_for(
    crm: CrmClient, crm_conversation_id: str, identity: str
) -> ToolRuntime:
    store = MemoryStore()
    conv = await store.get_or_create_conversation(identity)
    ctx = SimpleNamespace(crm=crm, store=store, agenda_enabled=True)
    return ToolRuntime(ctx, conv, crm_conversation_id)


def by_start(payload: dict[str, Any]) -> dict[str, dict[str, str]]:
    return {
        str(slot["start_utc"]): slot
        for slot in payload.get("slots") or []
        if isinstance(slot, dict) and slot.get("start_utc")
    }


async def main() -> None:
    crm = CrmClient(CRM_BASE_URL, CRM_BOT_API_KEY, timeout=30)
    try:
        first = await runtime_for(crm, CONVERSATION_1, "a04-e2e-1")
        second = await runtime_for(crm, CONVERSATION_2, "a04-e2e-2")

        offer_1 = await first.execute("propose_slots", {})
        offer_2 = await second.execute("propose_slots", {})
        check("Parley devuelve disponibilidad real a NEA", offer_1.get("ok") is True)
        check("segunda conversación recibe disponibilidad real", offer_2.get("ok") is True)

        slots_1 = by_start(offer_1)
        slots_2 = by_start(offer_2)
        common = sorted(set(slots_1).intersection(slots_2))
        check("existe un slot común ofrecido antes de reservar", bool(common))

        start = common[0]
        label_1 = slots_1[start]["label"]
        label_2 = slots_2[start]["label"]

        # El modelo no puede inventarse una confirmación que el cliente no dio.
        first._user_text = "quiero pensarlo un poco"
        first._previous_assistant_text = f"¿Te aparto el {label_1}?"
        rejected = await first.execute(
            "book_session",
            {"start_utc": start, "dia_confirmado": "sí, ese horario"},
        )
        check(
            "confirmación inventada no reserva",
            rejected.get("error") == "confirmacion_requerida",
            rejected,
        )

        # Tampoco puede escoger una hora que Parley nunca ofreció.
        not_offered = await first.execute(
            "book_session",
            {
                "start_utc": "2099-01-01T12:00:00Z",
                "dia_confirmado": "sí, ese horario",
            },
        )
        check(
            "horario no ofrecido se rechaza antes de reservar",
            not_offered.get("error") == "slot_no_ofrecido",
            not_offered,
        )

        # Confirmación real del slot exacto.
        first._user_text = "sí, ese horario"
        first._previous_assistant_text = f"¿Te aparto el {label_1}?"
        booked = await first.execute(
            "book_session",
            {"start_utc": start, "dia_confirmado": "sí, ese horario"},
        )
        check("elección confirmada crea cita real", booked.get("ok") is True, booked)

        # La segunda conversación tenía el mismo slot ofrecido antes de la
        # reserva. Parley debe volver a validar y detectar la carrera.
        second._user_text = "sí, ese horario"
        second._previous_assistant_text = f"¿Te aparto el {label_2}?"
        conflict = await second.execute(
            "book_session",
            {"start_utc": start, "dia_confirmado": "sí, ese horario"},
        )
        check(
            "Parley rechaza doble booking con slot_taken",
            conflict.get("error") == "slot_taken",
            conflict,
        )
        check(
            "slot_taken devuelve nuevas alternativas",
            bool(conflict.get("slots")),
            conflict,
        )

        print("\n===== A04 E2E: 8/8 checks OK =====")
    finally:
        await crm.aclose()


if __name__ == "__main__":
    asyncio.run(main())
