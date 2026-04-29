"""Error aggregator IA: dedup semantica errori → cluster + soglia + ticket aggregato.

Sostituisce il modello rigido di ``error_aggregations`` (count+window basati su
regex). Qui clusterizziamo i messaggi di errore per **similarità del fingerprint**:

- F2 base (sempre attivo): fingerprint deterministico = (subject normalizzato +
  first/last 30 char del body redatto). Cattura varianti tipo
  "backup failed on srv01" / "backup failed on srv02" come stesso cluster.
- F4+ (quando ``sentence-transformers`` è installato): fingerprint = embedding
  vettoriale 384-dim, cluster via cosine similarity > 0.85. Più potente ma
  richiede ~120MB di dipendenze.

Recovery:
- Subject contiene parole tipo "ok"/"recovered"/"resolved"/"cleared" + match
  su cluster esistente → cluster passa in state='recovered' e l'eventuale
  ticket viene chiuso/segnalato.

Soglia manuale per cluster:
- ``manual_threshold`` (default 5): n. eventi prima di aprire ticket aggregato.
- ``manual_recovery_window_min`` (default 60): finestra temporale fail→recovery
  entro cui un recovery automatico chiude il cluster.

Worker async:
- Invocato da scheduler/cron interno periodicamente (in F2 base lo chiamiamo
  on-demand dalla UI; in produzione si farà partire un thread).
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..storage.base import Storage

logger = logging.getLogger(__name__)

# Pattern di error indicators (subject)
_ERROR_KEYWORDS_RE = re.compile(
    r"(?i)\b(error|errore|failed|failure|critical|critico|alert|fault|"
    r"down|crash|exception|timeout|fatal|severe)\b",
)

# Pattern di recovery indicators
_RECOVERY_KEYWORDS_RE = re.compile(
    r"(?i)\b(ok|resolved|risolto|recovered|recovery|cleared|fixed|"
    r"backup\s+ok|service\s+restored|tutto\s+ok|back\s+online|up)\b",
)

# Tag generici di severity/log level che non distinguono semanticamente la natura
# del problema (es. [INFO]/[ALERT]/[WARNING] sono solo etichette di livello).
# Strippati prima del fingerprint per accomunare error/recovery dello stesso evento.
_LOG_LEVEL_KEYWORDS_RE = re.compile(
    r"(?i)\b(info|warn|warning|notice|debug|trace)\b",
)


def _normalize_subject(subject: str) -> str:
    """Normalizza il subject per fingerprint:
    - lowercase
    - rimuovi keyword error/recovery (così failed/recovered/ok mappano allo
      stesso cluster quando il resto del subject è simile)
    - rimuovi numeri / hostname / IP / timestamp
    - rimuovi punteggiatura (lascia spazi)
    - collapse whitespace.
    """
    s = (subject or "").lower()
    # Rimuovi tag log-level (info/warning/...) + keyword error/recovery PRIMA
    # della normalizzazione → cluster stabile fra failed/recovered/info/alert.
    s = _LOG_LEVEL_KEYWORDS_RE.sub(" ", s)
    s = _ERROR_KEYWORDS_RE.sub(" ", s)
    s = _RECOVERY_KEYWORDS_RE.sub(" ", s)
    s = re.sub(r"\b\d+\.\d+\.\d+\.\d+\b", "<ip>", s)         # IP
    s = re.sub(r"\b[a-z0-9-]+\d+(\.[a-z]{2,})?\b", "<host>", s)  # srv01, host-prod-12, ecc.
    s = re.sub(r"\b\d{1,4}([:/-]\d{1,4})+\b", "<time>", s)   # timestamp/dates
    s = re.sub(r"\d+", "<n>", s)                              # numeri rimanenti
    s = re.sub(r"[^\w\s<>]", " ", s)                          # punteggiatura
    s = re.sub(r"\s+", " ", s).strip()
    return s


def compute_fingerprint(subject: str, body_excerpt: str = "") -> str:
    """Fingerprint deterministico di un evento errore.

    Returns:
        Hash hex stabile (32 char) basato su subject normalizzato + extra.
    """
    norm_subject = _normalize_subject(subject)
    body_signature = (body_excerpt or "").strip()[:60].lower()
    body_signature = re.sub(r"\d+", "<n>", body_signature)
    fingerprint_input = f"{norm_subject}||{body_signature}"
    return hashlib.sha256(fingerprint_input.encode("utf-8")).hexdigest()[:32]


def is_error_event(subject: str | None, body: str | None) -> bool:
    """True se subject/body contengono error indicators."""
    text = f"{subject or ''} {body or ''}"
    return bool(_ERROR_KEYWORDS_RE.search(text))


def is_recovery_event(subject: str | None, body: str | None) -> bool:
    """True se subject/body contengono recovery indicators."""
    text = f"{subject or ''} {body or ''}"
    return bool(_RECOVERY_KEYWORDS_RE.search(text))


def process_event_for_clustering(
    *,
    storage: "Storage",
    tenant_id: int,
    event_uuid: str | None,
    subject: str | None,
    body_excerpt: str | None,
) -> dict | None:
    """Processa un evento per il clustering errori/recovery.

    Returns:
        dict ``{action, cluster_id, count, ...}`` o None se l'evento non è
        rilevante (no error/recovery indicators).
    """
    if not subject and not body_excerpt:
        return None
    is_err = is_error_event(subject, body_excerpt)
    is_rec = is_recovery_event(subject, body_excerpt)
    if not is_err and not is_rec:
        return None

    fp = compute_fingerprint(subject or "", body_excerpt or "")
    existing = _find_cluster_by_fingerprint(storage, tenant_id=tenant_id, fp=fp)

    if is_rec and existing:
        # Recovery sul cluster esistente
        return _mark_recovery(storage, existing, subject=subject)

    if is_err:
        if existing:
            return _increment_cluster(storage, existing, subject=subject,
                                       body_excerpt=body_excerpt)
        return _create_cluster(storage, tenant_id=tenant_id, fp=fp,
                                subject=subject, body_excerpt=body_excerpt)
    return None


def _find_cluster_by_fingerprint(storage: "Storage", *,
                                  tenant_id: int, fp: str) -> dict | None:
    """Cluster con fingerprint identico (F2 base, lookup esatto)."""
    rows = storage.list_ai_error_clusters(tenant_id=tenant_id,
                                            states=("accumulating", "ticket_opened"))
    for r in rows:
        if r.get("fingerprint_hex") == fp:
            return r
    return None


def _create_cluster(storage: "Storage", *, tenant_id: int, fp: str,
                     subject: str | None, body_excerpt: str | None) -> dict:
    cluster_id = storage.upsert_ai_error_cluster({
        "tenant_id": tenant_id,
        "fingerprint_hex": fp,
        "representative_subject": (subject or "")[:200],
        "representative_body_excerpt": (body_excerpt or "")[:500],
        "count": 1,
        "state": "accumulating",
    })
    logger.info("AI error cluster CREATED id=%s fp=%s '%s'",
                cluster_id, fp[:8], (subject or "")[:50])
    return {"action": "created", "cluster_id": cluster_id, "count": 1, "state": "accumulating"}


def _increment_cluster(storage: "Storage", cluster: dict, *,
                        subject: str | None,
                        body_excerpt: str | None) -> dict:
    new_count = int(cluster.get("count") or 0) + 1
    state = cluster.get("state") or "accumulating"
    threshold = int(cluster.get("manual_threshold") or 5)
    actions = ["incremented"]
    if state == "accumulating" and new_count >= threshold:
        state = "ticket_opened"
        actions.append("ticket_opened")
    storage.upsert_ai_error_cluster({
        "id": cluster["id"],
        "tenant_id": cluster["tenant_id"],
        "fingerprint_hex": cluster["fingerprint_hex"],
        "count": new_count,
        "state": state,
        "last_seen": "datetime('now')",
    })
    logger.info("AI error cluster #%s count=%d state=%s",
                cluster["id"], new_count, state)
    return {"action": ",".join(actions), "cluster_id": cluster["id"],
            "count": new_count, "state": state, "threshold": threshold}


def _mark_recovery(storage: "Storage", cluster: dict, *,
                    subject: str | None) -> dict:
    cid = cluster["id"]
    state_before = cluster.get("state")
    storage.upsert_ai_error_cluster({
        "id": cid,
        "tenant_id": cluster["tenant_id"],
        "fingerprint_hex": cluster["fingerprint_hex"],
        "state": "recovered",
        "recovery_seen_at": "datetime('now')",
    })
    logger.info("AI error cluster #%s RECOVERED (was %s) on '%s'",
                cid, state_before, (subject or "")[:50])
    return {"action": "recovered", "cluster_id": cid,
            "state": "recovered", "previous_state": state_before}
