"""Mapper: applica mapping_json + trasformazioni a un record raw.

Formato mapping:
    {
      "src_col_name": "target_col_name",                     # passthrough
      "src_col_name": {"target": "target_col", "transform": "lowercase"},
      "src_col_name": {"target": "target_col",
                       "transform": "split:," "default": "..."}
    }

Trasformazioni supportate:
    lowercase            -> str(value).lower()
    strip                -> str(value).strip()
    default:<v>          -> value if not empty else <v>
    split:<sep>          -> value.split(sep) -> list[str]
    bool                 -> coerce a bool (1/true/yes/si/on -> True)
    int                  -> int(value)
    json_parse           -> json.loads(value)
    coalesce:<col1,col2> -> primo non-vuoto tra value, raw[col1], raw[col2]

Il sentinel mapping `{"_legacy": true}` viene gestito dal runner: bypass
totale del mapper, il provider ritorna gia' record canonici.

Target canonici supportati (allineati a tabella `customers`):
    codcli, ragione_sociale, contract_active, tipologia_servizio,
    contract_type, contract_expiry, domains, aliases, timezone,
    service_hours_json, raw_json
"""
from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

CANONICAL_TARGETS = (
    "codcli",
    "ragione_sociale",
    "contract_active",
    "tipologia_servizio",
    "contract_type",
    "contract_expiry",
    "domains",
    "aliases",
    "timezone",
    "service_hours_json",
    "raw_json",
)

# Metadata dei campi canonici, esposti alla UI mapping editor.
# Per ogni campo:
#   description = cosa rappresenta nel funzionamento del sistema
#   type        = tipo Python atteso (dopo l'eventuale transform)
#   required    = se l'upsert fallisce senza questo
#   used_by     = dove il dato viene letto a runtime (per chi non conosce)
#   hint        = consiglio operativo (transform tipici, JOIN, ecc.)
CANONICAL_TARGETS_INFO: dict[str, dict[str, str | bool]] = {
    "codcli": {
        "type": "string (UPPERCASE)",
        "required": True,
        "description": "Codice cliente — chiave primaria. Match per upsert.",
        "used_by": "Rule engine (match_known_customer), pipeline.resolve_by_email, "
                   "API /api/v1/relay/customers/active, customer_groups.",
        "hint": "Sempre obbligatorio. Sara' normalizzato a UPPERCASE.",
    },
    "ragione_sociale": {
        "type": "string",
        "required": False,
        "description": "Denominazione legale del cliente.",
        "used_by": "UI clienti, audit eventi, payload ticket.",
        "hint": "Opzionale ma fortemente consigliato per audit leggibile.",
    },
    "contract_active": {
        "type": "bool",
        "required": False,
        "description": "Contratto attivo? Tristate match nel rule engine.",
        "used_by": "Rule engine match_contract_active, regola template no-contract, "
                   "synthetic groups all_contract / no_contract.",
        "hint": "Default 1 se omesso. Per CSV/JSON usa transform 'bool' "
                "(accetta 1/0/true/false/si/no/attivo/inattivo).",
    },
    "tipologia_servizio": {
        "type": "string",
        "required": False,
        "description": "Codice profilo orari (STD/EXT/H24/NO o profilo custom).",
        "used_by": "Rule engine match_in_service, payload listener.",
        "hint": "Default 'standard' se omesso. Esempio: STD, EXT, H24.",
    },
    "contract_type": {
        "type": "string",
        "required": False,
        "description": "Tipo contratto (es. 'Full Service - Gestione Completa').",
        "used_by": "UI clienti, audit, customer_groups virtuali per tipo.",
        "hint": "Sara' visibile in /customers come colonna 'TIPO CONTR.'",
    },
    "contract_expiry": {
        "type": "string (ISO date)",
        "required": False,
        "description": "Data scadenza contratto.",
        "used_by": "UI audit (oggi non e' usato dal rule engine).",
        "hint": "Formato 'YYYY-MM-DD'. Vuoto se senza scadenza.",
    },
    "domains": {
        "type": "list[string]",
        "required": False,
        "description": "Domini email del cliente (per resolve from/to).",
        "used_by": "Pipeline.find_customer_by_domain, routing forward/redirect, "
                   "rule engine match_from_domain / match_to_domain.",
        "hint": "Per fonti con tabella separata (es. PG client_domains): "
                "nella query SQL usa STRING_AGG(d.dominio, ',') GROUP BY codcli "
                "e poi mapping con transform 'split:,'. Per CSV con piu' domini "
                "in colonna unica: stesso transform.",
    },
    "aliases": {
        "type": "list[string]",
        "required": False,
        "description": "Indirizzi email che identificano il cliente come mittente.",
        "used_by": "Pipeline.find_customer_by_alias, customer resolve da from_email "
                   "in rule engine.",
        "hint": "Per fonti con tabella separata aliases: "
                "STRING_AGG(a.alias_name, ',') GROUP BY codcli + transform 'split:,'. "
                "I valori saranno normalizzati a lowercase.",
    },
    "timezone": {
        "type": "string (IANA tz)",
        "required": False,
        "description": "Fuso orario per calcolo in_service / has_exception_today.",
        "used_by": "Pipeline.is_in_service, match_has_exception_today.",
        "hint": "Default 'Europe/Rome'. Esempi: 'Europe/Rome', 'UTC', 'America/New_York'.",
    },
    "service_hours_json": {
        "type": "object {profile, timezone, schedule, holidays}",
        "required": False,
        "description": "Profilo orari + override + festivita' del cliente.",
        "used_by": "Rule engine match_in_service, match_has_exception_today, "
                   "calcolo finestre H24 e service hours.",
        "hint": "Per fonti relazionali: aggrega via json_build_object o passa "
                "JSON serializzato e usa transform 'json_parse'. Per CSV "
                "questo campo va lasciato vuoto.",
    },
    "raw_json": {
        "type": "object (audit)",
        "required": False,
        "description": "Snapshot completo del record sorgente per audit/debug.",
        "used_by": "Solo audit (non letto a runtime).",
        "hint": "Opzionale. Se popolato, viene salvato cosi' com'e'. Utile "
                "per debugging differenze tra schema sorgente e canonico.",
    },
}

