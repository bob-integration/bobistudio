# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Lecture d'un SDP ST 2110 — ce que la source DÉCLARE, et rien d'autre.

## Pourquoi ce module s'appelle « déclaré » et pas « format »

⚠ **UN SDP EST UNE INTENTION, PAS UNE MESURE.** Tout ce qu'on lit ici est une affirmation de
l'émetteur sur lui-même. Aucune de ces valeurs n'a été vérifiée en arrivant sur le fil, et
plusieurs ne peuvent pas l'être du tout. Les afficher comme des constats serait exactement la
faute que le projet traque partout ailleurs : présenter une intention comme un fait.

D'où la structure du retour : chaque champ porte, en plus de sa valeur, **la façon dont il peut
être confronté**. Trois cas, et il n'y en a pas d'autre :

| `verif` | ce que ça veut dire |
|---|---|
| `"mesure"` | une grandeur mesurée existe en face — on affiche la CONFRONTATION, jamais la valeur seule |
| `"symptome"` | rien ne la mesure, mais un désaccord produit un symptôme nommable (trames non assemblées) |
| `"aucune"` | rien dans le paquet RTP ne la porte. On répète la source, et on le DIT |

Le troisième cas est le plus important à ne pas oublier. `colorimetry` en fait partie — et c'est
sur elle que le reste du produit calcule le gamut et la parade. Un `BT709` faux fausse tout ça
sans que rien ne bronche : la seule protection honnête est d'écrire qu'on ne l'a pas vérifié.

## Le payload type, cas d'école

