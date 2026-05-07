/* Rule form modes — JS condiviso per i 3 form regola (orfana, gruppo padre, figlio).
 *
 * Implementa:
 *   - Toggle Modalità Base/Avanzata persistito in localStorage (chiave: rf-mode)
 *   - Preset priority (4 quick-button)
 *   - Validazione live inline (regex compile + lunghezza nome)
 *   - Mini-simulatore subject contro la regola in compilazione
 *   - Anteprima impatto (chiamata /rules/preview-impact)
 *
 * Tutte le funzioni sono no-op se i selettori target non esistono nella pagina.
 * Sicuro caricarlo in qualsiasi pagina senza side-effect.
 */

(function () {
    'use strict';

    // === CSRF token (presente in tutti i form Flask) ===
    function getCsrfToken() {
        const el = document.querySelector('input[name="csrf_token"]');
        return el ? el.value : '';
    }

    // ============================================================ Modalità

    function applyMode(mode) {
        if (mode !== 'advanced') mode = 'base';
        document.body.classList.toggle('rf-mode-base', mode === 'base');
        document.body.classList.toggle('rf-mode-advanced', mode === 'advanced');
        document.querySelectorAll('.rf-mode-radio').forEach(r => {
            r.classList.toggle('active', r.dataset.mode === mode);
        });
        document.querySelectorAll('input[name="rf_mode"]').forEach(r => {
            r.checked = r.value === mode;
        });
        // Quando si va in Base, ricalcola quali campi avanzati hanno
        // valore: questi restano visibili (rf-has-value) per non
        // nascondere informazioni importanti all'utente.
        if (mode === 'base') refreshAdvancedHasValue();
        const summary = document.getElementById('rf-mode-summary');
        if (summary) {
            summary.textContent = mode === 'base'
                ? 'Solo i campi essenziali (campi avanzati nascosti se vuoti, restano visibili se compilati).'
                : 'Tutti i campi visibili — incluse eccezioni puntuali, tristate derivati, flag flow, scope, severity.';
        }
        try { localStorage.setItem('rf-mode', mode); } catch (e) { /* ignore */ }
    }

    /**
     * Per ogni `.rf-advanced-only` controlla se al suo interno c'e' un
     * input/select/textarea con valore non-default. Se sì, applica
     * `.rf-has-value` per renderlo visibile anche in modalità Base.
     * In più aggiorna un counter nell'intestazione di ogni `.rf-section`
     * che mostra "N avanzati compilati" — così anche se i campi
     * restassero nascosti, l'utente vede subito che ci sono dati.
     *
     * Definizione "non-default":
     *   - input text/number/textarea: value.trim() != ''
     *   - select: selected option has value != '' (e non 'null')
     *   - checkbox: checked = true
     */
    function _hasNonDefaultValue(block) {
        const fields = block.querySelectorAll(
            'input[type="text"], input[type="number"], input[type="email"], textarea, select, input[type="checkbox"]'
        );
        for (const f of fields) {
            if (f.disabled) continue;
            if (f.type === 'checkbox') {
                if (f.checked) return true;
            } else if (f.tagName === 'SELECT') {
                if (f.value && f.value !== '' && f.value !== 'null') return true;
            } else {
                if ((f.value || '').trim() !== '') return true;
            }
        }
        return false;
    }

    function refreshAdvancedHasValue() {
        // Step 1: marca i singoli .rf-advanced-only con rf-has-value
        document.querySelectorAll('.rf-advanced-only').forEach(block => {
            block.classList.toggle('rf-has-value', _hasNonDefaultValue(block));
        });
        // Step 2: per ogni sezione, conta gli avanzati con valore e
        // aggiorna/crea un badge nell'intestazione (.rf-section-head)
        document.querySelectorAll('.rf-section').forEach(section => {
            const filled = section.querySelectorAll('.rf-advanced-only.rf-has-value').length;
            const total = section.querySelectorAll('.rf-advanced-only').length;
            const head = section.querySelector('.rf-section-head, h3');
            if (!head) return;
            let badge = head.querySelector('.rf-section-adv-counter');
            if (filled === 0) {
                if (badge) badge.remove();
                return;
            }
            if (!badge) {
                badge = document.createElement('span');
                badge.className = 'rf-section-adv-counter';
                head.appendChild(badge);
            }
            badge.innerHTML = '<i class="fas fa-circle-exclamation"></i> ' +
                filled + ' avanzat' + (filled === 1 ? 'o compilato' : 'i compilati') +
                (total > filled ? ' / ' + total : '');
            badge.title = 'In questa sezione ci sono ' + filled +
                ' campi della modalità Avanzata che hanno un valore. ' +
                'Restano visibili anche in Base per non perderli di vista.';
        });
    }

    window.rfSetMode = function (mode) { applyMode(mode); };

    function initModeToggle() {
        document.querySelectorAll('.rf-mode-radio').forEach(r => {
            r.addEventListener('click', () => applyMode(r.dataset.mode));
        });
        // Restore stato salvato (default Base se assente)
        let saved = 'base';
        try { saved = localStorage.getItem('rf-mode') || 'base'; } catch (e) { /* ignore */ }
        applyMode(saved);
        // Calcola lo stato dei campi avanzati anche al primo load (sia
        // in modalità Base sia Avanzata, così il counter è sempre fresco).
        refreshAdvancedHasValue();
        // Aggiorna live mentre l'utente compila: ogni cambio input
        // dentro un .rf-advanced-only ricalcola counter + visibilità.
        document.querySelectorAll('.rf-advanced-only input, .rf-advanced-only select, .rf-advanced-only textarea').forEach(el => {
            el.addEventListener('change', refreshAdvancedHasValue);
            el.addEventListener('blur', refreshAdvancedHasValue);
        });
    }

    // ============================================================ Preset priority

    window.rfSetPriority = function (val, btn) {
        const inp = document.getElementById('priority') || document.querySelector('input[name="priority"]');
        if (inp) inp.value = val;
        document.querySelectorAll('.rf-priority-presets button').forEach(b => b.classList.remove('applied'));
        if (btn) btn.classList.add('applied');
    };

    // ============================================================ Validazione live

    /* Validazione client-side leggera. Per i campi regex chiama
     * /rules/test-regex con un sample vuoto solo per validare la sintassi
     * (re.compile lato server). Per il name verifica solo lunghezza min. */

    let _validateTimer = null;

    window.rfLiveValidate = function (input, kind) {
        if (_validateTimer) clearTimeout(_validateTimer);
        _validateTimer = setTimeout(() => doValidate(input, kind), 350);
    };

    function doValidate(input, kind) {
        const row = input.closest('.rf-row');
        if (!row) return;
        row.classList.remove('rf-valid', 'rf-invalid', 'rf-validating');
        let msg = row.querySelector('.rf-validation-msg');
        if (!msg) {
            msg = document.createElement('div');
            msg.className = 'rf-validation-msg';
            const container = input.parentElement;
            (container || row).appendChild(msg);
        }
        msg.classList.remove('valid', 'invalid', 'validating');

        const val = (input.value || '').trim();
        if (!val) { msg.textContent = ''; return; }

        if (kind === 'regex') {
            // Validazione browser-side rapida con new RegExp (subset di Python)
            try {
                new RegExp(val);
                row.classList.add('rf-valid');
                msg.classList.add('valid');
                msg.textContent = '✓ Regex sintatticamente valida';
            } catch (e) {
                row.classList.add('rf-invalid');
                msg.classList.add('invalid');
                msg.textContent = '✗ Regex invalida: ' + e.message;
            }
        } else if (kind === 'name') {
            if (val.length >= 3) {
                row.classList.add('rf-valid');
                msg.classList.add('valid');
                msg.textContent = '✓ OK';
            } else {
                row.classList.add('rf-invalid');
                msg.classList.add('invalid');
                msg.textContent = 'Almeno 3 caratteri';
            }
        }
    }

    // ============================================================ Mini-simulatore

    /* Test rapido: prende il subject scritto dall'utente e verifica match
     * contro la regex client-side. Solo subject (semplice + immediato). */

    window.rfSimulateInline = function (textarea) {
        const result = document.getElementById('rf-sim-result');
        if (!result) return;
        const subject = (textarea.value || '').trim();
        if (!subject) {
            result.classList.remove('match', 'no-match');
            result.textContent = '';
            return;
        }
        const subjRegex = (document.querySelector('input[name="match_subject_regex"]') || {}).value || '';
        const bodyRegex = (document.querySelector('input[name="match_body_regex"]') || {}).value || '';
        let matches = true;
        let reasons = [];

        if (subjRegex) {
            try {
                const re = new RegExp(subjRegex);
                if (re.test(subject)) {
                    reasons.push('subject matcha <code>' + escapeHtml(subjRegex) + '</code>');
                } else {
                    matches = false;
                    reasons.push('subject NON matcha <code>' + escapeHtml(subjRegex) + '</code>');
                }
            } catch (e) {
                matches = false;
                reasons.push('subject regex invalida: ' + escapeHtml(e.message));
            }
        }
        if (bodyRegex) {
            reasons.push('body regex ignorata in questo test rapido (compila il body intero per testarla)');
        }
        if (!subjRegex && !bodyRegex) {
            reasons.push('nessuna regex contenuto impostata: la regola matcha qualsiasi subject');
        }

        result.classList.remove('match', 'no-match');
        if (matches) {
            result.classList.add('match');
            result.innerHTML = '✓ <strong>Matcha</strong> — ' + reasons.join('; ') + '.';
        } else {
            result.classList.add('no-match');
            result.innerHTML = '✗ <strong>Non matcha</strong> — ' + reasons.join('; ') + '.';
        }
    };

    // ============================================================ Anteprima impatto

    window.rfPreviewImpact = function (btn) {
        if (btn) {
            btn.disabled = true;
            btn.textContent = 'Calcolo in corso...';
        }
        const result = document.getElementById('rf-impact-result');
        if (result) result.classList.remove('shown');

        const form = document.querySelector('form');
        if (!form) return;

        // Raccolgo tutti i match_* compilati nel form
        const data = new FormData();
        const matchKeys = [
            'match_from_regex', 'match_from_domain',
            'match_to_regex', 'match_to_domain', 'match_to_group_id',
            'match_subject_regex', 'match_body_regex',
            'match_at_hours', 'match_in_service',
            'match_contract_active', 'match_known_customer',
            'match_has_exception_today', 'match_is_thread_continuation',
            'match_tag',
        ];
        matchKeys.forEach(k => {
            const el = form.querySelector('[name="' + k + '"]');
            if (el && el.value) data.append(k, el.value);
        });
        // match_customer_groups è multiselect → recupero tutti i selected
        form.querySelectorAll('select[name="match_customer_groups"] option:checked').forEach(o => {
            data.append('match_customer_groups', o.value);
        });

        fetch('/rules/preview-impact', {
            method: 'POST',
            headers: { 'X-CSRFToken': getCsrfToken() },
            body: data,
        })
        .then(r => r.json())
        .then(resp => {
            if (btn) { btn.disabled = false; btn.textContent = 'Calcola impatto stimato'; }
            if (!result) return;
            if (resp.error) {
                result.innerHTML = '<span style="color:#b91c1c;">✗ Errore: ' + escapeHtml(resp.error) + '</span>';
                result.classList.add('shown');
                return;
            }
            const total = resp.total_events_window || 0;
            const matched = resp.matched_count || 0;
            const samples = resp.samples || [];
            const window_h = resp.window_hours || 168;
            const days = Math.round(window_h / 24);

            let html = '<strong class="rf-impact-num">' + matched + '</strong> eventi degli ultimi ' + days + ' giorni avrebbero matchato questa regola';
            if (total > 0) {
                const pct = ((matched / total) * 100).toFixed(1);
                html += ' (sui <strong>' + total + '</strong> totali processati, <strong>' + pct + '%</strong>).';
            } else {
                html += '.';
            }
            if (samples.length) {
                html += '<ul>';
                samples.forEach(s => {
                    html += '<li>' +
                        '<small>' + escapeHtml(s.created_at || '') + '</small> ' +
                        '<code>' + escapeHtml(s.from_address || '?') + '</code> → ' +
                        '<code>' + escapeHtml(s.to_address || '?') + '</code>' +
                        ' — ' + escapeHtml((s.subject || '').substring(0, 80)) +
                        '</li>';
                });
                html += '</ul>';
            } else if (matched === 0) {
                html += '<div style="margin-top:6px; color:#64748b;"><em>Nessun evento corrispondente nel periodo.</em></div>';
            }
            result.innerHTML = html;
            result.classList.add('shown');
        })
        .catch(err => {
            if (btn) { btn.disabled = false; btn.textContent = 'Calcola impatto stimato'; }
            if (result) {
                result.innerHTML = '<span style="color:#b91c1c;">✗ Errore di rete: ' + escapeHtml(err.message || 'unknown') + '</span>';
                result.classList.add('shown');
            }
        });
    };

    // ============================================================ Helpers

    function escapeHtml(s) {
        if (s == null) return '';
        return String(s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    // ============================================================ Init

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initModeToggle);
    } else {
        initModeToggle();
    }
})();
