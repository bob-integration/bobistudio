/* SPDX-License-Identifier: GPL-3.0-or-later
   Copyright (C) 2026 BOBI SAS, France

   MIROIR NAVIGATEUR de `app/auth.py:valider_motdepasse`. Rend la même liste de clés de règles.

   ⚠ CE FICHIER NE PROTÈGE RIEN. La règle qui fait foi est celle du serveur : n'importe qui peut
   poster sur l'API sans passer par cette page. Il existe pour que l'utilisateur voie CE QUI
   MANQUE pendant qu'il tape, au lieu de deviner après un refus.

   ⚠ DEUX IMPLÉMENTATIONS DE LA MÊME RÈGLE DÉRIVENT. Confrontation :
       ./venv/bin/python tests/check_motdepasse.py
   qui fait tourner les deux sur le même corpus et compare verdict par verdict. */
(function (racine) {
    "use strict";

    // Défauts = profil « standard ». Les seuils RÉELS arrivent en dernier argument, posés par
    // le gabarit depuis `auth.pwd_exigences()` : les deux côtés lisent alors le même réglage,
    // et un changement de profil n'a pas à être recopié ici.
    const EXIGENCES_DEFAUT = {longueur_min: 12, variete: [[12, 3], [16, 2], [20, 1]]};
    const INTERDITS = new Set([
        "password", "motdepasse", "azerty", "qwerty", "administrateur", "admin", "root",
        "bobi", "bobistudio", "bobi studio", "changeme", "changermoi", "secret", "letmein",
        "welcome", "bienvenue", "iloveyou", "monkey", "dragon", "soleil", "console", "regie"
    ]);
    const SUITES = ["abcdefghijklmnopqrstuvwxyz", "0123456789",
                    "azertyuiop", "qwertyuiop", "qsdfghjklm", "asdfghjkl", "wxcvbn", "zxcvbn"];

    function classes(p) {
        let n = 0;
        if (/[a-zà-ÿ]/.test(p)) n++;
        if (/[A-ZÀ-Þ]/.test(p)) n++;
        if (/[0-9]/.test(p)) n++;
        if (/[^0-9A-Za-zÀ-ÿ]/.test(p)) n++;
        return n;
    }

    // Forme comparable : minuscules, sans espaces, sans chiffres de fin. `bobi2026` et
    // `bobi2027` sont le même mot de passe.
    function noyau(t) {
        const s = String(t || "").toLowerCase().replace(/\s+/g, "");
        return s.replace(/[0-9]+$/, "") || s;
    }

    function valider(pwd, username, extras, exigences) {
        pwd = pwd || "";
        const ex = exigences || racine.PWD_EXIGENCES || EXIGENCES_DEFAUT;
        const lmin = ex.longueur_min, VARIETE = ex.variete;
        const fautes = [];
        if (pwd.length < lmin) fautes.push("court");

        let besoin = VARIETE[VARIETE.length - 1][1];
        for (const [seuil, n] of VARIETE) { if (pwd.length < seuil) { besoin = n; break; } }
        if (pwd.length >= lmin && classes(pwd) < besoin) fautes.push("variete");

        const n = noyau(pwd);
        if (INTERDITS.has(n)) fautes.push("courant");

        // `n` doit faire 3 signes : la chaîne vide est sous-chaîne de tout.
        if (n.length >= 3) {
            for (const ident of [username].concat(extras || [])) {
                const i = noyau(ident || "");
                if (i.length < 3) continue;
                if (n.indexOf(i) >= 0 || i.indexOf(n) >= 0) { fautes.push("identite"); break; }
            }
        }

        if (pwd && new Set(pwd).size <= 2) { fautes.push("repetitif"); }
        else {
            const bas = pwd.toLowerCase();
            let vu = false;
            for (const suite of SUITES) {
                for (let i = 0; i < suite.length - 3; i++) {
                    const bout = suite.slice(i, i + 4);
                    const env = bout.split("").reverse().join("");
                    if (bas.indexOf(bout) >= 0 || bas.indexOf(env) >= 0) { vu = true; break; }
                }
                if (vu) break;
            }
            if (vu) fautes.push("repetitif");
        }
        return fautes;
    }

    valider.EXIGENCES_DEFAUT = EXIGENCES_DEFAUT;
    valider.REGLES = ["court", "variete", "courant", "identite", "repetitif"];
    racine.validerMotDePasse = valider;
    if (typeof module !== "undefined" && module.exports) module.exports = valider;
})(typeof window !== "undefined" ? window : globalThis);
