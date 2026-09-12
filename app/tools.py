"""Herramientas del LLM: ficha, kanban, agenda, descarte y handoff.

Solo se reserva lo que se ofreció, y **quien manda sobre eso es el CRM**:
Vocero guarda la oferta contra la conversación y rechaza cualquier otro
instante. La tabla `offered_slots` de Nea es un ESPEJO de esa oferta, no una
segunda fuente de verdad: sirve para etiquetar con el día en palabras y para
frenar una alucinación antes de gastar un viaje de red. Si el CRM dice que un
horario no se ofreció, el espejo está viejo y se resincroniza con lo que él
mande.

Un fallo del CRM dentro de una tool regresa `{"ok": false, ...}` al LLM —
nunca tumba el turno.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from app.crm import (
    AgendaUnavailable,
    CrmConflict,
    CrmError,
    SlotNotOffered,
    SlotTaken,
    canonical_handoff_reason,
)
from app.profile import BusinessProfile
from app.state import AppContext, Conversation, OfferedSlot

logger = logging.getLogger("nea.tools")

COMMERCIAL_EVIDENCE_KEYS = (
    "commercial_question",
    "product_identified",
    "product_preference",
    "explicit_interest",
    "price_question",
    "delivery_question",
    "order_confirmation",
    "quantity_confirmed",
    "configuration_complete",
    "delivery_commune",
    "delivery_address",
    "recipient_confirmed",
)
COMMERCIAL_EVIDENCE_SET = frozenset(COMMERCIAL_EVIDENCE_KEYS)

# Cuántos huecos quedan RESERVABLES tras un propose_slots. El agente muestra 3
# a la vez (regla del prompt), pero guardar solo 3 lo dejaba sin nada que
# ofrecer cuando el lead pedía otro día: el catálogo reservable es más ancho
# que el menú que se enseña.
MAX_OFFERED = 12
# Reparto pedido al CRM: hasta 3 huecos por día, en 5 días distintos.
OFFER_PER_DAY = 3
OFFER_DAYS = 5

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "update_ficha",
            "description": (
                "Guarda o actualiza la ficha del lead en el CRM (merge: solo los "
                "campos que mandes). Llámala en cuanto descubras un dato nuevo."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "rubro": {"type": "string"},
                    "rol": {
                        "type": "string",
                        "description": "dueno | hijo_del_dueno | empleado | otro",
                    },
                    "tamano_aprox": {"type": "string"},
                    "sistemas": {"type": "string"},
                    "dolor_principal": {"type": "string"},
                    "geo": {"type": "string"},
                    "calificado": {"type": "boolean"},
                    "resultado": {
                        "type": "string",
                        "description": "agendo | dio_diy | handoff | sin_respuesta",
                    },
                    "notas": {"type": "string"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_stage",
            "description": (
                "Avanza el lead a una etapa ABIERTA y habilitada del kanban, "
                "solo cuando se cumpla la regla de decisión de esa etapa en el "
                "contexto. Usa exactamente uno de los nombres habilitados. Nunca "
                "la uses para retroceder ni para declarar Cliente/ganado o Perdido."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "stage": {
                        "type": "string",
                        "description": "Nombre exacto de la etapa abierta de destino",
                    },
                    "evidence": {
                        "type": "array",
                        "description": (
                            "Evidencias ya demostradas por mensajes del cliente o "
                            "por su ficha. Incluye solo claves del catálogo; nunca "
                            "inventes una evidencia para lograr el movimiento."
                        ),
                        "items": {
                            "type": "string",
                            "enum": list(COMMERCIAL_EVIDENCE_KEYS),
                        },
                        "uniqueItems": True,
                    },
                },
                "required": ["stage", "evidence"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_media",
            "description": (
                "Envía una imagen o video aprobado de la biblioteca del negocio. "
                "Úsala solo si el recurso aparece en el contexto y su regla de "
                "uso coincide con lo pedido por el lead. Nunca inventes un asset_id."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "asset_id": {
                        "type": "string",
                        "description": "ID exacto de un recurso aprobado disponible",
                    }
                },
                "required": ["asset_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "propose_slots",
            "description": (
                "Consulta la disponibilidad real de la agenda del negocio. Te "
                "regresa los huecos libres REPARTIDOS entre los próximos días, "
                "cada uno con su día en palabras (hoy/mañana/nombre del día). "
                "Ofrece al lead máximo 3, los que embonen con lo que pidió. Si "
                "el día que pidió no aparece, es que no hay agenda ese día: "
                "dilo. SOLO estos horarios serán reservables después."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "book_session",
            "description": (
                "Reserva la cita en uno de los horarios previamente ofrecidos. "
                "start_utc debe ser EXACTAMENTE el start_utc de un slot ofrecido "
                "en esta conversación. Llámala SOLO después de haber nombrado el "
                "día completo y de que el lead lo aceptara sin ambigüedad."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_utc": {
                        "type": "string",
                        "description": "ISO 8601 UTC del slot elegido, tal cual se ofreció",
                    },
                    "dia_confirmado": {
                        "type": "string",
                        "description": (
                            "Lo que el lead escribió para aceptar ESE día concreto. "
                            "Si no puedes citarlo, todavía no confirmó: pregunta "
                            "en vez de reservar."
                        ),
                    },
                },
                "required": ["start_utc", "dia_confirmado"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "book_delivery",
            "description": (
                "Reserva una ENTREGA en uno de los horarios previamente ofrecidos. "
                "Úsala cuando el cliente esté cerrando un pedido físico, después "
                "de confirmar día completo, hora, dirección y receptor."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_utc": {
                        "type": "string",
                        "description": "ISO 8601 UTC exacto del slot ofrecido",
                    },
                    "dia_confirmado": {
                        "type": "string",
                        "description": "Texto con el que el cliente aceptó ese día y hora",
                    },
                },
                "required": ["start_utc", "dia_confirmado"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "reschedule_session",
            "description": (
                "Mueve la cita YA agendada del lead a otro horario ofrecido. "
                "Mismo protocolo que book_session: primero propose_slots, luego "
                "confirmas el día completo, y hasta entonces mueves."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "start_utc": {
                        "type": "string",
                        "description": "ISO 8601 UTC del nuevo slot, tal cual se ofreció",
                    },
                    "dia_confirmado": {
                        "type": "string",
                        "description": "Lo que el lead escribió para aceptar ESE día",
                    },
                },
                "required": ["start_utc", "dia_confirmado"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "route_out",
            "description": (
                "Marca al lead como no calificado (hoy). Después despídete con "
                "honestidad, compartiendo los recursos alternativos del negocio "
                "si existen, puerta abierta."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "handoff",
            "description": (
                "Pasa la conversación a un humano del negocio y pausa la IA. Tu "
                "mensaje de despedida se envía ANTES de la pausa — salvo en el "
                "handoff por hostilidad, donde cierras sobrio sin anunciarlo."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Motivo breve (p.ej. 'pidió humano', 'duda fuera del conocimiento')",
                    }
                },
            },
        },
    },
]


# Herramientas que solo tienen sentido si el CRM agenda.
AGENDA_TOOLS = frozenset(
    {"propose_slots", "book_session", "book_delivery", "reschedule_session"}
)
MEDIA_TOOLS = frozenset({"send_media"})


def tool_schemas(
    agenda_enabled: bool = True, media_enabled: bool = True
) -> list[dict[str, Any]]:
    """El catálogo que se le ofrece al modelo en ESTE turno.

    Contra un CRM sin agenda no se le enseñan las herramientas de agendar: si
    se le enseñan, las llama, fallan todas y el lead recibe evasivas en vez de
    un handoff limpio. Que no exista la herramienta es más claro que pedirle al
    prompt que se acuerde de no usarla.
    """
    if agenda_enabled and media_enabled:
        return TOOL_SCHEMAS
    return [
        t
        for t in TOOL_SCHEMAS
        if (
            (agenda_enabled or t.get("function", {}).get("name") not in AGENDA_TOOLS)
            and (media_enabled or t.get("function", {}).get("name") not in MEDIA_TOOLS)
        )
    ]


def _meeting(result: dict[str, Any]) -> tuple[str | None, bool]:
    """Enlace de la reunión y si el CRM lo dejó pendiente.

    Vocero devuelve `meetingLink` desde que la entrega de la reunión es un
    conector (puede ser Zoom, Google Meet o la sala fija del negocio); antes
    era `zoomJoinUrl`, y ese nombre se sigue aceptando para no romper un CRM
    viejo. Leer solo el viejo hacía que el enlace llegara SIEMPRE vacío contra
    un Vocero actual: la cita se creaba bien y el lead se quedaba sin por dónde
    entrar.

    `linkPending` es lo que evita prometer de más: la cita existe pero el
    proveedor todavía no entregó el enlace, así que se confirma la cita y se
    dice que el enlace llega en un momento.
    """
    link = result.get("meetingLink") or result.get("zoomJoinUrl")
    pending = bool(result.get("linkPending"))
    return (str(link) if link else None), pending


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except (TypeError, ValueError, AttributeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _label_of(raw: dict[str, Any], start: datetime) -> str:
    """Etiqueta con el día en palabras: "hoy viernes 7 de agosto, 10:30".

    La corta del CRM ("vie 7 ago, 10:30") se presta a que el lead entienda
    otro día: basta que conteste "10:30, de mañana" a una oferta de HOY para
    agendar mal. Si el CRM no manda `dayLabel` (respuestas sin reparto, p. ej.
    las alternativas de un slot_taken), se cae a la corta.
    """
    day_label = str(raw.get("dayLabel") or "").strip()
    time = str(raw.get("time") or "").strip()
    if day_label and time:
        return f"{day_label}, {time}"
    return str(raw.get("label") or _iso_z(start))


def _slots_from_payload(
    conversation_id: int, raw_slots: list[dict[str, Any]]
) -> list[OfferedSlot]:
    """Convierte slots del CRM ({startUtc,endUtc,label}) a OfferedSlot, tolerante."""
    out: list[OfferedSlot] = []
    for raw in raw_slots[:MAX_OFFERED]:
        start = _parse_utc(str(raw.get("startUtc") or ""))
        if start is None:
            continue
        end = _parse_utc(str(raw.get("endUtc") or "")) if raw.get("endUtc") else None
        out.append(
            OfferedSlot(
                conversation_id=conversation_id,
                start_utc=start,
                end_utc=end,
                label=_label_of(raw, start),
            )
        )
    return out


def _slots_for_llm(slots: list[OfferedSlot]) -> list[dict[str, str]]:
    return [{"start_utc": _iso_z(s.start_utc), "label": s.label} for s in slots]


def _validate_pipeline_request(
    context: dict[str, Any], stage_name: str, evidence: list[str]
) -> dict[str, Any] | None:
    """Espejo preventivo del contrato P03; Parley sigue siendo la autoridad.

    La verificación local mejora la conversación al devolver faltantes en la
    misma ronda del modelo. No reemplaza el candado multitenant de Parley ni
    concede el movimiento: la escritura siempre vuelve a validarse en el CRM.
    """
    stages = context.get("pipelineStages") or []
    if not isinstance(stages, list) or not stages:
        return None  # compatibilidad con Parley anterior a P03

    ordered_stages = [
        item
        for item in stages
        if isinstance(item, dict)
        and str(item.get("name") or "").strip()
    ]
    target = next(
        (
            item
            for item in ordered_stages
            if str(item.get("name") or "").strip().casefold() == stage_name.casefold()
        ),
        None,
    )
    if target is None:
        return {"ok": False, "error": "stage_not_available"}
    if target.get("botMoveEnabled", True) is False:
        return {"ok": False, "error": "stage_automation_disabled"}

    current_name = str((context.get("lead") or {}).get("stageName") or "").strip()
    current_index = next(
        (
            index
            for index, item in enumerate(ordered_stages)
            if str(item.get("name") or "").strip().casefold()
            == current_name.casefold()
        ),
        -1,
    )
    target_index = ordered_stages.index(target)
    if current_index >= 0 and target_index > current_index + 1:
        return {"ok": False, "error": "stage_skip"}
    if current_index >= 0 and target_index < current_index:
        return {"ok": False, "error": "backward_stage"}

    rule = target.get("evidenceRule")
    if not isinstance(rule, dict):
        return None  # Parley anterior a P03 decide con su contrato previo

    all_of = [str(key) for key in rule.get("allOf") or []]
    any_of = [str(key) for key in rule.get("anyOf") or []]
    available = set(evidence)
    missing = [key for key in all_of if key not in available]
    if any_of and not available.intersection(any_of):
        missing.extend(any_of)
    if not missing:
        return None
    return {
        "ok": False,
        "error": "insufficient_evidence",
        "missingEvidence": list(dict.fromkeys(missing)),
        "blockerCodes": list(rule.get("blockerCodes") or []),
        "detalle": "faltan datos; permanece en la etapa actual y pregunta uno por turno",
    }


def _current_stage_name(context: dict[str, Any]) -> str:
    return str((context.get("lead") or {}).get("stageName") or "").strip()


def _forward_stage_path(
    context: dict[str, Any], requested_stage: str
) -> list[str]:
    """Etapas posteriores hasta el destino, conservando el orden de Parley."""
    stages = context.get("pipelineStages") or []
    if not isinstance(stages, list):
        return []
    ordered = [
        stage
        for stage in stages
        if isinstance(stage, dict) and str(stage.get("name") or "").strip()
    ]
    current = _current_stage_name(context).casefold()
    target = requested_stage.casefold()
    current_index = next(
        (
            index
            for index, stage in enumerate(ordered)
            if str(stage.get("name") or "").strip().casefold() == current
        ),
        -1,
    )
    target_index = next(
        (
            index
            for index, stage in enumerate(ordered)
            if str(stage.get("name") or "").strip().casefold() == target
        ),
        -1,
    )
    if current_index < 0 or target_index <= current_index + 1:
        return []
    return [
        str(stage.get("name") or "").strip()
        for stage in ordered[current_index + 1 : target_index + 1]
    ]


class ToolRuntime:
    """Ejecuta las tool-calls de UN turno y acumula sus efectos."""

    def __init__(
        self,
        ctx: AppContext,
        conv: Conversation,
        crm_conversation_id: str,
        profile: BusinessProfile | None = None,
        context: dict[str, Any] | None = None,
        user_text: str = "",
    ) -> None:
        self._ctx = ctx
        self._conv = conv
        self._crm_conv_id = crm_conversation_id
        self._profile = profile or BusinessProfile()
        self._context = context or {}
        self._user_text = user_text
        # Efectos observables por turn.py:
        self.handoff_reason: str | None = None  # se ejecuta DESPUÉS de la despedida
        self.booked = False
        self.routed_out = False
        self.proposed = False
        self._sent_media_ids: set[str] = set()

    async def execute(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        # No registra argumentos porque pueden contener datos personales. El
        # nombre de la herramienta basta para reconstruir por qué un turno no
        # movió el pipeline en producción.
        logger.info("tools: ejecutando %s", name)
        try:
            if name == "update_ficha":
                return await self._update_ficha(args)
            if name == "move_stage":
                return await self._move_stage(args)
            if name == "send_media":
                return await self._send_media(args)
            if name == "propose_slots":
                return await self._propose_slots()
            if name == "book_session":
                return await self._book_session(args)
            if name == "book_delivery":
                return await self._book_session(args, kind="delivery")
            if name == "reschedule_session":
                return await self._reschedule_session(args)
            if name == "route_out":
                return await self._route_out()
            if name == "handoff":
                return self._handoff(args)
            logger.warning("tools: herramienta desconocida %r", name)
            return {"ok": False, "error": f"herramienta desconocida: {name}"}
        except CrmError as exc:
            logger.warning("tools: %s falló contra el CRM: %s", name, exc)
            return {
                "ok": False,
                "error": "crm_error",
                "detalle": "no pude completar la acción; continúa la conversación o haz handoff",
            }

    async def _update_ficha(self, args: dict[str, Any]) -> dict[str, Any]:
        # Tolera el drift del LLM: manda lo que haya, el CRM normaliza flojo.
        ficha = {k: v for k, v in args.items() if v is not None}
        if not ficha:
            return {"ok": True, "nota": "sin campos nuevos"}
        await self._ctx.crm.put_ficha(self._crm_conv_id, ficha)
        return {"ok": True}

    async def _move_stage(self, args: dict[str, Any]) -> dict[str, Any]:
        stage = str(args.get("stage") or "").strip()
        if not stage:
            return {"ok": False, "error": "stage_required"}
        raw_evidence = args.get("evidence")
        if not isinstance(raw_evidence, list):
            return {"ok": False, "error": "evidence_required"}
        evidence = list(
            dict.fromkeys(
                str(item).strip()
                for item in raw_evidence
                if str(item).strip() in COMMERCIAL_EVIDENCE_SET
            )
        )
        if len(evidence) != len(raw_evidence):
            return {"ok": False, "error": "invalid_evidence"}

        local = _validate_pipeline_request(self._context, stage, evidence)
        if local is not None and local.get("error") == "stage_skip":
            return await self._advance_stage_path(stage, evidence)
        if local is not None:
            return local
        return await self._post_stage(stage, evidence)

    async def _advance_stage_path(
        self, requested_stage: str, evidence: list[str]
    ) -> dict[str, Any]:
        """Convierte un salto pedido por el LLM en transiciones consecutivas.

        Cada paso se valida localmente y vuelve a pasar por la autoridad de
        Parley. Nunca escribe dos columnas de una vez: si la evidencia alcanza
        solo hasta una etapa intermedia, se detiene allí y devuelve los datos
        que faltan para continuar.
        """
        path = _forward_stage_path(self._context, requested_stage)
        if not path:
            return {"ok": False, "error": "stage_skip"}

        moved = False
        for next_stage in path:
            local = _validate_pipeline_request(self._context, next_stage, evidence)
            if local is not None:
                if moved:
                    local = {
                        **local,
                        "stageMoved": True,
                        "stage": _current_stage_name(self._context),
                        "requestedStage": requested_stage,
                    }
                return local
            result = await self._post_stage(next_stage, evidence)
            if not result.get("ok"):
                return result
            moved = moved or bool(result.get("stageMoved"))

        return {
            "ok": True,
            "stageMoved": moved,
            "stage": _current_stage_name(self._context),
        }

    async def _post_stage(
        self, stage: str, evidence: list[str]
    ) -> dict[str, Any]:
        try:
            result = await self._ctx.crm.post_move_stage(
                self._crm_conv_id, stage, evidence
            )
        except CrmConflict as exc:
            error = exc.payload.get("error")
            detail = error if isinstance(error, dict) else {}
            return {
                "ok": False,
                "error": exc.code,
                "missingEvidence": detail.get("missingEvidence", []),
                "blockerCodes": detail.get("blockerCodes", []),
                "detalle": (
                    "movimiento rechazado; conserva la etapa actual y continúa "
                    "atendiendo al cliente"
                ),
            }
        resolved_stage = ((result.get("lead") or {}).get("stageName") or stage)
        lead = self._context.setdefault("lead", {})
        if isinstance(lead, dict):
            lead["stageName"] = resolved_stage
        logger.info("tools: pipeline avanzó a %s", resolved_stage)
        return {
            "ok": True,
            "stageMoved": bool(result.get("stageMoved")),
            "stage": resolved_stage,
        }

    async def _send_media(self, args: dict[str, Any]) -> dict[str, Any]:
        asset_id = str(args.get("asset_id") or "").strip()
        approved = next(
            (
                asset
                for asset in self._profile.media_assets
                if str(asset.get("id") or "") == asset_id
            ),
            None,
        )
        if approved is None:
            return {"ok": False, "error": "media_no_aprobado"}
        if asset_id in self._sent_media_ids:
            return {"ok": False, "error": "media_ya_enviado"}
        await self._ctx.crm.send_media_message(self._crm_conv_id, asset_id)
        self._sent_media_ids.add(asset_id)
        return {
            "ok": True,
            "assetId": asset_id,
            "kind": approved.get("kind"),
            "label": approved.get("label"),
        }

    async def _propose_slots(self) -> dict[str, Any]:
        # La conversación va SIEMPRE: es contra ella que el CRM registra la
        # oferta, y sin ella no hay nada reservable después.
        try:
            raw = await self._ctx.crm.get_availability(
                self._crm_conv_id,
                limit=MAX_OFFERED,
                per_day=OFFER_PER_DAY,
                days=OFFER_DAYS,
            )
        except AgendaUnavailable:
            return self._sin_agenda()
        slots = _slots_from_payload(self._conv.id, raw)
        if not slots:
            return {
                "ok": False,
                "error": "sin_disponibilidad",
                "detalle": "no hay horarios abiertos; ofrece handoff para coordinar directo",
            }
        await self._ctx.store.replace_offered_slots(self._conv.id, slots)
        self.proposed = True
        return {
            "ok": True,
            "slots": _slots_for_llm(slots),
            "dias_con_agenda": sorted(
                {s.label.rsplit(",", 1)[0].strip() for s in slots}
            ),
            "instrucciones": (
                "esta es TODA la agenda abierta: los días que no aparecen aquí "
                "NO tienen agenda, dilo en vez de mover al lead a otro día. "
                "Ofrécele máximo 3, con su etiqueta tal cual (día incluido), "
                "los que embonen con lo que pidió."
            ),
        }

    async def _resolve_offered(
        self, args: dict[str, Any], accion: str
    ) -> tuple[OfferedSlot | None, dict[str, Any] | None]:
        """Slot elegido, o el error listo para devolverle al LLM.

        Validación server-side por epoch exacto: solo lo ofrecido es reservable.
        """
        wanted = _parse_utc(str(args.get("start_utc") or ""))
        offered = await self._ctx.store.get_offered_slots(self._conv.id)
        if wanted is None:
            return None, {
                "ok": False,
                "error": "start_utc_invalido",
                "slots_ofrecidos": _slots_for_llm(offered),
            }
        chosen = next(
            (
                s
                for s in offered
                if int(s.start_utc.timestamp()) == int(wanted.timestamp())
            ),
            None,
        )
        if chosen is None:
            logger.info(
                "tools: %s rechazado — %s no está entre los ofrecidos",
                accion,
                args.get("start_utc"),
            )
            return None, {
                "ok": False,
                "error": "slot_no_ofrecido",
                "detalle": "solo puedes agendar un horario que ya ofreciste",
                "slots_ofrecidos": _slots_for_llm(offered),
            }
        # Deja rastro de sobre qué frase del lead se tomó la decisión: cuando
        # una cita sale mal, esto dice si hubo confirmación o se asumió.
        logger.info(
            "tools: %s a %s (el lead confirmó con: %r)",
            accion,
            chosen.label,
            str(args.get("dia_confirmado") or "")[:120],
        )
        return chosen, None

    def _sin_agenda(self) -> dict[str, Any]:
        """Este CRM no tiene agenda: dejar de prometer citas, no reintentar."""
        self._ctx.agenda_enabled = False
        logger.info("tools: el CRM no expone agenda — agendamiento desactivado")
        return {
            "ok": False,
            "error": "sin_agenda",
            "detalle": (
                "este negocio no tiene calendario activo; no ofrezcas horarios "
                "ni confirmes una reserva. Continúa recopilando el pedido y "
                "guarda la fecha solicitada; esto no justifica handoff"
            ),
        }

    async def _resync_offer(
        self, exc: SlotNotOffered, accion: str
    ) -> dict[str, Any]:
        """El CRM no reconoce ese horario: su lista manda, la nuestra se tira.

        Pasa cuando el espejo local quedó viejo — por ejemplo si el CRM
        reemplazó la oferta por su cuenta. Antes esto caía en el `except
        CrmError` genérico y el agente solo decía "no pude"; ahora vuelve a
        ofrecer lo que el CRM sí tiene registrado.
        """
        fresh = _slots_from_payload(self._conv.id, exc.slots)
        await self._ctx.store.replace_offered_slots(self._conv.id, fresh)
        logger.info(
            "tools: %s rechazado por el CRM (no ofrecido) — oferta resincronizada a %d",
            accion,
            len(fresh),
        )
        if not fresh:
            return {
                "ok": False,
                "error": "slot_no_ofrecido",
                "detalle": (
                    "el CRM no tiene horarios ofrecidos en esta conversación; "
                    "vuelve a llamar propose_slots antes de agendar"
                ),
            }
        return {
            "ok": False,
            "error": "slot_no_ofrecido",
            "detalle": (
                "ese horario ya no está ofrecido; ofrécele estos, que son los "
                "que el negocio tiene reservados para esta conversación"
            ),
            "slots": _slots_for_llm(fresh),
        }

    async def _book_session(
        self, args: dict[str, Any], kind: str = "session"
    ) -> dict[str, Any]:
        if kind == "delivery" and not _is_order_stage(self._context):
            return {
                "ok": False,
                "error": "order_stage_required",
                "detalle": (
                    "antes de reservar la entrega, solicita move_stage a Pedido "
                    "con toda la evidencia requerida"
                ),
            }
        chosen, error = await self._resolve_offered(args, "book_session")
        if error is not None or chosen is None:
            return error or {"ok": False, "error": "slot_no_ofrecido"}
        try:
            result = await self._ctx.crm.create_booking(
                self._crm_conv_id, _iso_z(chosen.start_utc), kind=kind
            )
        except SlotTaken as exc:
            # El slot se ocupó entre oferta y elección: alternativas frescas.
            fresh = _slots_from_payload(self._conv.id, exc.slots)
            await self._ctx.store.replace_offered_slots(self._conv.id, fresh)
            return {
                "ok": False,
                "error": "slot_taken",
                "detalle": "ese horario se acaba de ocupar; discúlpate breve y ofrece estas alternativas",
                "slots": _slots_for_llm(fresh),
            }
        except SlotNotOffered as exc:
            return await self._resync_offer(exc, "book_session")
        except AgendaUnavailable:
            return self._sin_agenda()
        await self._ctx.store.clear_offered_slots(self._conv.id)
        self.booked = True
        try:
            await self._ctx.crm.put_ficha(
                self._crm_conv_id, {"calificado": True, "resultado": "agendo"}
            )
        except CrmError as exc:  # best-effort: la cita ya existe
            logger.warning("tools: no pude actualizar ficha tras booking: %s", exc)
        return {
            "ok": True,
            # La etiqueta del slot ofrecido trae el día en palabras; la del
            # CRM es la corta. Se repite ESTA para que el lead lea el día.
            "label": chosen.label or result.get("label"),
            "meeting_url": _meeting(result)[0],
            "enlace_pendiente": _meeting(result)[1],
            "instrucciones": (
                "confirma la ENTREGA con día completo y hora; no menciones "
                "reunión ni enlace"
                if kind == "delivery"
                else (
                    "confirma el día COMPLETO y la hora tal cual dice label, "
                    "comparte meeting_url si viene y menciona lo que el negocio "
                    "pida para llegar preparado. Si enlace_pendiente es true, la "
                    "cita SÍ quedó: di que el enlace le llega por aquí en un "
                    "momento, no prometas uno que no tienes"
                )
            ),
        }

    async def _reschedule_session(self, args: dict[str, Any]) -> dict[str, Any]:
        chosen, error = await self._resolve_offered(args, "reschedule_session")
        if error is not None or chosen is None:
            return error or {"ok": False, "error": "slot_no_ofrecido"}
        try:
            result = await self._ctx.crm.reschedule_booking(
                self._crm_conv_id, _iso_z(chosen.start_utc)
            )
        except SlotTaken as exc:
            fresh = _slots_from_payload(self._conv.id, exc.slots)
            await self._ctx.store.replace_offered_slots(self._conv.id, fresh)
            return {
                "ok": False,
                "error": "slot_taken",
                "detalle": "ese horario se acaba de ocupar; discúlpate breve y ofrece estas alternativas",
                "slots": _slots_for_llm(fresh),
            }
        except SlotNotOffered as exc:
            return await self._resync_offer(exc, "reschedule_session")
        except AgendaUnavailable:
            return self._sin_agenda()
        except CrmConflict as exc:
            if exc.code == "no_booking":
                return {
                    "ok": False,
                    "error": "sin_cita",
                    "detalle": "el lead no tiene cita por delante; usa book_session",
                }
            raise
        await self._ctx.store.clear_offered_slots(self._conv.id)
        self.booked = True
        return {
            "ok": True,
            "label": chosen.label or result.get("label"),
            "meeting_url": _meeting(result)[0],
            "enlace_pendiente": _meeting(result)[1],
            "instrucciones": (
                "confirma que quedó movida, con el día COMPLETO y la hora tal "
                "cual dice label; el link de la videollamada sigue siendo el "
                "mismo salvo que aquí venga otro"
            ),
        }

    async def _route_out(self) -> dict[str, Any]:
        # "dio_diy" es el valor del enum `resultado` en el gateway del CRM
        # (006); el nombre de la herramienta es genérico, el cable no cambia.
        await self._ctx.crm.put_ficha(
            self._crm_conv_id, {"calificado": False, "resultado": "dio_diy"}
        )
        self.routed_out = True
        out: dict[str, Any] = {"ok": True}
        if self._profile.resources:
            out["recursos"] = self._profile.resources
            out["instrucciones"] = "comparte estos recursos al despedirte, puerta abierta"
        return out

    def _handoff(self, args: dict[str, Any]) -> dict[str, Any]:
        reason = str(args.get("reason") or "").strip()
        canonical = canonical_handoff_reason(reason)
        # Una intención de compra o la falta de calendario no equivalen a que
        # el cliente haya pedido una persona. Este guardarraíl es determinista:
        # incluso si una regla libre del perfil induce al modelo a escalar, el
        # pase de tipo `cliente` requiere palabras explícitas del propio lead.
        if (
            canonical == "cliente" or _purchase_only_handoff_reason(reason)
        ) and not _explicit_human_request(self._user_text):
            return {
                "ok": False,
                "error": "handoff_not_justified",
                "detalle": (
                    "el cliente no pidió una persona; continúa el pedido, "
                    "guarda los datos y solicita el siguiente faltante"
                ),
            }
        self.handoff_reason = canonical
        return {
            "ok": True,
            "nota": (
                "el pase a humano se ejecutará después de tu mensaje de despedida"
            ),
        }


def _explicit_human_request(text: str) -> bool:
    normalized = " ".join(text.casefold().split())
    people = ("humano", "persona", "asesor", "vendedor", "ejecutivo", "alguien")
    actions = ("hablar", "comunicar", "pasar", "contactar", "atender")
    return any(person in normalized for person in people) and (
        any(action in normalized for action in actions)
        or any(phrase in normalized for phrase in ("quiero un", "quiero una", "con un", "con una"))
    )


def _purchase_only_handoff_reason(reason: str) -> bool:
    """Detecta motivos comerciales que el modelo no puede convertir en pase.

    Los motivos de la herramienta son texto libre y su normalizador histórico
    clasifica cualquier valor desconocido como ``modelo``. Por eso la barrera
    no puede depender únicamente del código canónico: debe reconocer también
    las formulaciones habituales de una compra o coordinación normal.
    """
    normalized = " ".join(reason.casefold().split())
    commercial_terms = (
        "avanzar",
        "comprar",
        "compra",
        "pedido",
        "confirmar",
        "coordinar",
        "agenda",
        "calendario",
        "entrega",
        "despacho",
    )
    return any(term in normalized for term in commercial_terms)


def _is_order_stage(context: dict[str, Any]) -> bool:
    lead_name = str((context.get("lead") or {}).get("stageName") or "").strip()
    stages = context.get("pipelineStages") or []
    for stage in stages if isinstance(stages, list) else []:
        if not isinstance(stage, dict):
            continue
        if str(stage.get("name") or "").strip().casefold() != lead_name.casefold():
            continue
        key = str(stage.get("botStageKey") or "").strip().casefold()
        if key:
            return key == "order"
    return lead_name.casefold() in {"pedido", "order"}