TRUTHY = {"1", "true", "t", "yes", "y", "si", "sì", "on", "vero", "attivo"}
FALSY = {"0", "false", "f", "no", "n", "off", "falso", "inattivo"}


def is_legacy_mapping(mapping: dict[str, Any] | None) -> bool:
    return bool(mapping and mapping.get("_legacy"))


def apply(raw: dict[str, Any], mapping: dict[str, Any]) -> dict[str, Any]:
    """Applica mapping a un record raw, ritorna dict canonico.

    Se la chiave canonica non e' valorizzata dal mapping, viene omessa
    (cosi' upsert_customer_record la lascia intatta su update).
    """
    if not mapping:
        # Senza mapping, copia chiavi canoniche presenti per nome
        return {k: raw[k] for k in CANONICAL_TARGETS if k in raw}

    if is_legacy_mapping(mapping):
        # Il provider ritorna gia' record canonici
        return dict(raw)

    out: dict[str, Any] = {}
    for src_key, spec in mapping.items():
        if src_key.startswith("_"):
            continue  # sentinel/meta key
        value = raw.get(src_key)

        if isinstance(spec, str):
            target = spec
            transform = None
            default = None
        elif isinstance(spec, dict):
            target = spec.get("target")
            transform = spec.get("transform")
            default = spec.get("default")
        else:
            logger.warning("mapper: spec non valida per %r: %r", src_key, spec)
            continue

        if not target:
            continue

        try:
            value = _apply_transform(value, transform, raw=raw)
        except Exception as exc:  # noqa: BLE001
            logger.warning("mapper: transform '%s' fallita su %r: %s",
                           transform, src_key, exc)
            continue

        if (value is None or value == "" or value == []) and default is not None:
            value = default

        out[target] = value

    return out


def _apply_transform(value: Any, transform: str | None, *,
                     raw: dict[str, Any]) -> Any:
    if not transform:
        return value
    t = transform.strip()

    if t == "lowercase":
        return str(value).lower() if value is not None else None
    if t == "uppercase":
        return str(value).upper() if value is not None else None
    if t == "strip":
        return str(value).strip() if value is not None else None
    if t == "bool":
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        s = str(value).strip().lower()
        if s in TRUTHY:
            return True
        if s in FALSY:
            return False
        return bool(s)
    if t == "int":
        if value is None or value == "":
            return None
        return int(value)
    if t == "json_parse":
        if value is None or value == "":
            return None
        if isinstance(value, (dict, list)):
            return value
        return json.loads(value)
    if t.startswith("default:"):
        d = t.split(":", 1)[1]
        if value is None or value == "" or value == []:
            return d
        return value
    if t.startswith("split:"):
        sep = t.split(":", 1)[1] or ","
        if value is None or value == "":
            return []
        if isinstance(value, list):
            return [str(v).strip() for v in value if v]
        return [s.strip() for s in str(value).split(sep) if s.strip()]
    if t.startswith("coalesce:"):
        cols = [c.strip() for c in t.split(":", 1)[1].split(",") if c.strip()]
        if value is not None and value != "":
            return value
        for c in cols:
            v = raw.get(c)
            if v is not None and v != "":
                return v
        return None

    raise ValueError(f"Trasformazione non supportata: {transform!r}")
