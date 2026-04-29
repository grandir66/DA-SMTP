"""Interfaccia astratta CustomerSource — schema minimo cliente (D5 del piano)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Customer:
    """Schema minimo customer come da decisione D5 del piano standalone."""
    codice_cliente: str
    ragione_sociale: str | None = None
    tipologia_servizio: str = "standard"      # standard | extended | h24 | <profile-id>
    profile_description: str | None = None    # Descrizione human-readable del profilo orari
    is_active: bool = True                    # Cliente abilitato sul manager (sostituisce contract_active)
    contract_type: str | None = None          # Es. "Full Service - Gestione Completa"
    contract_expiry_date: str | None = None   # ISO date o None se nessuna scadenza
    domains: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    notes: str | None = None
    holidays: list[str] | None = None
    schedule: dict[str, Any] | None = None    # Schedule template completo
    schedule_overrides: list[dict[str, Any]] | None = None

    # Retrocompat: alias di is_active per consumer pre-2026-04-29
    @property
    def contract_active(self) -> bool:
        return self.is_active

    def to_dict(self) -> dict[str, Any]:
        return {
            "codcli": self.codice_cliente,
            "ragione_sociale": self.ragione_sociale,
            "tipologia_servizio": self.tipologia_servizio,
            "profile_description": self.profile_description,
            "is_active": self.is_active,
            "contract_type": self.contract_type,
            "contract_expiry_date": self.contract_expiry_date,
            "domains": list(self.domains),
            "aliases": list(self.aliases),
            "notes": self.notes,
            "holidays": self.holidays,
            "schedule": self.schedule,
            "schedule_overrides": self.schedule_overrides,
        }


class CustomerSource(ABC):
    """Adapter pluggable per l'anagrafica clienti."""

    @abstractmethod
    def list_customers(self) -> list[Customer]: ...

    @abstractmethod
    def get_by_codcli(self, codcli: str) -> Customer | None: ...

    def resolve_by_email(self, email: str) -> Customer | None:
        """Default: split email → cerca alias diretto su tutti, poi domain match."""
        if not email or "@" not in email:
            return None
        email = email.strip().lower()
        domain = email.rsplit("@", 1)[-1]
        # 1. Alias diretto
        for c in self.list_customers():
            if email in (a.lower() for a in (c.aliases or [])):
                return c
        # 2. Domain match
        for c in self.list_customers():
            if domain in (d.lower() for d in (c.domains or [])):
                return c
        return None

    @abstractmethod
    def health(self) -> dict[str, Any]:
        """Stato del backend (count, last_sync, errori) per /diagnostic."""
