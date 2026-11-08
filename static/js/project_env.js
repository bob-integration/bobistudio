// Project environment (app/project_environment.py), asked on 2026-11-07.
//  - confirmType / confirmVmid: reminder before a transcoder, pyramide… joins a project;
//  - propose(pid): after a recall, offers to create the shared services this cluster lacks,
//    outside any project.
(function () {
    const t = (k) => window.t(k);
    const esc = (s) => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

    async function notice(url) {
        try {
            const r = await fetch(url);
            return r.ok ? (await r.json()).notice : null;
        } catch (e) { return null; }
    }

    function ask(n, projectName) {
        if (!n) return true;
        const msg = t('projects.env_notice_' + n.scope).replace('{label}', n.label);
        return confirm(msg + '\n\n' + (projectName
            ? t('projects.env_notice_question').replace('{project}', projectName)
            : t('projects.env_notice_question_this')));
    }

    async function confirmType(type, projectName) {
        return ask(await notice('/api/plugins/' + encodeURIComponent(type) + '/project_notice'), projectName);
    }

    async function confirmVmid(vmid, projectName) {
        return ask(await notice('/api/containers/' + vmid + '/project_notice'), projectName);
    }

    async function propose(pid) {
        let env;
        try {
            const r = await fetch('/api/projects/' + pid + '/environment');
            if (!r.ok) return;
            env = await r.json();
        } catch (e) { return; }
        if (!env.missing || !env.missing.length) return;

        const ov = document.createElement('div');
        ov.className = 'modal-overlay';
        ov.style.zIndex = 1100;     // above the recall overlay it follows
        const nodeSel = (nodes) => nodes.length
            ? '<select class="pe-node">' + nodes.map(n => `<option value="${n.id}">${esc(n.name)}</option>`).join('') + '</select>'
            : `<span class="text-muted">${esc(t('projects.env_no_node'))}</span>`;
        const any = env.missing.some(m => (m.nodes || []).length);
        ov.innerHTML = `<div class="modal-card" role="dialog" aria-modal="true">
            <div class="modal-head"><h3>${esc(t('projects.env_title'))}</h3></div>
            <p>${esc(t('projects.env_intro'))}</p>
            <table class="table pe-table"><tbody>${env.missing.map((m, i) => `
                <tr data-i="${i}">
                  <td><label><input type="checkbox" class="pe-on" ${(m.nodes || []).length ? 'checked' : 'disabled'}> <b>${esc(m.label)}</b></label>
                      <div class="text-muted">${esc(t('projects.env_reason_' + m.reason))} · ${esc(t('projects.env_scope_' + m.scope))}</div></td>
                  <td>${esc(t('projects.env_node'))} ${nodeSel(m.nodes || [])}</td>
                  <td class="pe-res"></td>
                </tr>`).join('')}</tbody></table>
            <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px">
              <button class="btn btn-ghost pe-skip">${esc(t('projects.env_skip'))}</button>
              <button class="btn btn-primary pe-go" ${any ? '' : 'disabled'}>${esc(t('projects.env_create'))}</button>
            </div></div>`;
        document.body.appendChild(ov);
        const close = () => ov.remove();
        ov.querySelector('.pe-skip').onclick = close;
        ov.querySelector('.pe-go').onclick = async (ev) => {
            const go = ev.currentTarget;
            go.disabled = true;
            let allOk = true;
            for (const tr of ov.querySelectorAll('tr[data-i]')) {
                if (!tr.querySelector('.pe-on').checked) continue;
                const m = env.missing[+tr.dataset.i];
                const res = tr.querySelector('.pe-res');
                try {
                    const r = await fetch('/api/containers', {
                        method: 'POST', headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ hostname: m.hostname, deploy_type: m.type,
                                               deploy_params: Object.keys(m.params || {}).length ? m.params : null,
                                               node_id: parseInt(tr.querySelector('.pe-node').value),
                                               project_id: null }),
                    });
                    const b = await r.json().catch(() => ({}));
                    res.textContent = r.ok ? '✓ ' + t('projects.env_created') : '✕ ' + (b.error || t('projects.env_failed'));
                    allOk = allOk && r.ok;
                } catch (e) { res.textContent = '✕ ' + e.message; allOk = false; }
                tr.querySelector('.pe-on').disabled = true;
            }
            go.style.display = 'none';
            ov.querySelector('.pe-skip').textContent = t('projects.env_close');
            if (allOk) setTimeout(close, 1500);
        };
    }

    window.ProjectEnv = { confirmType, confirmVmid, propose };
})();
