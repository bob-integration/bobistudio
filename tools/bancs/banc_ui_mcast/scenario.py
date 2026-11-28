#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
"""Browser bench of the multicast plan editor (Settings -> Multicast). See README.md.

Runs with a Python that has Playwright (NOT the repo venv); the harness itself (Flask render +
throwaway DB) runs in a subprocess with the repo venv. One fresh harness per run.

    <pw-python> tools/bancs/banc_ui_mcast/scenario.py --out /tmp/mcast-shots
Exit status: 0 = every check passed; 1 = a console error, a failed request or a broken check.
"""
import argparse
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
REPO_PY = os.path.join(ROOT, "venv", "bin", "python")

from playwright.sync_api import sync_playwright  # noqa: E402

FAILS = []
WARNS = []


def warn(run, label, cond, got=None):
    """A defect of a SHARED control (catalogue): reported, does not fail the bench."""
    print(("  [OK] " if cond else "  [WARN] ") + label + ("" if cond else "  -> %r" % (got,)))
    if not cond:
        WARNS.append("%s: %s" % (run, label))
# Console errors raised by OTHER parts of the settings page, reported separately (see README).
KNOWN_UNRELATED = [
    "reading 'style'",      # setInterval on #set-tab-pupitres, an element no template renders
]


def check(run, label, cond, got=None):
    print(("  [OK] " if cond else "  [KO] ") + label + ("" if cond else "  -> %r" % (got,)))
    if not cond:
        FAILS.append("%s: %s" % (run, label))