`ops.payload_type` est passé à libmtl comme **filtre**. Une source qui émet un PT différent de
celui du SDP voit tous ses paquets jetés : zéro trame, « pas de signal », alors que le flux
arrive sur le port. Le désaccord EXISTE, il produit un symptôme, et ce symptôme ne se nomme pas.
C'est pour ça qu'il est classé `"symptome"` et pas `"mesure"` tant que le moteur ne publie pas
le PT réellement reçu.
"""
import re

# Ce que chaque champ déclaré peut affronter. Table EXPLICITE et pas une heuristique : un champ
# oublié ici doit apparaître comme non classé plutôt que de se faire passer pour vérifié.
CONFRONTATION = {
    "fps":          ("mesure",   "cadence mesurée par le moteur"),
    "tp":           ("mesure",   "Cinst et VRX — le gabarit 2110-21 dit si la classe est tenue"),
    "gm":           ("mesure",   "le grandmaster sur lequel le moteur est verrouillé"),
    "domaine_ptp":  ("mesure",   "le domaine PTP du moteur"),
    "legs":         ("mesure",   "le nombre de pattes réellement reçues"),
    "payload_type": ("symptome", "filtre de libmtl : un écart donne zéro trame, pas une erreur"),
    "width":        ("symptome", "configure le récepteur : un écart donne des trames non assemblées"),
    "height":       ("symptome", "idem"),
    "depth":        ("symptome", "change la taille du pgroup : un écart casse l'assemblage"),
    "sampling":     ("symptome", "idem"),
    "colorimetrie": ("aucune",   "rien dans le paquet RTP ne la porte"),
    "tcs":          ("aucune",   "rien dans le paquet RTP ne la porte"),
    "range":        ("aucune",   "rien dans le paquet RTP ne la porte"),
    "ssn":          ("aucune",   "version de norme annoncée, invérifiable"),
    "pm":           ("aucune",   "mode de paquetisation annoncé"),
    "mediaclk":     ("aucune",   "décalage d'horloge média annoncé"),
}


def _fmtp(txt):
    """Paramètres du `a=fmtp:` — `clé=valeur` séparés par des points-virgules."""
    out = {}
    for m in re.finditer(r"^a=fmtp:\d+\s+(.*)$", txt, re.M):
        for morceau in m.group(1).split(";"):
            morceau = morceau.strip()
            if not morceau:
                continue
            if "=" in morceau:
                k, v = morceau.split("=", 1)
                out[k.strip()] = v.strip()
            else:
                # Certains paramètres sont des DRAPEAUX sans valeur (`interlace`). Les jeter
                # ferait passer un flux entrelacé pour progressif — un défaut d'une trame.
                out[morceau] = True
    return out


def lire(sdp):
    """Décompose un SDP en ce que la source DÉCLARE. Rend None si le texte n'est pas un SDP.

    Le retour porte les valeurs ET, dans `confrontation`, la façon dont chacune peut être
    vérifiée. L'appelant n'a donc aucune excuse pour afficher une valeur sans dire son statut."""
    if not sdp or "v=0" not in sdp:
        return None
    f = _fmtp(sdp)
    mc = re.findall(r"^c=IN IP4 ([\d.]+)", sdp, re.M)
    ports = re.findall(r"^m=(\w+)\s+(\d+)\s+RTP/AVP\s+(\d+)", sdp, re.M)
    refclk = re.findall(r"ts-refclk:ptp=IEEE1588-2008:([0-9A-Fa-f\-]+):(\d+)", sdp)
    src = re.findall(r"source-filter:\s*incl IN IP4 [\d.]+ ([\d.]+)", sdp)
    fps = f.get("exactframerate")
    if isinstance(fps, str) and "/" in fps:
        # `30000/1001` : on garde la fraction ET sa valeur, parce que 29,97 arrondi à 30
        # ferait conclure à un désaccord là où il n'y en a pas.
        a, b = fps.split("/", 1)
        try:
            fps_val = float(a) / float(b)
        except (ValueError, ZeroDivisionError):
            fps_val = None
    else:
        try:
            fps_val = float(fps) if fps else None
        except ValueError:
            fps_val = None
    d = {
        "nom": (re.findall(r"^s=(.*)$", sdp, re.M) or [""])[0].strip() or None,
        "essence": ports[0][0] if ports else None,
        "payload_type": int(ports[0][2]) if ports else None,
        "udp_port": int(ports[0][1]) if ports else None,
        "mcast": mc[:2],
        "source_ip": src[0] if src else None,
        # `a=group:DUP` = la source PROPOSE la redondance 2022-7. Qu'on s'y abonne ou non est
        # une autre question, et c'est justement la confrontation qui a de la valeur.
        "dup": "group:DUP" in sdp,
        "legs": len(mc),
        "gm": refclk[0][0] if refclk else None,
        "domaine_ptp": int(refclk[0][1]) if refclk else None,
        "mediaclk": (re.findall(r"mediaclk:(\S+)", sdp) or [None])[0],
        "width": int(f["width"]) if str(f.get("width", "")).isdigit() else None,
        "height": int(f["height"]) if str(f.get("height", "")).isdigit() else None,
        "fps": fps_val,
        "fps_texte": fps if isinstance(fps, str) else None,
        "interlace": bool(f.get("interlace")),
        "depth": int(f["depth"]) if str(f.get("depth", "")).isdigit() else None,
        "sampling": f.get("sampling"),
        "colorimetrie": f.get("colorimetry"),
        "tcs": f.get("TCS"),
        "range": f.get("RANGE"),
        "pm": f.get("PM"),
        "ssn": f.get("SSN"),
        "tp": f.get("TP"),
        # Audio : le nombre de canaux et le temps de paquet sont dans `rtpmap`/`ptime`.
        "canaux": None,
        "ptime": None,
    }
    rt = re.findall(r"^a=rtpmap:\d+\s+L(\d+)/(\d+)/(\d+)", sdp, re.M)
    if rt:
        d["depth"] = int(rt[0][0])
        d["canaux"] = int(rt[0][2])
    pt = re.findall(r"^a=ptime:([\d.]+)", sdp, re.M)
    if pt:
        d["ptime"] = float(pt[0])
    d["confrontation"] = {k: {"comment": v[0], "quoi": v[1]}
                          for k, v in CONFRONTATION.items() if d.get(k) is not None}
    return d
