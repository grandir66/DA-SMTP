/**
 * sortable.js — sort lato client per tabelle Domarc SMTP Relay.
 *
 * Uso (HTML):
 *   <table class="dr-sortable">  oppure  <table class="fw-sortable">
 *     <thead>
 *       <tr>
 *         <th data-sort="text">Email</th>      <- sort alfabetico
 *         <th data-sort="num">Visto</th>       <- sort numerico
 *         <th data-sort="date">Ultima</th>     <- sort cronologico (ISO)
 *         <th>Azioni</th>                       <- nessun sort (no data-sort)
 *       </tr>
 *     </thead>
 *     <tbody>
 *       <tr><td>...</td><td>...</td><td>...</td><td>...</td></tr>
 *     </tbody>
 *   </table>
 *
 * Click sull'intestazione → ordinamento ascendente.
 * Re-click → discendente.
 * Indicatore visuale: ▲ asc / ▼ desc / ⇅ (default).
 *
 * Le righe vuote (con classe .dr-empty o un solo td colspan) NON vengono
 * ordinate (rimangono in coda).
 */
(function() {
    'use strict';

    function initSortable(table) {
        const headers = table.querySelectorAll('thead th[data-sort]');
        if (!headers.length) return;

        headers.forEach((th, idx) => {
            th.style.cursor = 'pointer';
            th.style.userSelect = 'none';
            th.classList.add('sortable-th');
            // marker visuale default
            if (!th.querySelector('.sort-marker')) {
                const m = document.createElement('span');
                m.className = 'sort-marker';
                m.style.opacity = '0.4';
                m.style.fontSize = '0.75em';
                m.style.marginLeft = '0.3em';
                m.textContent = ' ⇅';
                th.appendChild(m);
            }
            th.addEventListener('click', () => sortBy(table, th, idx));
        });
    }

    function sortBy(table, th, colIdx) {
        const type = th.dataset.sort || 'text';
        const tbody = table.querySelector('tbody');
        if (!tbody) return;

        const allRows = Array.from(tbody.querySelectorAll(':scope > tr'));
        // Skip righe "vuote" o senza colonna corrispondente
        const dataRows = allRows.filter(r => {
            if (r.classList.contains('dr-empty')) return false;
            const cells = r.querySelectorAll(':scope > td');
            if (cells.length <= colIdx) return false;
            // Se la riga è una "details/expand" (es. body-row in h24_code_usages)
            // non la consideriamo nel sorting
            if (r.classList.contains('body-row')) return false;
            return true;
        });
        const otherRows = allRows.filter(r => !dataRows.includes(r));
        if (!dataRows.length) return;

        const asc = !th.classList.contains('sort-asc');
        // reset marker su tutti gli header sortable della stessa table
        table.querySelectorAll('thead th[data-sort]').forEach(h => {
            h.classList.remove('sort-asc', 'sort-desc');
            const m = h.querySelector('.sort-marker');
            if (m) {
                m.textContent = ' ⇅';
                m.style.opacity = '0.4';
            }
        });
        th.classList.add(asc ? 'sort-asc' : 'sort-desc');
        const marker = th.querySelector('.sort-marker');
        if (marker) {
            marker.textContent = asc ? ' ▲' : ' ▼';
            marker.style.opacity = '1';
        }

        dataRows.sort((rA, rB) => {
            const a = (rA.querySelectorAll(':scope > td')[colIdx] || {}).textContent || '';
            const b = (rB.querySelectorAll(':scope > td')[colIdx] || {}).textContent || '';
            const cmp = compareValues(a.trim(), b.trim(), type);
            return asc ? cmp : -cmp;
        });

        // Riapplica al DOM (data rows prima, others dopo)
        dataRows.forEach(r => tbody.appendChild(r));
        otherRows.forEach(r => tbody.appendChild(r));
    }

    function compareValues(a, b, type) {
        if (type === 'num') {
            const na = parseFloat(a.replace(/[^\d.\-]/g, ''));
            const nb = parseFloat(b.replace(/[^\d.\-]/g, ''));
            const fa = isNaN(na) ? -Infinity : na;
            const fb = isNaN(nb) ? -Infinity : nb;
            return fa - fb;
        }
        if (type === 'date') {
            // ISO date prefix → parsa come Date
            const da = Date.parse(a) || 0;
            const db = Date.parse(b) || 0;
            return da - db;
        }
        // text default — locale italiano, case-insensitive
        return a.localeCompare(b, 'it', { sensitivity: 'base', numeric: true });
    }

    // Auto-init: tabelle marcate esplicitamente OR tabelle dr-table/fw-table
    // con almeno una colonna marcata data-sort.
    // Per le tabelle generiche dr-table/fw-table senza data-sort esplicito,
    // applica auto-detection: ogni <th> con testo diventa sortable text di default,
    // tranne l'ultima colonna (tipicamente "azioni") e quelle con class .no-sort.
    function autoMarkSortable(table) {
        const headers = table.querySelectorAll('thead th');
        if (!headers.length) return;
        const hasExplicit = Array.from(headers).some(h => h.dataset.sort);
        if (hasExplicit) return; // rispetta marcatura manuale
        const last = headers.length - 1;
        headers.forEach((th, i) => {
            if (th.classList.contains('no-sort')) return;
            // Skip ultima colonna se contiene solo bottoni/azioni (heuristica)
            if (i === last && (th.textContent.trim() === '' || /azion/i.test(th.textContent))) {
                return;
            }
            // Skip checkbox column (selectAll)
            if (th.querySelector('input[type=checkbox]')) return;
            // Numeric heuristic: header contains "count", "n.", "n°", "visto"
            if (/count|n[.°]|visto|num|qta|usat/i.test(th.textContent)) {
                th.dataset.sort = 'num';
            } else if (/data|quando|giorno|ora|prima|ultima|generat|spedit|accettat|scad/i.test(th.textContent)) {
                th.dataset.sort = 'date';
            } else {
                th.dataset.sort = 'text';
            }
        });
    }

    function initAll() {
        // Tabelle marcate manualmente
        document.querySelectorAll('table.dr-sortable, table.fw-sortable').forEach(initSortable);
        // Auto-detection sulle generiche dr-table / fw-table con almeno 3 righe
        document.querySelectorAll('table.dr-table, table.fw-table').forEach(table => {
            if (table.classList.contains('dr-sortable') || table.classList.contains('fw-sortable')) {
                return;  // già processata
            }
            const tbody = table.querySelector('tbody');
            if (!tbody) return;
            const rows = tbody.querySelectorAll(':scope > tr');
            if (rows.length < 2) return;  // niente sorting su tabelle vuote/minime
            autoMarkSortable(table);
            initSortable(table);
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initAll);
    } else {
        initAll();
    }

    // Espone API pubblica per init manuale di tabelle aggiunte dinamicamente
    window.DomarcSortable = { init: initSortable, initAll };
})();
