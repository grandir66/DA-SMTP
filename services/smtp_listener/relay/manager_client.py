"""Client HTTP verso il manager (backend pluggabile).

Espone l'astrazione `ManagerBackend` che il resto del codice usa per dialogare con il manager
remoto. L'implementazione `StormshieldManagerBackend` è quella di default e parla con gli
endpoint `/api/v1/relay/*` dello Stormshield Manager. Il design plug-in permette di sostituire
il backend con altri (es. un manager diverso, una sorgente locale per uso standalone) senza
toccare listener/scheduler/pipeline.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import httpx

from relay.config import ManagerConfig

logger = logging.getLogger(__name__)


@dataclass
class CustomersPayload:
    synced_at: str
    customers: list[dict[str, Any]] = field(default_factory=list)
    etag: str | None = None


@dataclass
class RulesPayload:
    synced_at: str
    rules: list[dict[str, Any]] = field(default_factory=list)
    etag: str | None = None


@dataclass
class RoutesPayload:
    synced_at: str
    routes: list[dict[str, Any]] = field(default_factory=list)
    etag: str | None = None


@dataclass
class SettingsPayload:
    synced_at: str
    settings: dict[str, Any] = field(default_factory=dict)
    etag: str | None = None


@dataclass
class DomainRoutingPayload:
    synced_at: str
    domains: list[dict[str, Any]] = field(default_factory=list)
    etag: str | None = None


@dataclass
class TemplatesPayload:
    synced_at: str
    templates: list[dict[str, Any]] = field(default_factory=list)
    etag: str | None = None


@dataclass
class TicketResult:
    ok: bool
    ticket_id: str | None
    status_code: int
    response_body: str
    error: str | None = None


@dataclass
class AuthCodeResult:
    ok: bool
    code: str | None = None
    valid_until: str | None = None
    code_id: int | None = None
    error: str | None = None


@dataclass
class AuthCodeValidationResult:
    """Esito validazione codice (cascade oneshot → permanente)."""
    valid: bool
    kind: str | None = None      # 'oneshot' | 'permanent' | None
    reason: str | None = None    # 'ok' | 'not_found' | 'already_used' | 'expired' | 'revoked' | 'no_code_in_subject'
    code: str | None = None
    code_info: dict[str, Any] | None = None
    usage_id: int | None = None  # solo per permanente
    extracted_from_subject: bool = False
    error: str | None = None


@dataclass
class AggregationsPayload:
    synced_at: str
    aggregations: list[dict[str, Any]] = field(default_factory=list)
    etag: str | None = None


@dataclass
class CustomerGroupsPayload:
    """Gruppi clienti + membership (admin migration 018).

    Cached lato listener per il match `match_customer_groups` nelle regole.
    """
    synced_at: str
    groups: list[dict[str, Any]] = field(default_factory=list)
    members: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class H24TargetsPayload:
    """Mappatura source_domain → h24_alias multi-brand (Fase E).
    Cached lato listener per popolare h24_inbound_alias in build_context auto_reply.
    """
    synced_at: str
    targets: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class RecipientGroupsPayload:
    """Gruppi destinatari + membri (Migration 027).
    Cached lato listener per match_to_group_id e forward_to_group_id."""
    synced_at: str
    groups: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class PrivacyBypassPayload:
    """Lista privacy-bypass attiva (migration 011 admin).

    Indirizzi e domini che NON devono essere elaborati dal rule engine,
    dalle aggregations o dall'auto-reply per ragioni GDPR/operative.
    Pre-controllo lato listener PRIMA del rule engine.
    """
    synced_at: str
    from_emails: list[str] = field(default_factory=list)
    to_emails: list[str] = field(default_factory=list)
    from_domains: list[str] = field(default_factory=list)
    to_domains: list[str] = field(default_factory=list)
    etag: str | None = None


class ManagerError(Exception):
    """Errore comunicazione manager (rete, auth, schema invalido)."""


class ManagerBackend(ABC):
    @abstractmethod
    def fetch_active_customers(self) -> CustomersPayload: ...

    @abstractmethod
    def fetch_active_rules(self) -> RulesPayload: ...

    @abstractmethod
    def fetch_active_routes(self) -> RoutesPayload: ...

    @abstractmethod
    def fetch_active_settings(self) -> SettingsPayload: ...

    @abstractmethod
    def fetch_active_domain_routing(self) -> DomainRoutingPayload: ...

    @abstractmethod
    def fetch_active_templates(self) -> TemplatesPayload: ...

    def fetch_active_customer_groups(self) -> CustomerGroupsPayload:
        # Fallback: backend pre-migration-018 ignorano i gruppi.
        return CustomerGroupsPayload(synced_at="")

    @abstractmethod
    def submit_events(self, events: list[dict[str, Any]]) -> dict[str, Any]: ...

    @abstractmethod
    def submit_ticket(self, payload: dict[str, Any]) -> TicketResult: ...

    def validate_auth_code(self, *, code: str | None = None,
                             subject: str | None = None,
                             event_uuid: str | None = None,
                             from_address: str | None = None,
                             inbound_alias: str | None = None,
                             tenant_id: int = 1) -> "AuthCodeValidationResult":
        """Default: backend non supporta H24 (pre-Fase B). Ritorna invalid."""
        return AuthCodeValidationResult(
            valid=False, error="backend_does_not_support_h24",
        )

    def update_h24_usage_ticket(self, *, usage_id: int,
                                  ticket_id: str | None) -> bool:
        """Default: backend non supporta H24."""
        return False

    @abstractmethod
    def issue_auth_code(self, *, codcli: str | None, rule_id: int | None,
                        ttl_hours: int, note: str | None = None,
                        sent_to_email: str | None = None) -> AuthCodeResult: ...

    @abstractmethod
    def fetch_active_aggregations(self) -> AggregationsPayload: ...

    @abstractmethod
    def replicate_occurrence(self, agg_id: int, payload: dict[str, Any]) -> bool: ...

    def fetch_active_privacy_bypass(self) -> PrivacyBypassPayload:
        """Default implementation per backend che non ancora implementano la
        migrazione 011. Ritorna lista vuota (no bypass) — il listener si
        comporta come pre-011 (rule engine sempre attivo)."""
        return PrivacyBypassPayload(synced_at="", from_emails=[], to_emails=[],
                                    from_domains=[], to_domains=[])


class StormshieldManagerBackend(ManagerBackend):
    """Backend che parla con Stormshield Manager via /api/v1/relay/* + /api/v1/tickets/.

    submit_ticket() può essere indirizzato a un endpoint diverso (es. manager-dev
    esterno) tramite le settings `ticket_api.base_url` + `ticket_api.api_key` +
    `ticket_api.create_path` se `storage` è valorizzato. Pattern usato quando
    l'admin standalone (sul localhost del relay) NON serve l'endpoint
    /api/v1/tickets/ e i ticket vanno girati al vero gestionale.
    """

    def __init__(self, cfg: ManagerConfig, storage: "Storage | None" = None):
        self._cfg = cfg
        self._storage = storage
        if cfg.ca_bundle:
            verify: bool | str = cfg.ca_bundle
        elif not cfg.verify_tls:
            verify = False
        else:
            verify = True
        self._client = httpx.Client(
            base_url=cfg.base_url.rstrip("/"),
            timeout=cfg.timeout_sec,
            headers={
                "X-API-Key": cfg.api_key,
                "User-Agent": "stormshield-smtp-relay/0.1",
                "Accept": "application/json",
            },
            verify=verify,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "StormshieldManagerBackend":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    def _get_json(self, path: str) -> dict[str, Any]:
        try:
            resp = self._client.get(path)
        except httpx.HTTPError as exc:
            raise ManagerError(f"Errore rete su GET {path}: {exc}") from exc
        if resp.status_code == 401:
            raise ManagerError(f"Autenticazione fallita su {path} (API key invalida)")
        if resp.status_code >= 400:
            raise ManagerError(f"GET {path} ha risposto {resp.status_code}: {resp.text[:200]}")
        try:
            data = resp.json()
        except ValueError as exc:
            raise ManagerError(f"Risposta non JSON da {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ManagerError(f"Schema risposta invalido da {path}: atteso dict, ottenuto {type(data).__name__}")
        return data

    def fetch_active_customers(self) -> CustomersPayload:
        data = self._get_json("/api/v1/relay/customers/active")
        return CustomersPayload(
            synced_at=data.get("synced_at", ""),
            customers=list(data.get("customers", [])),
        )

    def fetch_active_rules(self) -> RulesPayload:
        data = self._get_json("/api/v1/relay/rules/active")
        return RulesPayload(
            synced_at=data.get("synced_at", ""),
            rules=list(data.get("rules", [])),
        )

    def fetch_active_routes(self) -> RoutesPayload:
        data = self._get_json("/api/v1/relay/routes/active")
        return RoutesPayload(
            synced_at=data.get("synced_at", ""),
            routes=list(data.get("routes", [])),
        )

    def fetch_active_settings(self) -> SettingsPayload:
        data = self._get_json("/api/v1/relay/settings/active")
        return SettingsPayload(
            synced_at=data.get("synced_at", ""),
            settings=dict(data.get("settings", {})),
        )

    def fetch_active_domain_routing(self) -> DomainRoutingPayload:
        data = self._get_json("/api/v1/relay/domain-routing/active")
        return DomainRoutingPayload(
            synced_at=data.get("synced_at", ""),
            domains=list(data.get("domains", [])),
        )

    def fetch_active_privacy_bypass(self) -> PrivacyBypassPayload:
        try:
            data = self._get_json("/api/v1/relay/privacy-bypass/active")
        except ManagerError as exc:
            # 404 atteso su manager pre-migration-011 → fallback safe
            if "404" in str(exc):
                return PrivacyBypassPayload(synced_at="")
            raise
        return PrivacyBypassPayload(
            synced_at=data.get("synced_at", ""),
            from_emails=[s.lower() for s in data.get("from", []) if s],
            to_emails=[s.lower() for s in data.get("to", []) if s],
            from_domains=[s.lower() for s in data.get("from_domains", []) if s],
            to_domains=[s.lower() for s in data.get("to_domains", []) if s],
        )

    def fetch_active_customer_groups(self) -> CustomerGroupsPayload:
        try:
            data = self._get_json("/api/v1/relay/customer-groups/active")
        except ManagerError as exc:
            # 404 atteso su manager pre-migration-018 → fallback safe
            if "404" in str(exc):
                return CustomerGroupsPayload(synced_at="")
            raise
        return CustomerGroupsPayload(
            synced_at=data.get("synced_at", ""),
            groups=list(data.get("groups", [])),
            members=list(data.get("members", [])),
        )

    def fetch_active_templates(self) -> TemplatesPayload:
        data = self._get_json("/api/v1/relay/templates/active")
        return TemplatesPayload(
            synced_at=data.get("synced_at", ""),
            templates=list(data.get("templates", [])),
        )

    def submit_events(self, events: list[dict[str, Any]]) -> dict[str, Any]:
        if not events:
            return {"accepted": 0, "duplicates": 0}
        try:
            resp = self._client.post("/api/v1/relay/events", json={"events": events})
        except httpx.HTTPError as exc:
            raise ManagerError(f"Errore rete su POST events: {exc}") from exc
        if resp.status_code >= 400:
            raise ManagerError(f"POST events ha risposto {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError:
            return {"accepted": len(events), "duplicates": 0}

    @staticmethod
    def _normalize_ticket_payload(payload: dict[str, Any]) -> dict[str, Any]:
        """Trasforma il payload interno nel formato accettato da manager API.

        Manager schema (da test su manager-dev.domarc.it):
        - descrizione  (obbligatorio)
        - urgenza      ∈ {'NORMALE', 'URGENTE'}
        - settore      ∈ {'G' (gestionale), 'S' (sistemistico)}
        - oggetto      (opzionale)
        - canale       (opzionale, default 'manuale')
        - codice_cliente (opzionale)
        - external_id  (opzionale, per idempotenza)

        Mapping interno → manager:
        - body          → descrizione
        - subject       → oggetto
        - channel       → canale
        - urgenza ALTA/HIGH/CRITICAL    → URGENTE
        - urgenza MEDIA/BASSA/altro     → NORMALE
        - settore S* / sistem*          → S
        - altro                          → G
        """
        urg = (payload.get("urgenza") or "").strip().upper()
        if urg in ("ALTA", "HIGH", "CRITICAL", "URGENTE"):
            urgenza = "URGENTE"
        else:
            urgenza = "NORMALE"
        sett = (payload.get("settore") or "").strip().upper()
        if sett.startswith("S"):
            settore = "S"
        else:
            settore = "G"
        out = {
            "descrizione": payload.get("body") or payload.get("descrizione") or "(no body)",
            "oggetto": payload.get("subject") or payload.get("oggetto"),
            "canale": payload.get("channel") or payload.get("canale") or "smtp_relay",
            "urgenza": urgenza,
            "settore": settore,
        }
        cc = (payload.get("codice_cliente") or "").strip()
        if cc:
            out["codice_cliente"] = cc
        ext_id = payload.get("external_id")
        if ext_id:
            out["external_id"] = ext_id
        # Tronca campi lunghi per evitare 413/limiti server
        if out.get("descrizione") and len(out["descrizione"]) > 8000:
            out["descrizione"] = out["descrizione"][:8000] + "\n…(truncato)"
        return out

    def submit_ticket(self, payload: dict[str, Any]) -> TicketResult:
        client = self._client
        path = "/api/v1/tickets/"
        external_client: httpx.Client | None = None
        # Normalizza il payload nel formato manager
        payload = self._normalize_ticket_payload(payload)
        # Override via settings ticket_api.* (es. manager-dev esterno)
        if self._storage is not None:
            try:
                ext_url = (self._storage.get_setting("ticket_api.base_url") or "").strip()
                ext_key = (self._storage.get_setting("ticket_api.api_key") or "").strip()
                ext_path = (self._storage.get_setting("ticket_api.create_path") or "").strip() or "/api/v1/tickets/"
                ext_verify = (self._storage.get_setting("ticket_api.verify_tls") or "true").lower() != "false"
                ext_timeout = float(self._storage.get_setting("ticket_api.timeout_sec") or "10")
            except Exception:  # noqa: BLE001
                ext_url = ext_key = ext_path = ""
                ext_verify, ext_timeout = True, 10.0
            if ext_url and ext_url.rstrip("/") != self._cfg.base_url.rstrip("/"):
                external_client = httpx.Client(
                    base_url=ext_url.rstrip("/"),
                    timeout=ext_timeout,
                    headers={
                        "X-API-Key": ext_key or self._cfg.api_key,
                        "User-Agent": "stormshield-smtp-relay/0.1",
                        "Accept": "application/json",
                    },
                    verify=ext_verify,
                )
                client = external_client
                path = ext_path
        try:
            try:
                resp = client.post(path, json=payload)
            except httpx.HTTPError as exc:
                return TicketResult(ok=False, ticket_id=None, status_code=0, response_body="", error=str(exc))
            body = resp.text[:1000]
            if resp.status_code >= 200 and resp.status_code < 300:
                try:
                    data = resp.json()
                    # Manager response: {"success": true, "data": {"tk_key": "...", ...}}
                    inner = data.get("data") if isinstance(data, dict) else None
                    src = inner if isinstance(inner, dict) else (data if isinstance(data, dict) else {})
                    ticket_id = str(src.get("tk_key") or src.get("ticket_id") or src.get("id") or "")
                except ValueError:
                    ticket_id = ""
                return TicketResult(ok=True, ticket_id=ticket_id or None, status_code=resp.status_code, response_body=body)
            return TicketResult(
                ok=False,
                ticket_id=None,
                status_code=resp.status_code,
                response_body=body,
                error=f"HTTP {resp.status_code}",
            )
        finally:
            if external_client is not None:
                external_client.close()

    def issue_auth_code(self, *, codcli: str | None, rule_id: int | None,
                        ttl_hours: int, note: str | None = None,
                        event_uuid: str | None = None,
                        sent_to_email: str | None = None) -> AuthCodeResult:
        try:
            resp = self._client.post(
                "/api/v1/relay/auth-codes",
                json={
                    "codice_cliente": codcli,
                    "rule_id": rule_id,
                    "ttl_hours": int(ttl_hours),
                    "note": note,
                    "event_uuid": event_uuid,
                    "sent_to_email": sent_to_email,
                },
            )
        except httpx.HTTPError as exc:
            return AuthCodeResult(ok=False, error=f"network: {exc}")
        if resp.status_code >= 200 and resp.status_code < 300:
            try:
                data = resp.json()
                if data.get("ok"):
                    return AuthCodeResult(
                        ok=True,
                        code=data.get("code"),
                        valid_until=data.get("valid_until"),
                        code_id=data.get("id"),
                    )
                return AuthCodeResult(ok=False, error=data.get("error") or "manager returned ok=false")
            except ValueError:
                return AuthCodeResult(ok=False, error="risposta non-JSON")
        return AuthCodeResult(ok=False, error=f"HTTP {resp.status_code}: {resp.text[:200]}")

    def validate_auth_code(self, *,
                             code: str | None = None,
                             subject: str | None = None,
                             event_uuid: str | None = None,
                             from_address: str | None = None,
                             inbound_alias: str | None = None,
                             tenant_id: int = 1) -> AuthCodeValidationResult:
        """Valida codice autorizzazione H24 sull'admin (cascade oneshot →
        permanente). Estrazione server-side se code è None ma subject è valorizzato.
        Atomico per consume oneshot.
        """
        try:
            resp = self._client.post(
                "/api/v1/relay/auth-codes/validate",
                json={
                    "code": code,
                    "subject": subject,
                    "event_uuid": event_uuid,
                    "from_address": from_address,
                    "inbound_alias": inbound_alias,
                    "tenant_id": int(tenant_id),
                },
            )
        except httpx.HTTPError as exc:
            return AuthCodeValidationResult(valid=False, error=f"network: {exc}")
        if resp.status_code < 200 or resp.status_code >= 300:
            return AuthCodeValidationResult(
                valid=False,
                error=f"HTTP {resp.status_code}: {resp.text[:200]}",
            )
        try:
            data = resp.json()
        except ValueError:
            return AuthCodeValidationResult(valid=False, error="risposta non-JSON")
        return AuthCodeValidationResult(
            valid=bool(data.get("valid")),
            kind=data.get("kind"),
            reason=data.get("reason"),
            code=data.get("code"),
            code_info=data.get("code_info"),
            usage_id=data.get("usage_id"),
            extracted_from_subject=bool(data.get("extracted_from_subject")),
        )

    def update_h24_usage_ticket(self, *, usage_id: int,
                                  ticket_id: str | None) -> bool:
        """Aggiorna usage_id.ticket_id post creazione ticket sul manager.
        Usato solo per codici permanenti (audit fatturazione)."""
        try:
            resp = self._client.post(
                f"/api/v1/relay/auth-codes/usage/{int(usage_id)}/ticket",
                json={"ticket_id": ticket_id},
            )
        except httpx.HTTPError:
            return False
        if resp.status_code < 200 or resp.status_code >= 300:
            return False
        try:
            return bool(resp.json().get("ok"))
        except ValueError:
            return False

    def fetch_active_aggregations(self) -> AggregationsPayload:
        data = self._get_json("/api/v1/relay/aggregations/active")
        return AggregationsPayload(
            synced_at=data.get("synced_at", ""),
            aggregations=list(data.get("aggregations", [])),
        )

    def fetch_active_h24_targets(self) -> H24TargetsPayload:
        """Mappatura source_domain → h24_alias (Fase E)."""
        data = self._get_json("/api/v1/relay/h24-targets/active")
        return H24TargetsPayload(
            synced_at=data.get("synced_at", ""),
            targets=list(data.get("targets", [])),
        )

    def fetch_active_recipient_groups(self) -> RecipientGroupsPayload:
        """Gruppi destinatari + membri (Migration 027)."""
        try:
            data = self._get_json("/api/v1/relay/recipient-groups/active")
        except Exception:  # noqa: BLE001
            return RecipientGroupsPayload(synced_at="", groups=[])
        return RecipientGroupsPayload(
            synced_at=data.get("synced_at", ""),
            groups=list(data.get("groups", [])),
        )

    def replicate_occurrence(self, agg_id: int, payload: dict[str, Any]) -> bool:
        try:
            resp = self._client.post(f"/api/v1/relay/aggregations/{agg_id}/occurrence", json=payload)
        except httpx.HTTPError as exc:
            logger.warning("replicate_occurrence rete: %s", exc)
            return False
        if resp.status_code >= 200 and resp.status_code < 300:
            return True
        logger.warning("replicate_occurrence HTTP %s: %s", resp.status_code, resp.text[:200])
        return False


def build_backend(cfg: ManagerConfig, storage: "Storage | None" = None) -> ManagerBackend:
    """Factory: sceglie l'implementazione in base a `cfg.backend`. Default 'stormshield'."""
    if cfg.backend == "stormshield":
        return StormshieldManagerBackend(cfg, storage=storage)
    raise ValueError(f"Backend manager '{cfg.backend}' non supportato in questa versione")
