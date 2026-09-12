// SPDX-License-Identifier: GPL-3.0-or-later
// Copyright (C) 2026 BOBI SAS, France
// Éditeur de code minimal, autonome (aucune dépendance) : numéros de ligne + coloration
// syntaxique légère par overlay (textarea transparent au-dessus d'un <pre> coloré).
// Usage : const ed = CodeEditor.enhance(textareaEl); ed.setLang('py'); ed.setValue(s); ed.getValue();
(function () {
  const KW = {
    py: 'def class return if elif else for while in is not and or import from as with try except finally raise pass break continue lambda yield global nonlocal None True False self async await',
    js: 'function return if else for while var let const new class extends import from export default try catch finally throw typeof instanceof null true false undefined await async this',
    sh: 'if then else elif fi for while do done case esac function in echo export local return exit set unset',
  };
  function kwRe(words){ return new RegExp('\\b(' + words.trim().split(/\s+/).join('|') + ')\\b', 'y'); }
  const RULES = {
    json: [[/"(?:\\.|[^"\\])*"/y,'str'],[/-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?/y,'num'],[/\b(?:true|false|null)\b/y,'kw']],
    py:   [[/#.*/y,'com'],[/'''[\s\S]*?'''|"""[\s\S]*?"""/y,'str'],[/'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*"/y,'str'],[/\b\d+(?:\.\d+)?\b/y,'num'],[kwRe(KW.py),'kw']],
    js:   [[/\/\/.*/y,'com'],[/\/\*[\s\S]*?\*\//y,'com'],[/`(?:\\.|[^`\\])*`|'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*"/y,'str'],[/\b\d+(?:\.\d+)?\b/y,'num'],[kwRe(KW.js),'kw']],
    sh:   [[/#.*/y,'com'],[/'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*"/y,'str'],[/\$\{?\w+\}?/y,'var'],[kwRe(KW.sh),'kw']],
    xml:  [[/<!--[\s\S]*?-->/y,'com'],[/<\/?[\w:.-]+/y,'kw'],[/"(?:\\.|[^"\\])*"/y,'str'],[/[\w:-]+=/y,'var']],
    ini:  [[/[#;].*/y,'com'],[/^\s*\[[^\]]*\]/ym,'kw'],[/"(?:\\.|[^"\\])*"/y,'str']],
  };
  const EXT2LANG = {'.json':'json','.py':'py','.js':'js','.mjs':'js','.sh':'sh','.bash':'sh',
    '.xml':'xml','.html':'xml','.htm':'xml','.svg':'xml','.ini':'ini','.conf':'ini','.cfg':'ini','.toml':'ini'};
  function esc(s){ return s.replace(/[&<>]/g, c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c])); }
  function highlight(code, lang){
    const rules = RULES[lang];
    if (!rules || code.length > 120000) return esc(code);   // gros fichier → pas de coloration
    let i=0, out='';
    while (i < code.length){
      let matched=false;
      for (const [re,cls] of rules){
        re.lastIndex=i; const m=re.exec(code);
        if (m && m.index===i && m[0].length){ out+='<span class="ce-'+cls+'">'+esc(m[0])+'</span>'; i+=m[0].length; matched=true; break; }
      }
      if (!matched){ out+=esc(code[i]); i++; }
    }
    return out;
  }

  function enhance(ta){
    const wrap=document.createElement('div'); wrap.className='ce-wrap';
    const gutter=document.createElement('div'); gutter.className='ce-gutter';
    const pre=document.createElement('pre'); pre.className='ce-pre'; const codeEl=document.createElement('code'); pre.appendChild(codeEl);
    ta.parentNode.insertBefore(wrap, ta); wrap.appendChild(gutter); wrap.appendChild(pre); wrap.appendChild(ta);
    ta.classList.add('ce-input'); ta.spellcheck=false; ta.setAttribute('autocomplete','off'); ta.setAttribute('autocapitalize','off'); ta.setAttribute('wrap','off');
    let lang=null;
    function refresh(){
      const v=ta.value;
      codeEl.innerHTML=highlight(v, lang) + '\n';
      const n=v.split('\n').length;
      let g=''; for (let i=1;i<=n;i++) g+=i+'\n'; gutter.textContent=g;
    }
    function sync(){ pre.scrollTop=ta.scrollTop; pre.scrollLeft=ta.scrollLeft; gutter.scrollTop=ta.scrollTop; }
    ta.addEventListener('input', refresh);
    ta.addEventListener('scroll', sync);
    ta.addEventListener('keydown', function(e){
      if (e.key==='Tab'){ e.preventDefault(); const s=ta.selectionStart, en=ta.selectionEnd;
        ta.value=ta.value.slice(0,s)+'  '+ta.value.slice(en); ta.selectionStart=ta.selectionEnd=s+2; refresh(); }
    });
    const api={
      setLang(l){ lang=l; refresh(); },
      setLangByName(name){ const ext=(name||'').slice((name||'').lastIndexOf('.')); api.setLang(EXT2LANG[ext.toLowerCase()]||null); },
      setValue(v){ ta.value=v||''; refresh(); sync(); },
      getValue(){ return ta.value; },
      setReadOnly(ro){ ta.readOnly=!!ro; },
      el: ta,
    };
    refresh();
    return api;
  }
  window.CodeEditor = { enhance };
})();
