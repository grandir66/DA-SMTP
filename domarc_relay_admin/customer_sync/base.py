"""Interfaccia astratta CustomerSyncProvider."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Iterator


# Alias per chiarezza: dict raw fetchato dal provider, prima del mapping.
FetchedRecord = dict[str, Any]


class ProviderConnectionError(RuntimeError):
    """Errore di connessione/auth verso la sorgente esterna."""


class CustomerSyncProvider(ABC):
    """Provider che produce record cliente da una sorgente esterna.

    Il provider conosce solo come parlare con la sorgente (postgres/mssql/csv/
    json_url) — non conosce lo schema canonico dei clienti. La normalizzazione
    e' responsabilita' di `mapper.apply()` con `mapping_json` configurato per
    sorgente.

    Eccezione: PostgresProvider in modalita' legacy (sentinel
    `_use_legacy_pgconfig=true`) ritorna gia' record canonici e bypassa il
    mapper (mapping_json deve contenere `_legacy: true`).
    """

    @abstractmethod
    def fetch(self) -> Iterator[FetchedRecord]:
        """Yield record raw dalla sorgente. Un record per cliente."""
        ...

    @abstractmethod
    def test_connection(self) -> dict[str, Any]:
        """Verifica connessione + count stimato. Ritorna dict diagnostico:
            {ok: bool, message: str, sample_count?: int, error?: str}
        """
        ...

    @abstractmethod
    def describe_schema(self) -> list[str]:
        """Ritorna i nomi delle colonne disponibili dalla sorgente.

        Usato dal mapping editor UI per popolare il dropdown 'colonna sorgente'.
        Per provider relazionali: legge le colonne della query/tabella.
        Per CSV: legge la riga di header.
        Per JSON: ispeziona la prima record.
        """
        ...

    # ---- helper per modalita' legacy (override in PostgresProvider) ---------

    def is_legacy_mode(self) -> bool:
        return False
