"""Preset di regole per il wizard `/rules/wizard`.

Organizzati secondo la struttura logica del sistema (dall'alto verso il basso
nella priorita' di valutazione):

  1. BYPASS         - privacy, kill switch (gestiti fuori dal rule engine)
  2. CODICI H24     - autorizzazione tramite codici permanenti/monouso
  3. AI             - classificazione e apprendimento via Claude
  4. CLIENTE        - regole specifiche per cliente / gruppo / contratto
                      (correlazione con domini cliente + orari di servizio)
  5. DESTINATARIO   - regole basate su destinatario / gruppo destinatari
                      (instradamento sostituto, mailbox no-fuori-orario)
  6. ORARIO         - regole basate sulla finestra temporale del cliente
                      (inoltro, risposta, ticket in/fuori orario)
  7. GLOBALE        - catch-all per servizi non gestiti specificamente

Ogni preset descrive un caso d'uso tipico con campi pre-compilati.
"""
from __future__ import annotations

from typing import Any


CATEGORIES = [
    {
        "id": "bypass",
        "title": "Bypass e priorita' assolute",
        "icon": "fa-shield",
        "color": "#0f172a",
        "description": "Regole sopra a tutto: privacy bypass, kill switch, "
                       "passthrough. Gestite fuori dal rule engine standard "
                       "ma documentate qui per completezza.",
    },
    {
        "id": "codici_h24",
        "title": "Codici di autorizzazione H24",
        "icon": "fa-key",
        "color": "#b91c1c",
        "description": "Apertura ticket urgenti con codice valido nel subject "
                       "(monouso o permanente). Sempre attive a priorita' alta.",
    },
    {
        "id": "ai",
        "title": "AI / Apprendimento",
        "icon": "fa-robot",
        "color": "#a855f7",
        "description": "Classificazione semantica via Claude. L'AI propone "
                       "intent / urgenza / azione finale. Modalita' shadow o live.",
    },
    {
        "id": "cliente",
        "title": "Per cliente o gruppo di clienti",
        "icon": "fa-building",
        "color": "#15803d",
        "description": "Regole che dipendono dal cliente specifico (codcli, "
                       "gruppo cliente) o dal contratto. Per filtrare per "
                       "tipologia di contratto (STD/EXT/H24) usa direttamente "
                       "il set di regole; per OVERRIDE custom (es. cliente STD "
                       "trattato come H24 per accordo informale) usa il set "
                       "'globali' + un gruppo cliente dedicato.",
    },
    {
        "id": "destinatario",
        "title": "Per destinatario o gruppo destinatari",
        "icon": "fa-people-group",
        "color": "#a16207",
        "description": "Regole basate sulla mailbox di arrivo: tecnici "
                       "no-fuori-orario, dottori reperibili, instradamento "
                       "a sostituti.",
    },
    {
        "id": "orario",
        "title": "Per orario del giorno",
        "icon": "fa-clock",
        "color": "#1e40af",
        "description": "Regole basate sulla finestra operativa del cliente: "
                       "auto-reply in/fuori orario, ticket diversi per "
                       "fascia. La gerarchia STD ⊂ EXT ⊂ H24 e' rispettata "
                       "automaticamente.",
    },
    {
        "id": "globale",
        "title": "Globali / servizi non gestiti",
        "icon": "fa-globe",
        "color": "#64748b",
        "description": "Catch-all e fallback per mail che non rientrano in "
                       "casi specifici. Default delivery, scarto, log.",
    },
]