def start_harness(delay):
    env = dict(os.environ, BANC_DELAY_REFS=str(delay))
    p = subprocess.Popen([REPO_PY, os.path.join(HERE, "harness.py")], cwd=ROOT, env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    t0 = time.time()
    try:
        while time.time() - t0 < 120:
            line = p.stdout.readline()
            if not line:
                break
            if line.startswith("serving "):
                # Keep draining its output: a full pipe would block the harness mid-run.
                threading.Thread(target=lambda: [None for _ in p.stdout], daemon=True).start()
                return p, line.split()[1].rsplit("/settings", 1)[0]
    except BaseException:
        stop_harness(p)
        raise
    stop_harness(p)
    raise SystemExit("harness did not start")


def stop_harness(p):
    """Stop the harness THIS bench started (never another process)."""
    if p.poll() is None:
        p.kill()
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


# Geometry of every control of the editor, grouped by visual row. A row = the .mp-fieldcol
# siblings whose LABEL starts at the same height; inside a row, every control must share its top
# and its height (buttons were 10 px lower than fields).
ALIGN_JS = r"""
(sel) => {
  const out = [];
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 0 && r.height > 0; };
  document.querySelectorAll(sel).forEach((row, ri) => {
    const cols = [...row.querySelectorAll(':scope > .mp-fieldcol, :scope > .mp-line-main .mp-fieldcol')].filter(vis);
    const groups = [];
    cols.forEach(fc => {
      const lab = fc.querySelector(':scope > label');
      const top = Math.round((lab || fc).getBoundingClientRect().top);
      let g = groups.find(g => Math.abs(g.top - top) <= 3);
      if (!g) { g = {top, items: [], texts: []}; groups.push(g); }
      fc.querySelectorAll('input:not([type=checkbox]):not([type=radio]), select, button').forEach(c => {
        if (!vis(c) || c.closest('details')) return;
        const r = c.getBoundingClientRect();
        g.items.push({id: c.id || c.className || c.tagName, top: r.top, h: r.height, bottom: r.bottom});
      });
      fc.querySelectorAll('input[type=checkbox]').forEach(c => {
        if (!vis(c)) return;
        const r = c.getBoundingClientRect();
        g.texts.push({id: c.id, mid: (r.top + r.bottom) / 2});
      });
      fc.querySelectorAll('[id^=mp_cap_]').forEach(c => {
        const r = c.getBoundingClientRect();
        g.texts.push({id: c.id, mid: (r.top + r.bottom) / 2});
      });
    });
    groups.forEach(g => {
      if (!g.items.length) return;
      const tops = g.items.map(i => i.top), hs = g.items.map(i => i.h);
      const mid = (Math.min(...tops) + Math.max(...g.items.map(i => i.bottom))) / 2;
      out.push({row: ri, top: g.top, dTop: Math.max(...tops) - Math.min(...tops),
                dH: Math.max(...hs) - Math.min(...hs), minH: Math.min(...hs),
                dText: Math.max(0, ...g.texts.map(t => Math.abs(t.mid - mid))),
                items: g.items.map(i => `${i.id}@${Math.round(i.top)}+${Math.round(i.h)}`)});
    });
  });
  return out;
}
"""

# Every visible interactive element of the editor: size (touch target) and overflow.
TARGETS_JS = r"""
() => {
  const bad = [];
  document.querySelectorAll('#set-tab-mcast button, #set-tab-mcast select, #set-tab-mcast input, #set-tab-mcast summary').forEach(e => {
    const r = e.getBoundingClientRect();
    if (!r.width || !r.height || e.closest('dialog:not([open])')) return;
    const box = e.type === 'checkbox' || e.type === 'radio' ? (e.closest('label') || e) : e;
    const rb = box.getBoundingClientRect();
    if (e.type !== 'radio' && (rb.height < 34 || (e.tagName === 'BUTTON' && rb.width < 34)))
      bad.push({shared: e.classList.contains('num-btn') || e.classList.contains('ios-toggle'),
                what: `${e.id || e.className || e.tagName} ${Math.round(rb.width)}x${Math.round(rb.height)}`});
  });
  return bad;
}
"""

# Rows of each plan line: the field columns grouped by the top of their label. `tail` = the ports +
# free/capacity block, which must move as a whole.
ROWS_JS = r"""
() => [...document.querySelectorAll('#mp-editor .mp-line')].map(line => {
  const top = e => Math.round((e.querySelector(':scope > label') || e).getBoundingClientRect().top);
  const groups = {};
  line.querySelectorAll('.mp-line-main .mp-fieldcol').forEach(fc => {
    const t = top(fc), k = Object.keys(groups).find(x => Math.abs(x - t) <= 3) || t;
    (groups[k] = groups[k] || []).push(fc.querySelector('input,select,button,span[id^=mp_cap_]')?.id || fc.className);
  });
  const tail = [...line.querySelectorAll('.mp-line-tail .mp-fieldcol')].map(top);
  return {i: line.dataset.i, rows: Object.values(groups), tailTops: [...new Set(tail)]};
})
"""

CLIP_JS = r"""
() => {
  const bad = [];
  document.querySelectorAll('#set-tab-mcast input, #set-tab-mcast select, #set-tab-mcast button, #set-tab-mcast .mp-range-info, #set-tab-mcast td, #set-tab-mcast .mp-fab').forEach(e => {
    const r = e.getBoundingClientRect();
    if (!r.width) return;
    if (e.scrollWidth > e.clientWidth + 1 && e.tagName !== 'SELECT' && e.tagName !== 'INPUT')
      bad.push('clipped ' + (e.id || e.className || e.tagName) + ' ' + e.scrollWidth + '>' + e.clientWidth);
    if (e.tagName === 'INPUT' && e.type === 'text' && e.value && e.scrollWidth > e.clientWidth + 1)
      bad.push('value cut ' + e.id + ' "' + e.value + '"');
    const host = document.getElementById('set-tab-mcast').getBoundingClientRect();
    if (r.right > host.right + 1) bad.push('overflows tab ' + (e.id || e.className || e.tagName));
  });
  const de = document.documentElement;
  if (de.scrollWidth > de.clientWidth + 1) bad.push('page scrolls horizontally ' + de.scrollWidth + '>' + de.clientWidth);
  return bad;
}
"""


def run(pw, engine, width, height, touch, out, delay, inject_css=""):
    """One viewport on a fresh harness. The harness and the browser are stopped whatever happens
    (an exception in a step used to leave `harness.py` running)."""
    name = "%s-%d" % (engine, width)
    print("\n== %s %dx%d%s ==" % (engine, width, height, " touch" if touch else ""))
    proc, base = start_harness(delay)
    browser = None
    try:
        browser = getattr(pw, engine).launch()
        _scenario(browser, base, name, width, height, touch, out, inject_css)
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        stop_harness(proc)


def _scenario(browser, base, name, width, height, touch, out, inject_css):
    # iPadOS Safari asks for the desktop site: touch, but no mobile viewport zoom-out.
    ctx = browser.new_context(viewport={"width": width, "height": height}, has_touch=touch,
                              device_scale_factor=1)
    page = ctx.new_page()
    errors, failed = [], []
    allow_400 = {"on": False}
    def on_console(m):
        if m.type != "error":
            return
        url = (m.location or {}).get("url", "")
        if "status of 400" in m.text and "/api/mcast_plans/preview" in url and allow_400["on"]:
            return                      # the overlap refusal the scenario provokes on purpose
        errors.append(m.text + " @" + url)
    page.on("console", on_console)
    page.on("pageerror", lambda e: errors.append("pageerror: %s" % e))
    page.on("requestfailed", lambda r: failed.append("%s %s (%s)" % (r.method, r.url, r.failure)))

    def on_resp(r):
        if r.status >= 400 and not (allow_400["on"] and r.status == 400 and "/api/mcast_plans/preview" in r.url):
            try:
                body = r.text()[:200]
            except Exception:
                body = ""
            failed.append("%s %s -> %d %s %s" % (r.request.method, r.url, r.status, body,
                                                 (r.request.post_data or "")[:300]))
    page.on("response", on_resp)
    native = []
    page.on("dialog", lambda d: (native.append(d.message), d.accept()))
    n = [0]

    def shot(label, full_tab=True):
        n[0] += 1
        path = os.path.join(out, "%s-%02d-%s.png" % (name, n[0], label))
        page.wait_for_timeout(150)
        if full_tab:
            page.locator("#set-tab-mcast").screenshot(path=path)
        else:
            page.screenshot(path=path)
        return path

    def click(sel):
        loc = page.locator(sel).first
        loc.scroll_into_view_if_needed()
        loc.tap() if touch else loc.click()

    def settle():
        page.wait_for_timeout(700)          # preview debounce (400 ms) + round trip

    def aligned(step):
        rows = page.evaluate(ALIGN_JS, "#mp-editor .mp-line, #mp-editor .mp-row")
        bad = [r for r in rows if r["dTop"] > 1 or r["dH"] > 1 or r["dText"] > 4]
        check(name, "%s: controls of each line share top and height (%d rows)" % (step, len(rows)),
              not bad, bad)
        small = page.evaluate(TARGETS_JS)
        own = [x["what"] for x in small if not x["shared"]]
        shared = sorted({x["what"] for x in small if x["shared"]})
        check(name, "%s: touch targets >= 34 px" % step, not own, own)
        warn(name, "%s: shared controls (stepper, switch) >= 34 px" % step, not shared, shared)
        clip = page.evaluate(CLIP_JS)
        check(name, "%s: nothing clipped, no horizontal scroll" % step, not clip, clip)

    def one_row(step, at_wide):
        """Item 4: at 1440 a line holds on ONE row; narrower, the ports + free/capacity block
        moves as a whole (one row of its own), and no row ever holds a lone column."""
        lines = page.evaluate(ROWS_JS)
        if at_wide:
            bad = [ln for ln in lines if len(ln["rows"]) != 1]
            check(name, "%s: every line holds on one row (%d lines)" % (step, len(lines)), not bad, bad)
        else:
            bad = [ln for ln in lines if len(ln["tailTops"]) != 1 or any(len(r) < 2 for r in ln["rows"])]
            check(name, "%s: ports + free/capacity move as one block, no lone column (%d lines)"
                  % (step, len(lines)), not bad, bad)

    def retype(sel, text):
        """The operator's way: click the field, select what is there, type over it."""
        loc = page.locator(sel)
        loc.scroll_into_view_if_needed()
        loc.click()
        page.keyboard.press("Control+A")
        page.keyboard.type(text, delay=20)

    page.goto(base + "/settings")
    page.wait_for_load_state("domcontentloaded")
    # Screenshots only: the sticky top bar would cover the element captures.
    page.add_style_tag(content=".topnav{position:static!important}" + inject_css)
    page.evaluate("switchSetTab('mcast')")

    # 1. tab opens; "New plan" disabled while targets/classes load, enabled after
    page.wait_for_timeout(100)
    dis = page.locator("#mp-new-btn").is_disabled()
    op_dis = page.evaluate("getComputedStyle(document.getElementById('mp-new-btn')).opacity")
    cur_dis = page.evaluate("getComputedStyle(document.getElementById('mp-new-btn')).cursor")
    shot("01-loading")
    check(name, "1: New plan disabled while loading", dis)
    page.wait_for_selector("#mp-list table", timeout=10000)
    page.wait_for_function("!document.getElementById('mp-new-btn').disabled", timeout=10000)
    op_en = page.evaluate("getComputedStyle(document.getElementById('mp-new-btn')).opacity")
    warn(name, "1: disabled New plan looks different (opacity %s vs %s, cursor %s)" % (op_dis, op_en, cur_dis),
          op_dis != op_en)
    check(name, "1: seeded plan listed", page.locator("#mp-list tbody tr").count() == 1)
    shot("01-loaded")

    # P. global pool: on THIS tab (not Cluster), loaded, span read-out, saved by its own button
    pool_tab = page.evaluate("['s_mcast_pool_base','s_mcast_pool_size','s_mcast_port_default']"
                             ".map(id => document.getElementById(id)?.closest('.set-tab-content')?.id)")
    check(name, "P: the three pool fields live on the Multicast tab", pool_tab == ["set-tab-mcast"] * 3, pool_tab)
    check(name, "P: no pool field and no cross-link left on Cluster",
          page.evaluate("!document.querySelector('#set-tab-proxmox [id^=s_mcast_]') && "
                        "!document.querySelector('#set-tab-mcast a[onclick*=proxmox]')"))
    vals = [page.input_value(x) for x in ("#s_mcast_pool_base", "#s_mcast_pool_size", "#s_mcast_port_default")]
    check(name, "P: pool loaded from the settings", vals == ["239.100.0.0", "4096", "5000"], vals)
    info = page.inner_text("#mp_pool_info")
    check(name, "P: allocatable span under the base", "239.100.0.1" in info and "239.100.15.255" in info
          and "4095" in info, info)
    retype("#s_mcast_pool_base", "10.1.2.0")
    check(name, "P: a non-multicast base is flagged at once", page.locator("#mp_pool_info.mp-err").count() == 1,
          page.inner_text("#mp_pool_info"))
    click("#mp-pool-save")
    page.wait_for_timeout(300)
    check(name, "P: invalid pool not posted", page.locator("#mp-pool-msg.mp-err").count() == 1,
          page.inner_text("#mp-pool-msg"))
    retype("#s_mcast_pool_base", "239.150.0.0")
    retype("#s_mcast_pool_size", "256")
    retype("#s_mcast_port_default", "5100")
    info = page.inner_text("#mp_pool_info")
    check(name, "P: read-out follows the typing", "239.150.0.1" in info and "239.150.0.255" in info
          and "255" in info, info)
    aligned("P")
    with page.expect_request(lambda r: r.url.endswith("/api/settings") and r.method == "POST") as rq:
        click("#mp-pool-save")
    posted = rq.value.post_data_json
    check(name, "P: save posts the three settings", posted == {"mcast_pool_base": "239.150.0.0",
          "mcast_pool_size": 256, "mcast_port_default": 5100}, posted)
    page.wait_for_function("document.getElementById('mp-pool-msg').textContent.length > 1")
    shot("P-pool-saved")
    page.reload()
    page.wait_for_load_state("domcontentloaded")
    page.add_style_tag(content=".topnav{position:static!important}" + inject_css)
    page.evaluate("switchSetTab('mcast')")
    page.wait_for_function("document.getElementById('s_mcast_pool_base').value !== ''", timeout=10000)
    vals = [page.input_value(x) for x in ("#s_mcast_pool_base", "#s_mcast_pool_size", "#s_mcast_port_default")]
    check(name, "P: after a reload the stored values come back", vals == ["239.150.0.0", "256", "5100"], vals)
    page.evaluate("switchSetTab('proxmox')")
    page.evaluate("enregistrerSettings()")         # the Cluster form must not post the pool any more
    page.wait_for_timeout(400)
    page.reload()
    page.wait_for_load_state("domcontentloaded")
    page.add_style_tag(content=".topnav{position:static!important}" + inject_css)
    page.evaluate("switchSetTab('mcast')")
    page.wait_for_function("document.getElementById('s_mcast_pool_base').value !== ''", timeout=10000)
    page.wait_for_function("!document.getElementById('mp-new-btn').disabled", timeout=10000)
    vals = [page.input_value(x) for x in ("#s_mcast_pool_base", "#s_mcast_pool_size", "#s_mcast_port_default")]
    check(name, "P: saving the Cluster tab leaves the pool as stored", vals == ["239.150.0.0", "256", "5100"], vals)

    # 2. new plan on the network WITHOUT a saved rule: NO dialog; the rule button stands out
    nets = page.evaluate("McastPlans.targets.networks.map(n => [n.id, n.name])")
    n2 = [i for i, nm in nets if nm == "Media net 2"][0]
    n1 = [i for i, nm in nets if nm == "Media net 1"][0]
    click("#mp-new-btn")
    page.wait_for_selector("#mp-editor:not([hidden])")
    page.select_option("#mp_target", str(n1))
    page.wait_for_timeout(300)
    check(name, "2: no rule dialog opens by itself", page.locator("#mp-rule-dlg[open]").count() == 0)
    pend = page.locator(".mp-rule-btn.is-pending")
    want = page.evaluate("T('settings.mcast.rule_btn_choose')")
    check(name, "2: rule button reads 'choose the rule' and stands out",
          pend.count() == 1 and want in pend.inner_text(), page.inner_text(".mp-rule-btn"))
    col = page.evaluate("[getComputedStyle(document.querySelector('.mp-rule-btn')).color,"
                        " getComputedStyle(document.querySelector('.mp-rule-btn')).borderTopColor,"
                        " getComputedStyle(document.documentElement).getPropertyValue('--status-warning-fg')]")
    check(name, "2: pending rule button in the warning colour", col[0] == col[1], col)
    check(name, "2: per-line rule button disabled while the rule is to choose",
          page.locator("#mp-editor .mp-line[data-i='0'] .mp-follow").is_disabled())
    check(name, "2: B read-only, no address-like placeholder",
          page.evaluate("(()=>{const b=document.getElementById('mp_b_0'); return b.readOnly && !/\\d/.test(b.placeholder)})()"))
    hint = page.inner_text("#mp_bi_0")
    check(name, "2: neutral hint under B", hint == page.evaluate("T('settings.mcast.b_rule_pending')"), hint)
    check(name, "2: hint under B in the warning colour (not a grey value)",
          page.evaluate("document.getElementById('mp_bi_0').classList.contains('mp-warn')"))
    click("#mp-editor .mp-given ~ div .btn-green")
    smsg = page.inner_text("#mp-save-msg")
    check(name, "2: save blocked while the rule is to choose",
          smsg == page.evaluate("T('settings.mcast.rule_need_choice')"), smsg)
    settle()
    shot("02-rule-pending")
    click(".mp-rule-btn")
    page.wait_for_selector("#mp-rule-dlg[open]", timeout=3000)
    checked = page.evaluate("[...document.querySelectorAll('input[name=mp_rule_mode]')].filter(x=>x.checked).length")
    check(name, "2: rule dialog opened by the button, nothing preselected", checked == 0, checked)
    delta = page.input_value("#mp_rule_delta")
    check(name, "2: delta field empty on first choice (no hidden default)", delta == "", delta)
    ctr = page.evaluate("(()=>{const r=document.getElementById('mp-rule-dlg').getBoundingClientRect();"
                        "return [r.left + r.width/2 - innerWidth/2, r.top + r.height/2 - innerHeight/2]})()")
    check(name, "2: rule dialog centred in the viewport", abs(ctr[0]) <= 2 and abs(ctr[1]) <= 2, ctr)
    page.locator("#mp-rule-dlg .btn-green").click()
    err = page.inner_text("#mp-rule-err").strip()
    check(name, "2: validating empty shows an error", bool(err), err)
    shot("02-rule-empty-error", full_tab=False)
    # The operator's natural path: type the shift straight away, without picking the option first.
    # The fields were disabled until the radio was chosen, so nothing could be typed.
    check(name, "2: shift field is editable with no option chosen", page.is_enabled("#mp_rule_delta"))
    page.click("#mp_rule_delta")
    page.keyboard.type("100")
    check(name, "2: typing the shift keeps the digits", page.input_value("#mp_rule_delta") == "100",
          page.input_value("#mp_rule_delta"))
    check(name, "2: typing the shift picks 'shift an octet'", page.is_checked("#mp_rule_octet"))
    page.select_option("#mp_rule_oct_n", "2")
    shot("02-rule-octet", full_tab=False)
    page.locator("#mp-rule-dlg .btn-green").click()
    page.wait_for_function("!document.getElementById('mp-rule-dlg').open")
    btn = page.inner_text(".mp-rule-btn")
    check(name, "2: chosen rule shown on the rule button", "+100" in btn and pend.count() == 0, btn)
    check(name, "2: per-line rule button enabled once the rule is chosen",
          not page.locator("#mp-editor .mp-line[data-i='0'] .mp-follow").is_disabled())
    settle()
    shot("02-rule-chosen")

    # 3. network WITH a saved rule: no dialog, rule on the button, colours follow first_fabric
    page.select_option("#mp_target", str(n2))
    page.wait_for_timeout(200)
    check(name, "3: no rule dialog on a network with a saved rule",
          page.locator("#mp-rule-dlg[open]").count() == 0)
    check(name, "3: saved rule shown, not pending", page.locator(".mp-rule-btn.is-pending").count() == 0
          and "+100" in page.inner_text(".mp-rule-btn"))
    fabA = page.evaluate("getComputedStyle(document.querySelector('.mp-fab')).borderTopColor")
    check(name, "3: blue-first network shows Fabric A in blue", fabA == "rgb(59, 130, 246)", fabA)
    check(name, "3: rule button sits between the two fabric boxes",
          page.evaluate("(()=>{const f=[...document.querySelectorAll('.mp-fabrics > *')].map(e=>e.className);"
                        "return f.join(',')})()") == "mp-fab,mp-rule-mid,mp-fab")
    settle()
    shot("03-saved-rule-blue-first")
    page.select_option("#mp_target", str(n1))
    settle()
    check(name, "3: back on the network without rule: the operator's rule is kept",
          "+100" in page.inner_text(".mp-rule-btn"))

    # 4. video / all formats with a /28
    page.select_option("#mp_ess_0", "video")
    page.wait_for_selector("#mp_cls_0")
    check(name, "4: format step offered for video",
          page.evaluate("document.getElementById('mp_cls_0').selectedOptions[0].value") == "video_all")
    page.locator("#mp_a_0").click()
    page.keyboard.type("239.20.1.0/28", delay=40)
    settle()
    foc = page.evaluate("[document.activeElement.id, document.activeElement.selectionStart, document.activeElement.value.length]")
    check(name, "4: focus and caret kept while the preview runs", foc[0] == "mp_a_0" and foc[1] == foc[2], foc)
    info = page.inner_text("#mp_ai_0")
    check(name, "4: read-out under A gives first, last, count",
          "239.20.1.1" in info and "239.20.1.14" in info and "14" in info, info)
    bval = page.input_value("#mp_b_0")
    check(name, "4: range B derived and filled (a value, not a placeholder)", bval == "239.120.1.0/28", bval)
    binfo = page.inner_text("#mp_bi_0")
    check(name, "4: read-out under B", "239.120.1.1" in binfo, binfo)
    cap = page.inner_text("#mp_cap_0")
    check(name, "4: free/capacity filled after the preview", any(c.isdigit() for c in cap), cap)
    aligned("4")
    shot("04-video-28")

    # 5. audio and ANC lines; two-step type selector; per-essence ports
    click("text=" + page.evaluate("T('settings.mcast.line_add')"))
    page.select_option("#mp_ess_1", "audio")
    page.wait_for_selector("#mp_cls_1")
    page.select_option("#mp_cls_1", "audio_8ch_1ms")
    page.fill("#mp_a_1", "239.20.5.0/28")
    click("text=" + page.evaluate("T('settings.mcast.line_add')"))
    page.select_option("#mp_ess_2", "anc")
    page.fill("#mp_a_2", "239.20.9.0/28")
    settle()
    ports = page.evaluate("[0,1,2].map(i => ['video','audio','anc'].filter(k => document.getElementById('mp_'+k+'_'+i)))")
    check(name, "5: one port field per essence", ports == [["video"], ["audio"], ["anc"]], ports)
    check(name, "5: no format step for ANC (single class)", page.locator("#mp_cls_2").count() == 0)
    page.locator("#mp_audio_1").click()
    page.keyboard.type("5004", delay=40)
    settle()
    foc = page.evaluate("document.activeElement.id")
    check(name, "5: port field keeps the focus", foc == "mp_audio_1", foc)
    aligned("5")
    one_row("5", width >= 1440)
    hint = page.inner_text("#mp-ports-hint")
    check(name, "5: ports hint says one port per essence",
          hint == page.evaluate("T('settings.mcast.ports_hint_fixed')"), hint)
    phs = page.evaluate("[...document.querySelectorAll('#mp-editor .mp-port')].map(e => e.placeholder)")
    check(name, "5: no port placeholder that looks like a value", not any(phs), phs)
    shot("05-three-lines")

    # 6. "/xx" <-> "first - last"
    # A bare address (no /xx yet) must survive the toggle: it was wiped.
    kept = page.input_value("#mp_a_0")
    page.fill("#mp_a_0", "239.20.1.9")
    click("#mp-editor .mp-line[data-i='0'] .mp-span-btn")
    a1 = page.input_value("#mp_a1_0")
    check(name, "6: a bare address is carried into 'first'", a1 == "239.20.1.9", a1)
    click("#mp-editor .mp-line[data-i='0'] .mp-span-btn")
    v = page.input_value("#mp_a_0")
    check(name, "6: and comes back as typed", v == "239.20.1.9", v)
    page.fill("#mp_a_0", kept)
    settle()
    click("#mp-editor .mp-line[data-i='0'] .mp-span-btn")
    a1, a2 = page.input_value("#mp_a1_0"), page.input_value("#mp_a2_0")
    check(name, "6: /28 converted to its usable span", (a1, a2) == ("239.20.1.1", "239.20.1.14"), (a1, a2))
    aligned("6 span")
    shot("06-span-converted")
    click("#mp-editor .mp-line[data-i='0'] .mp-span-btn")
    v = page.input_value("#mp_a_0")
    check(name, "6: back to /28", v == "239.20.1.0/28", v)
    click("#mp-editor .mp-line[data-i='1'] .mp-span-btn")
    page.fill("#mp_a1_1", "239.20.5.101")
    page.fill("#mp_a2_1", "239.20.5.200")
    settle()
    info = page.inner_text("#mp_ai_1")
    check(name, "6: free span .101-.200 counts 100", "100" in info, info)
    click("#mp-editor .mp-line[data-i='1'] .mp-span-btn")
    v = page.input_value("#mp_a_1")
    check(name, "6: span without a block brings the previous /xx back", v == "239.20.5.0/28", v)
    click("#mp-editor .mp-line[data-i='1'] .mp-span-btn")
    a1, a2 = page.input_value("#mp_a1_1"), page.input_value("#mp_a2_1")
    check(name, "6: and the typed span comes back untouched", (a1, a2) == ("239.20.5.101", "239.20.5.200"), (a1, a2))
    settle()
    bval = page.input_value("#mp_b_1")
    check(name, "6: B of a span line derived as a span", bval == "239.120.5.101-239.120.5.200", bval)
    aligned("6 free span")
    shot("06-free-span")

    # 7. line 2 B typed by hand
    click("#mp-editor .mp-line[data-i='2'] .mp-follow")
    ro = page.evaluate("document.getElementById('mp_b_2').readOnly")
    check(name, "7: B editable once typed by hand", not ro)
    page.fill("#mp_b_2", "239.99.9.0/28")
    settle()
    check(name, "7: manual B kept after the preview", page.input_value("#mp_b_2") == "239.99.9.0/28")
    aligned("7")
    one_row("7 busiest lines", width >= 1440)
    shot("07-b-by-hand")

    # 8. overlapping ranges: error shown near "What this gives"
    allow_400["on"] = True
    page.fill("#mp_a_2", "239.20.1.0/29")
    settle()
    msg = page.inner_text("#mp-given")
    vis = page.evaluate("(()=>{const e=document.querySelector('#mp-given .mp-err'); if(!e) return false;"
                        "const c=getComputedStyle(e).color; return c})()")
    check(name, "8: overlap error shown in What this gives", bool(vis) and msg.strip() != "", (vis, msg))
    shot("08-overlap-error")
    allow_400["on"] = False
    page.fill("#mp_a_2", "239.30.0.16/28")       # overlaps the OTHER plan: warning, not an error
    settle()
    check(name, "8: overlap with another plan warned", page.locator("#mp-given .mp-warn").count() >= 1)
    shot("08-overlap-other-plan")
    page.fill("#mp_a_2", "239.20.9.0/28")
    settle()

    # 9. what this gives, several states, unmatched stands out
    states = page.evaluate("[...document.querySelectorAll('#mp-given .mp-st')].map(e => e.className.split('mp-st-')[1])")
    check(name, "9: flows in several states", len(set(states)) >= 3, sorted(set(states)))
    page.select_option("#mp_cls_1", "audio_2ch_1ms")         # audio no longer matches: unmatched rows
    settle()
    um = page.evaluate("(()=>{const r=document.querySelector('#mp-given tr.mp-row-unmatched > td'); if(!r) return null;"
                       "const n=document.querySelector('#mp-given tbody tr:not(.mp-row-unmatched) > td');"
                       "return [getComputedStyle(r).backgroundColor, n ? getComputedStyle(n).backgroundColor : null]})()")
    check(name, "9: unmatched row stands out (row background differs)", um and um[0] != um[1], um)
    shot("09-given-states")

    # A. active outputs vs provisioned ones (engine: 3 active out of 8)
    page.select_option("#mp_cls_1", "audio_8ch_1ms")
    settle()
    head = page.inner_text("#mp-given .mp-given-head")
    want_a = page.evaluate("T('settings.mcast.given_active').replace('{n}', 12).replace('{o}', 3)")
    want_i = page.evaluate("T('settings.mcast.given_inactive').replace('{n}', 20).replace('{o}', 5)")
    check(name, "A: summary: flows on 3 active outputs, then the 5 provisioned ones",
          head.startswith(want_a + " · " + want_i), head)
    top_rows = page.locator("#mp-given > table tbody tr").count()
    det = page.locator("#mp-given details.mp-inactive-d")
    check(name, "A: only the 12 flows of the active outputs in the main table", top_rows == 12, top_rows)
    check(name, "A: provisioned outputs folded, closed by default",
          det.count() == 1 and not page.evaluate("document.querySelector('#mp-given details').open"))
    check(name, "A: folded rows are TX4..TX8", page.evaluate(
        "[...document.querySelectorAll('#mp-given details tbody tr td:first-child')].map(e=>e.textContent)"
        ".every(t => /^TX[4-8] /.test(t))"))
    check(name, "A: no shortage message with enough addresses",
          page.locator("#mp-given .mp-short, #mp-given .mp-short-info").count() == 0)
    shot("A-active-first")
    click("#mp-given details.mp-inactive-d > summary")
    page.wait_for_timeout(200)
    check(name, "A: the operator opens the provisioned outputs", page.evaluate("document.querySelector('#mp-given details').open"))
    retype("#mp_a_0", "239.20.1.0/29")          # 5 addresses for 8 videos: provisioned ones short
    settle()
    check(name, "A: details stay open while the preview refreshes",
          page.evaluate("document.querySelector('#mp-given details')?.open") is True)
    info = page.locator("#mp-given .mp-short-info")
    check(name, "A: shortage on provisioned outputs only = information, not an error",
          info.count() == 1 and page.locator("#mp-given .mp-short").count() == 0,
          page.inner_text("#mp-given .mp-given-head"))
    check(name, "A: provisioned rows left without address are not painted as errors",
          page.locator("#mp-given details tr.mp-row-unmatched").count() == 0
          and page.locator("#mp-given details .mp-st-full.is-info").count() >= 1)
    shot("A-short-provisioned")
    retype("#mp_a_0", "239.20.1.0/31")          # no address left beyond what TX1 holds
    settle()
    err = page.locator("#mp-given .mp-short.mp-err")
    colours = page.evaluate("(()=>{const e=document.querySelector('#mp-given .mp-short'); if(!e) return null;"
                            "const c=getComputedStyle(e); return [c.color, c.borderTopColor, c.backgroundColor,"
                            "getComputedStyle(document.querySelector('#mp-given .mp-given-head')).color]})()")
    check(name, "A: shortage on ACTIVE outputs = error that stands out (border, background, colour)",
          err.count() == 1 and colours and colours[0] == colours[1] and colours[0] != colours[3]
          and colours[2] not in ("rgba(0, 0, 0, 0)", "transparent"), colours)
    check(name, "A: the active flow left without address is in the main table, marked",
          page.locator("#mp-given > table .mp-st-full").count() >= 1)
    shot("A-short-active", full_tab=True)
    retype("#mp_a_0", "239.20.1.0/28")
    settle()

    # 10. pair, then interface without pair
    page.select_option("#mp_scope", "pair")
    settle()
    check(name, "10: pair shows both fabrics with their interfaces",
          page.locator(".mp-fab").count() == 2 and "ensR" in page.inner_text(".mp-fabrics"))
    aligned("10 pair")
    shot("10-pair")
    page.select_option("#mp_scope", "interface")
    settle()
    check(name, "10: interface without pair: one fabric, no rule button, no B",
          page.locator(".mp-fab").count() == 1 and page.locator(".mp-rule-btn").count() == 0
          and page.locator("[id^=mp_b_]").count() == 0 and page.locator(".mp-follow").count() == 0)
    heads = page.evaluate("[...document.querySelectorAll('#mp-given th')].map(e=>e.textContent)")
    check(name, "10: no B column", "B" not in heads, heads)
    aligned("10 interface")
    shot("10-interface")

    # 11. save, list, edit (PUT), delete
    page.select_option("#mp_scope", "network")
    page.select_option("#mp_target", str(n1))
    page.select_option("#mp_cls_1", "audio_8ch_1ms")
    page.fill("#mp_label", "Bench plan")
    settle()
    # Operator's path: save with the video and ANC ports left empty (fixed mode, default).
    posts = []
    page.on("request", lambda r: posts.append(r.url) if r.method in ("POST", "PUT") and r.url.endswith("/api/mcast_plans") else None)
    click("#mp-editor .mp-given ~ div .btn-green")
    page.wait_for_timeout(400)
    errs = page.evaluate("[...document.querySelectorAll('#mp-editor [id*=_err_]')].map(e => e.id)")
    check(name, "B: save refused while a fixed port is missing (nothing posted)", not posts, posts)
    check(name, "B: error under the video port of line 1 and the ANC port of line 3, not elsewhere",
          sorted(errs) == ["mp_anc_err_2", "mp_video_err_0"], errs)
    check(name, "B: focus on the first missing port", page.evaluate("document.activeElement.id") == "mp_video_0",
          page.evaluate("document.activeElement.id"))
    check(name, "B: the save message says why", page.inner_text("#mp-save-msg")
          == page.evaluate("T('settings.mcast.port_required_save')"), page.inner_text("#mp-save-msg"))
    aligned("B errors")
    one_row("B errors", width >= 1440)
    shot("B-ports-missing")
    page.keyboard.type("2120", delay=30)
    check(name, "B: typing the port clears its error, focus kept",
          page.locator("#mp_video_err_0").count() == 0 and page.evaluate("document.activeElement.id") == "mp_video_0")
    retype("#mp_anc_2", "2140")
    check(name, "B: every port given: the message goes", page.inner_text("#mp-save-msg").strip() == "",
          page.inner_text("#mp-save-msg"))
    settle()
    click("#mp-editor .mp-given ~ div .btn-green")
    page.wait_for_function("document.querySelectorAll('#mp-list tbody tr').length === 2", timeout=5000)
    check(name, "11: saved plan appears in the list", "Bench plan" in page.inner_text("#mp-list"))
    shot("11-saved")
    click("#mp-list tbody tr:has-text('Bench plan') button >> nth=0")
    page.wait_for_selector("#mp-editor:not([hidden])")
    check(name, "11: edit does not ask the rule again", page.locator("#mp-rule-dlg[open]").count() == 0)
    page.fill("#mp_label", "Bench plan edited")
    settle()
    shot("11-edit")
    click("#mp-editor .btn-green")
    page.wait_for_function("document.getElementById('mp-list').textContent.includes('Bench plan edited')", timeout=5000)
    check(name, "11: PUT saved", True)
    click("#mp-list tbody tr:has-text('Bench plan edited') button >> nth=1")
    page.wait_for_timeout(300)
    page.wait_for_selector(".modal-overlay [data-act=ok]", timeout=3000)
    check(name, "11: delete asks through the shared confirmation, not the browser's", True)
    shot("11-delete-confirm", full_tab=False)
    page.locator(".modal-overlay [data-act=ok]").click()
    page.wait_for_function("document.querySelectorAll('#mp-list tbody tr').length === 1", timeout=5000)
    check(name, "11: deleted", "Bench plan" not in page.inner_text("#mp-list"))
    shot("11-deleted")

    # 12. slot rank on an "all essences" line: offsets disclosure
    click("#mp-new-btn")
    page.wait_for_timeout(300)
    check(name, "12: network now has a saved rule: no dialog", page.locator("#mp-rule-dlg[open]").count() == 0)
    page.select_option("#mp_ports", "increment")
    hint = page.inner_text("#mp-ports-hint")
    check(name, "B: increment mode says the port comes from the engine",
          hint == page.evaluate("T('settings.mcast.ports_hint_increment')"), hint)
    page.select_option("#mp_rank", "slot")
    # S. slot rank on a small AUDIO line: the 2nd audio of each output falls outside the line
    page.select_option("#mp_target", str(n1))      # the network of the engine
    page.select_option("#mp_ess_0", "audio")
    retype("#mp_a_0", "239.20.40.0/28")
    settle()
    out_err = page.locator("#mp-given .mp-short.mp-err")
    check(name, "S: flows outside their line = error on the active outputs",
          out_err.count() == 1 and page.locator("#mp-given > table .mp-st-outside").count() == 3,
          page.inner_text("#mp-given .mp-given-head"))
    check(name, "S: the audio step field is offered on that line, opened by the preview",
          page.evaluate("document.querySelector('.mp-line[data-i=\"0\"] .mp-step-d')?.open") is True)
    aligned("S")
    shot("S-audio-outside")
    retype("#mp_off_ip_step_audio_0", "4")
    settle()
    check(name, "S: with a step that fits, the error goes",
          page.locator("#mp-given .mp-short").count() == 0 and page.locator("#mp-given .mp-st-outside").count() == 0)
    check(name, "S: the step field kept the focus", page.evaluate("document.activeElement.id") == "mp_off_ip_step_audio_0")
    page.select_option("#mp_ess_0", "")
    page.wait_for_selector("details.mp-offsets-d")
    page.locator("details.mp-offsets-d > summary").click()
    page.fill("#mp_a_0", "239.20.40.0/24")
    settle()
    aligned("12")
    offs = page.evaluate(ALIGN_JS, "#mp-editor .mp-offsets")
    sel_h = page.evaluate("[...document.querySelectorAll('#mp-editor select')].map(e => Math.round(e.getBoundingClientRect().height))")
    check(name, "12: every select of the editor is 34 px high", all(h == 34 for h in sel_h), sel_h)
    check(name, "12: offsets fields aligned", all(r["dTop"] <= 1 and r["dH"] <= 1 for r in offs), offs)
    shot("12-slot-offsets")

    known = [e for e in errors if any(k in e for k in KNOWN_UNRELATED)]
    if known:
        print("  [WARN] %d known error(s) outside the editor: %s" % (len(known), known[0][:120]))
    errors = [e for e in errors if e not in known]
    check(name, "no native alert/confirm", not native, native)
    check(name, "no console error", not errors, errors)
    check(name, "no failed request", not failed, failed)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join("/tmp", "banc_ui_mcast_shots"))
    ap.add_argument("--engines", default="chromium", help="comma list: chromium,webkit")
    ap.add_argument("--delay", type=float, default=1.5, help="seconds the targets API is held back")
    ap.add_argument("--inject-css", default="", help="extra CSS (mutation check: break something, "
                    "the bench must go red)")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    with sync_playwright() as pw:
        for eng in a.engines.split(","):
            for w, h, touch in ((1440, 900, False), (1024, 768, True)):
                try:
                    run(pw, eng, w, h, touch, a.out, a.delay, a.inject_css)
                except Exception as e:
                    FAILS.append("%s-%d: exception %s" % (eng, w, str(e).splitlines()[0][:300]))
                    print("  [KO] exception: %s" % e)
    print("\nscreenshots in %s" % a.out)
    print("%d warning(s) (shared catalogue)" % len(WARNS))
    for w in sorted(set(x.split(": ", 1)[1] for x in WARNS)):
        print("  - " + w)
    print("%d failure(s)" % len(FAILS))
    for f in FAILS:
        print("  - " + f)
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