# Ogni preset ha:
#   id, category, title, description, icon, color
#   needs       - lista di campi che il wizard deve mostrare al step 2
#   defaults    - valori pre-compilati per la regola finale
PRESETS: list[dict[str, Any]] = [

    # =================================================================
    # CATEGORIA 2: CODICI H24
    # =================================================================
    {
        "id": "h24_codice",
        "category": "codici_h24",
        "title": "Apertura ticket H24 con codice valido nel subject",
        "description": "Apre un ticket URGENTE solo se il subject contiene un "
                       "codice di autorizzazione (AUTH-XXXX, H24-XXXX, "
                       "DOMARC-XXXX). Riconosce il codice via regex, lo valida "
                       "su DB (monouso o permanente), apre ticket o reject.",
        "icon": "fa-key",
        "color": "#b91c1c",
        "needs": ["to_regex_optional", "ack_template", "reject_template"],
        "defaults": {
            "rule_set_code": "globali",
            "match_subject_regex": r"\b(AUTH-[A-Z0-9]{6,}|H24-[A-Z0-9]{4,}|DOMARC-[A-Z0-9]+)\b",
            "action": "create_authorized_ticket",
            "priority_hint": 1,
            "action_map": {"settore": "S", "urgenza": "URGENTE"},
        },
    },
    {
        "id": "h24_inbound_reject",
        "category": "codici_h24",
        "title": "Reject mail su mailbox H24 senza codice valido",
        "description": "Se una mail arriva sulla mailbox di rientro H24 (es. "
                       "h24@datia.it) MA non ha un codice valido nel subject, "
                       "risponde al mittente con template 'reject' e non apre "
                       "ticket. Si combina con il preset 'h24_codice' a "
                       "priorita' inferiore.",
        "icon": "fa-ban",
        "color": "#dc2626",
        "needs": ["to_regex", "reject_template"],
        "defaults": {
            "rule_set_code": "globali",
            "match_to_regex": r"^h24@(datia|domarc)\.it$",
            "action": "auto_reply",
            "priority_hint": 50,
        },
    },

    # =================================================================
    # CATEGORIA 3: AI
    # =================================================================
    {
        "id": "ai_classify",
        "category": "ai",
        "title": "Classificazione AI generica (Claude)",
        "description": "Passa la mail all'AI Claude per classificarla "
                       "(intent / urgenza / summary / azione suggerita). "
                       "L'azione finale viene proposta dall'AI. In shadow "
                       "mode la decisione e' solo loggata per training.",
        "icon": "fa-robot",
        "color": "#a855f7",
        "needs": ["from_regex_optional", "to_regex_optional",
                  "subject_regex_optional"],
        "defaults": {
            "rule_set_code": "globali",
            "action": "ai_classify",
            "priority_hint": 100,
        },
    },
    {
        "id": "ai_critical",
        "category": "ai",
        "title": "AI critical check (per decisioni delicate)",
        "description": "Versione potenziata che usa Claude Sonnet per casi "
                       "particolari. Costa di piu' ma e' piu' affidabile su "
                       "casi ambigui.",
        "icon": "fa-brain",
        "color": "#7c3aed",
        "needs": ["from_regex_optional", "subject_regex_optional"],
        "defaults": {
            "rule_set_code": "globali",
            "action": "ai_critical_check",
            "priority_hint": 90,
        },
    },

    # =================================================================
    # CATEGORIA 4: CLIENTE / CONTRATTO
    # =================================================================
    {
        "id": "cliente_specifico_h24",
        "category": "cliente",
        "title": "Trattamento dedicato per clienti H24",
        "description": "Regola attiva SOLO per clienti con contratto H24 "
                       "(servizio 24/7). Tipico: escalation immediata, "
                       "tariffe straordinario, integrazione tecnico "
                       "reperibile, urgenza ALTA di default.",
        "icon": "fa-user-shield",
        "color": "#dc2626",
        "needs": ["from_regex_optional", "subject_regex_optional",
                  "settore", "urgenza", "action_choice"],
        "defaults": {
            "rule_set_code": "h24",
            "action": "create_ticket",
            "priority_hint": 200,
            "action_map": {"urgenza": "ALTA", "settore": "assistenza_h24"},
        },
    },
    {
        "id": "cliente_specifico_std",
        "category": "cliente",
        "title": "Trattamento dedicato per clienti Standard (STD)",
        "description": "Regola attiva SOLO per clienti con contratto Standard "
                       "(STD: lun-ven 08:30-13:00 + 14:30-17:30). Tipico: "
                       "tariffe specifiche, template dedicato, blocco fuori "
                       "orario specifico.",
        "icon": "fa-building",
        "color": "#15803d",
        "needs": ["from_regex_optional", "subject_regex_optional",
                  "in_service_choice", "action_choice", "settore", "urgenza"],
        "defaults": {
            "rule_set_code": "standard",
            "action": "create_ticket",
            "priority_hint": 200,
            "action_map": {"urgenza": "NORMALE", "settore": "assistenza"},
        },
    },
    {
        "id": "cliente_specifico_ext",
        "category": "cliente",
        "title": "Trattamento dedicato per clienti Esteso (EXT)",
        "description": "Regola attiva SOLO per clienti EXT (lun-ven "
                       "06:30-19:30 + sab mattina). Tipico: routing dedicato, "
                       "priorita' diversa.",
        "icon": "fa-business-time",
        "color": "#a16207",
        "needs": ["from_regex_optional", "subject_regex_optional",
                  "in_service_choice", "action_choice", "settore", "urgenza"],
        "defaults": {
            "rule_set_code": "esteso",
            "action": "create_ticket",
            "priority_hint": 200,
            "action_map": {"urgenza": "NORMALE", "settore": "assistenza"},
        },
    },

    # =================================================================
    # CATEGORIA 5: DESTINATARIO
    # =================================================================
    {
        "id": "destinatari_no_fo_sostituto",
        "category": "destinatario",
        "title": "Tecnici/Medici no-fuori-orario → reindirizza a sostituto",
        "description": "Per certi destinatari (tecnici, medici, reperibili), "
                       "fuori dalla finestra del cliente la mail viene "
                       "inoltrata a un indirizzo sostitutivo. Opzionale: CC "
                       "al destinatario originale + auto-reply al mittente.",
        "icon": "fa-shuffle",
        "color": "#a16207",
        "needs": ["recipient_group", "forward_target", "from_regex_optional",
                  "keep_original", "template_optional"],
        "defaults": {
            "rule_set_code": "globali",
            "match_in_service": False,
            "action": "forward",
            "priority_hint": 220,
            "action_map": {"keep_original_delivery": True},
        },
    },
    {
        "id": "destinatari_dedicato",
        "category": "destinatario",
        "title": "Mailbox dedicata → ticket / template specifico",
        "description": "Mail che arriva a una mailbox o gruppo specifico (es. "
                       "supporto@cliente.it, vendite@) viene processata in "
                       "modo dedicato (ticket settoriale, auto-reply "
                       "personalizzato).",
        "icon": "fa-envelope",
        "color": "#16a34a",
        "needs": ["recipient_group_or_to_regex", "action_choice",
                  "settore", "urgenza", "template_optional"],
        "defaults": {
            "rule_set_code": "globali",
            "action": "create_ticket",
            "priority_hint": 150,
            "action_map": {"urgenza": "NORMALE"},
        },
    },

    # =================================================================
    # CATEGORIA 6: ORARIO
    # =================================================================
    {
        "id": "auto_reply_in_orario",
        "category": "orario",
        "title": "Auto-reply in orario lavorativo",
        "description": "Risponde con un template solo quando il cliente e' "
                       "DENTRO la sua finestra operativa. Funziona per tutti "
                       "i contratti (STD/EXT/H24) perche' usa match_in_service "
                       "che legge il profilo del singolo cliente.",
        "icon": "fa-sun",
        "color": "#1e40af",
        "needs": ["template", "from_regex_optional", "to_regex_optional"],
        "defaults": {
            "rule_set_code": "globali",
            "match_in_service": True,
            "action": "auto_reply",
            "priority_hint": 200,
        },
    },
    {
        "id": "auto_reply_fuori_orario",
        "category": "orario",
        "title": "Auto-reply fuori orario lavorativo",
        "description": "Risponde con un template solo quando il cliente e' "
                       "FUORI dalla finestra operativa. I clienti H24 (sempre "
                       "in servizio) sono naturalmente esclusi.",
        "icon": "fa-moon",
        "color": "#1e40af",
        "needs": ["template", "from_regex_optional", "to_regex_optional"],
        "defaults": {
            "rule_set_code": "globali",
            "match_in_service": False,
            "action": "auto_reply",
            "priority_hint": 250,
        },
    },
    {
        "id": "ticket_fuori_orario",
        "category": "orario",
        "title": "Apertura ticket per mail fuori orario",
        "description": "Apre un ticket (urgenza configurabile) per mail "
                       "ricevute fuori dalla finestra del cliente. Tipico per "
                       "registrazione mancato presidio.",
        "icon": "fa-ticket",
        "color": "#16a34a",
        "needs": ["from_regex_optional", "to_regex_optional",
                  "settore", "urgenza"],
        "defaults": {
            "rule_set_code": "globali",
            "match_in_service": False,
            "action": "create_ticket",
            "priority_hint": 240,
            "action_map": {"urgenza": "NORMALE", "settore": "assistenza"},
        },
    },

    # =================================================================
    # CATEGORIA 7: GLOBALE / SERVIZI NON GESTITI
    # =================================================================
    {
        "id": "alert_automatico",
        "category": "globale",
        "title": "Alert da sistema automatico (CloudTIK, monitoring, ...)",
        "description": "Riconosce mail da sistemi di monitoring (no-reply, "
                       "sensori, ecc.) e apre ticket diretto al settore "
                       "tecnico bypassando il triage manuale.",
        "icon": "fa-bolt",
        "color": "#7c3aed",
        "needs": ["from_regex", "subject_regex_optional", "settore", "urgenza"],
        "defaults": {
            "rule_set_code": "globali",
            "match_from_regex": r"(?i)^no-?reply@",
            "action": "create_ticket",
            "priority_hint": 60,
            "action_map": {"settore": "T", "urgenza": "NORMALE"},
        },
    },
    {
        "id": "quarantena",
        "category": "globale",
        "title": "Quarantena per revisione manuale",
        "description": "Sposta la mail in quarantena (non viene consegnata). "
                       "Un operatore decidera' poi se rilasciarla o scartarla. "
                       "Tipico per mittenti sospetti, contenuti non riconosciuti.",
        "icon": "fa-shield-virus",
        "color": "#dc2626",
        "needs": ["from_regex_optional", "subject_regex_optional", "reason"],
        "defaults": {
            "rule_set_code": "globali",
            "action": "quarantine",
            "priority_hint": 80,
        },
    },
    {
        "id": "catch_all",
        "category": "globale",
        "title": "Catch-all default delivery",
        "description": "Fallback finale: ogni mail che non ha matchato regole "
                       "specifiche viene comunque consegnata al destinatario "
                       "originale via smarthost. Da mettere a priorita' alta "
                       "(999).",
        "icon": "fa-arrow-right",
        "color": "#64748b",
        "needs": [],
        "defaults": {
            "rule_set_code": "globali",
            "match_to_regex": r".*",
            "action": "default_delivery",
            "priority_hint": 999,
        },
    },
]


def get_preset(preset_id: str) -> dict[str, Any] | None:
    return next((p for p in PRESETS if p["id"] == preset_id), None)


def presets_by_category() -> dict[str, list[dict[str, Any]]]:
    """Raggruppa preset per categoria, mantenendo l'ordine di CATEGORIES."""
    out: dict[str, list[dict[str, Any]]] = {c["id"]: [] for c in CATEGORIES}
    for p in PRESETS:
        cat = p.get("category", "globale")
        if cat in out:
            out[cat].append(p)
    return out
