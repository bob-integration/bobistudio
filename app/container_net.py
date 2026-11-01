# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 BOBI SAS, France
# Auteur : Cyril Mazouer, pour le compte de BOBI SAS
# Distribué sous licence GNU GPL v3 (ou ultérieure) ; voir le fichier LICENSE.

"""Réseau des conteneurs : les deux MODES, et le shim macvlan du nœud tout-en-un.

Deux modes par nœud (`nodes.container_net_mode`, vocabulaire `database.NET_MODES`) :

- « **réseau du site** » (`site`, macvlan) — chaque conteneur est une machine du LAN, avec son
  IP dessus. C'est l'historique.
- « **réseau interne** » (`interne`, bridge Docker privé) — subnet privé au nœud, joint
  nativement par l'hôte, sortie par NAT.

★ LE PROBLÈME QUE CE MODULE RÉSOUT (constaté par le testeur le 2026-10-27, serveur tout-en-un).
Le noyau Linux ISOLE une interface parente de ses enfants macvlan : un paquet émis par l'hôte
depuis `eno1` ne redescend jamais vers un enfant macvlan de `eno1`, et réciproquement. Sur un
serveur tout-en-un, l'orchestrateur EST sur l'hôte parent — il ne joint donc aucun de ses
conteneurs, alors que `docker ps` les dit « running », que l'agent du nœud va bien, et qu'un
autre poste du LAN, lui, les joint parfaitement. Rien n'échoue : tout est vert et rien ne marche.

Le remède est un **shim** : une interface macvlan de PLUS, posée côté hôte sur le même parent.
Les enfants macvlan se parlent entre eux ; l'hôte passe donc par le shim pour atteindre ses
conteneurs. C'est le « shim macvlan requis » que `routes/node_network.py` annonçait déjà dans
son avertissement, sans que personne ne le pose.

⚠ ET VOICI CE QUI REND SA ROUTE DÉLICATE — à lire avant de toucher `script_shim()`.
La tentation est de router le SUBNET ENTIER par le shim (`ip route add 192.0.2.0/24 dev
bobi-shim`). En mode fusionné, ce subnet est le LAN DU SITE : cette route détournerait vers le
shim le routeur, les autres serveurs, les pupitres, les postes de montage — tout le site. Le
nœud perdrait son réseau, et le geste qui l'a cassé n'aurait rien à voir avec le symptôme.
On ne route donc QUE les adresses réellement attribuées aux conteneurs (`ip_start..ip_end`,
résumé en CIDR minimaux), jamais le subnet. Garde : `tests/verif_shim_macvlan.py`.
"""
import ipaddress
import logging
import shlex

from .database import node_net_mode, node_net_interne, NET_MODE_SITE, NET_MODE_INTERNE

log = logging.getLogger(__name__)

# Nom de l'interface shim et des objets persistants qui vont avec. Fixe : un nœud n'a qu'un
# réseau de conteneurs (`nodes.docker_network`), donc qu'un shim. 15 caractères maximum
# (IFNAMSIZ-1) — « bobi-shim » en fait 9.
SHIM_IFACE = "bobi-shim"
SHIM_SCRIPT = "/usr/local/sbin/bobi-shim.sh"
SHIM_UNIT = "bobi-shim"

__all__ = ["SHIM_IFACE", "SHIM_SCRIPT", "SHIM_UNIT", "NET_MODE_SITE", "NET_MODE_INTERNE",
           "cidrs_de_plage", "est_tout_en_un", "shim_requis", "mode_interne_possible",
           "pool_hors_subnet",
           "shim_ip_suggeree", "script_shim",
           "script_retrait_shim", "appliquer_shim", "retirer_shim", "etat_shim",
           "iface_internet", "sortie_possible", "script_sortie", "script_retrait_sortie",
           "ips_sortie_internet", "appliquer_sortie", "poser_sortie", "etat_sortie"]


def cidrs_de_plage(start, end):
    """Plage `start..end` → liste MINIMALE de CIDR la couvrant exactement. [] si bornes invalides.

    C'est la pièce qui empêche le shim de capturer le LAN : on route ces CIDR-là, pas le subnet.
    `summarize_address_range` ne déborde jamais de la plage : 192.0.2.100-199 donne cinq
    routes — 100/30, 104/29, 112/28, 128/26, 192/29 — et pas une adresse du site dedans
    (mesuré le 2026-10-27, c'est la sortie réelle de la fonction, pas une estimation).
    """
    try:
        a = ipaddress.IPv4Address(str(start).strip())
        b = ipaddress.IPv4Address(str(end).strip())
    except Exception as e:
        log.warning("cidrs_de_plage(%s, %s): %s", start, end, e)
        return []
    if int(a) > int(b):
        a, b = b, a
    return [str(n) for n in ipaddress.summarize_address_range(a, b)]


def est_tout_en_un(node, controller_ips=None):
    """Ce nœud porte-t-il AUSSI l'orchestrateur ? (une seule définition, deux usages)

    ★ LE DÉFAUT QUE CETTE FONCTION EXISTE POUR NE PLUS REFAIRE (trouvé par la session Z8 le
    2026-10-27, après déploiement). Les deux appelants comparaient `node["host"]` aux IP du
    contrôleur. Or un tout-en-un installé par `install/install.py` est enrôlé sous **127.0.0.1**
    (c'est l'installeur lui-même qui appelle `register('127.0.0.1', 9100, …)`), et l'adresse de
    bouclage ne figure JAMAIS dans `controller_ipv4s()`. Conséquence : TOUT tout-en-un installé
    normalement était classé « orchestrateur ailleurs » — donc pas de shim et mode interne
    refusé, exactement le cas que ce chantier devait réparer.

    Et il n'a pas été pris par mes bancs parce que je les avais écrits avec des hôtes
    PLAUSIBLES (« 192.0.2.5 ») au lieu de la valeur que le produit PRODUIT. Une garde qui
    s'invente ses données ne mesure que l'abstraction : cf. [[garde-verte-du-premier-coup-doit-etre-mutee]].

    La boucle locale est donc décisive par elle-même : si le contrôleur joint l'agent par
    127.0.0.1, l'agent est sur la machine du contrôleur — il n'y a rien à vérifier de plus.
    """
    host = ((node or {}).get("host") or "").strip()
    if not host:
        return False
    if host.lower() in ("localhost", "ip6-localhost"):
        return True
    try:
        if ipaddress.ip_address(host).is_loopback:
            return True
    except ValueError:
        pass                       # nom d'hôte non résolu ici : on s'en tient aux IP connues
    if controller_ips is None:
        from .addressing import controller_ipv4s
        controller_ips = controller_ipv4s()
    return host in set(controller_ips or ())


def shim_requis(node, controller_ips=None):
    """Ce nœud a-t-il besoin du shim ? (mode SITE **et** nœud portant l'orchestrateur)

    Les deux conditions, et aucune autre :
    - mode « interne » → le réseau est un bridge, que l'hôte joint nativement : un shim n'y
      servirait à rien et ajouterait une interface à expliquer ;
    - nœud DISTINCT du contrôleur → l'isolation macvlan ne gêne personne, le contrôleur n'est
      pas sur le parent. Poser un shim là ne réparerait rien et masquerait un vrai défaut de
      routage s'il y en a un.
    """
    if node_net_mode(node) != NET_MODE_SITE:
        return False
    return est_tout_en_un(node, controller_ips)


def mode_interne_possible(node, controller_ips=None):
    """Ce nœud peut-il passer en « réseau interne » ? → (ok, raison_si_non, avertissement).

    ★ LA RÉPONSE EST NON, SAUF SUR UN TOUT-EN-UN, et ce n'est pas une précaution : c'est le
    plan de CONTRÔLE qui l'impose. L'orchestrateur parle à chaque conteneur en direct, à
    `docker_ip:8081` (agent par-conteneur) et `:8080` (métriques) — cf. `addressing
    .get_container_ip`. Un subnet privé en bridge ne sort pas de la machine : depuis un
    contrôleur situé ailleurs, ces deux ports sont injoignables. Le déploiement ne serait pas
    « dégradé », il serait impossible — et les conteneurs apparaîtraient « running » sans jamais
    recevoir leur script, c'est-à-dire la panne que ce chantier répare, reproduite à l'identique
    par l'autre bout.
    On REFUSE donc, en nommant la raison. Tant qu'il n'y a pas de publication de port par
    conteneur pour le plan de contrôle lui-même (et non seulement pour la passerelle WebRTC),
    le mode interne n'a de sens que là où le contrôleur est sur le nœud.

    ⚠ L'avertissement rendu en troisième position n'est pas cosmétique : en HAUTE DISPO, le
    contrôleur peut MIGRER. Un nœud tout-en-un est tout-en-un pour le contrôleur d'aujourd'hui ;
    après un basculement, ses conteneurs en mode interne deviennent injoignables depuis le
    contrôleur devenu actif, sans qu'aucun réglage n'ait changé. On ne l'interdit pas — un site
    peut vouloir le mode interne sur un contrôleur appairé et l'assumer — mais on le DIT, parce
    que personne ne ferait le lien entre un failover et un plugin qui ne se déploie plus.
    """
    host = ((node or {}).get("host") or "").strip()
    if not host:
        return False, "nœud sans hôte connu (enrôlement non finalisé ?)", None
    if not est_tout_en_un(node, controller_ips):
        return False, ("le « réseau interne » n'est disponible que sur un serveur tout-en-un "
                       "(orchestrateur sur le nœud). Ici l'orchestrateur est ailleurs : son plan "
                       "de contrôle joint chaque conteneur en direct sur :8081, ce qu'un subnet "
                       "privé au nœud ne permet pas. Garder « réseau du site » pour ce nœud."), None
    avert = None
    try:
        from . import ha
        if ha.peer_url():
            avert = ("haute dispo appairée : si le contrôleur bascule sur l'autre machine, les "
                     "conteneurs de ce nœud en « réseau interne » ne seront plus joignables "
                     "depuis le contrôleur devenu actif.")
    except Exception as e:                      # HA non configurée / module indisponible
        log.debug("mode_interne_possible: HA non consultable : %s", e)
    return True, None, avert


# Préfixe des interfaces que l'ORCHESTRATEUR crée lui-même sur un hôte (`bobi-shim` aujourd'hui).
# Elles ne sont pas du matériel du nœud, et aucune décision prise « d'après les cartes du nœud »
# ne doit les considérer.
PREFIXE_A_NOUS = "bobi-"


def interface_a_nous(ifname):
    """Cette interface a-t-elle été créée par l'orchestrateur (donc : pas une carte du nœud) ?

    ★ POURQUOI CE FILTRE EXISTE (relevé par la session Z8 le 2026-10-27, après déploiement).
    Une fois le shim posé, `pool_hors_subnet` comparait le pool d'IP à... `bobi-shim`, dont
    l'adresse est en /32 DANS le subnet des conteneurs. Aucun pool ne peut jamais contenir un
    /32 : la remarque apparaissait donc sur TOUT nœud dont le shim est posé, quelle que soit la
    configuration — un avertissement qui se déclenche sur le succès de l'étape précédente.

    Et le danger dépassait la remarque : toute logique qui cherche « la carte des conteneurs »
    parmi les interfaces du nœud (`parent_declare`, les suggestions de subnet) pouvait élire le
    shim, et le macvlan des conteneurs aurait alors eu pour parent… le shim lui-même.
    """
    return (ifname or "").strip().startswith(PREFIXE_A_NOUS)


def pool_hors_subnet(node_id, subnet_pool):
    """Le pool d'IP proposé est-il HORS du subnet de la carte conteneurs du nœud ?
    → (subnet_de_la_carte, ifname, subnet_du_pool) si incohérent, (None, None, None) sinon.

    Le subnet du POOL fait partie du constat. Sans lui, l'objet rendu par l'API nomme ce que
    porte la carte sans dire ce qui est proposé à la place : un lecteur de l'API (une autre
    session, un script de recette) voit « incohérent » sans les deux termes de l'incohérence.
    Relevé par la session Z8 le 2026-10-27 : la page l'affichait, l'API non.

    ★ POURQUOI C'EST UN AVERTISSEMENT ET PAS UNE CORRECTION (relevé par la session Z8 le
    2026-10-27 après déploiement). Sur la Z8, la carte des conteneurs est en 192.0.2.0/24 et le
    pool d'IP du cluster vaut 192.168.1.x. La suggestion du panneau proposait donc fidèlement
    192.168.1.0/24 — et « Reconfigurer le réseau » aurait DÉTRUIT un réseau qui marchait pour le
    recréer dans un subnet où le nœud n'est pas : conteneurs injoignables, et le geste qui a
    cassé n'aurait aucun rapport apparent avec le symptôme. Le shim, lui, aurait posé des routes
    vers des adresses que personne ne porte.

    On ne substitue PAS la valeur en silence : un pool sur un autre subnet que la carte peut être
    légitime (routage, topologie séparée mal déclarée), et deviner à la place de l'exploitant
    remplacerait une panne lisible par une décision invisible. On NOMME les deux subnets et on
    laisse trancher — c'est la même règle que pour la table de routage de l'hôte : on signale,
    on ne réarrange pas.
    """
    from .database import db_get_node_interfaces, role_is_containers
    try:
        pool = ipaddress.ip_network(str(subnet_pool or "").strip(), strict=False)
    except Exception:
        return (None, None, None)
    for r in (db_get_node_interfaces(node_id) or []):
        if not role_is_containers(r.get("role")) or interface_a_nous(r.get("ifname")):
            continue
        cidr = (r.get("ip_cidr") or "").strip()
        if not cidr:
            continue
        try:
            carte = ipaddress.ip_interface(cidr).network
        except Exception:
            continue
        if carte != pool:
            return (str(carte), r.get("ifname") or "", str(pool))
    return (None, None, None)


def shim_ip_suggeree(node_id, subnet, start, end, prises=None):
    """IP à donner au shim : dans le subnet des conteneurs, HORS de la plage qui leur est
    attribuée, et libre. None si le subnet n'en offre aucune.

    On descend depuis le HAUT du subnet : les plages de conteneurs sont presque toujours posées
    en partant du bas, et le dernier hôte d'un /24 (.254) est l'adresse la moins disputée du
    segment. On exclut la passerelle, les hôtes des nœuds, les IP du contrôleur et les adresses
    déjà prises par un conteneur.

    ⚠ Ce n'est qu'une SUGGESTION. Sur un LAN de site, personne ne peut garantir depuis ici
    qu'une adresse est libre : c'est l'exploitant qui tranche, l'interface la propose. Prendre
    une adresse déjà utilisée par un équipement du site ferait un conflit d'IP, c'est-à-dire la
    panne la plus difficile à lire qu'un réseau sache produire.
    """
    from . import allocations
    try:
        net = ipaddress.ip_network(str(subnet).strip(), strict=False)
    except Exception as e:
        log.warning("shim_ip_suggeree(%s): subnet « %s » invalide : %s", node_id, subnet, e)
        return None
    plage = set(allocations._range_list(start, end))
    bloquees = set(prises) if prises is not None else (
        allocations._used_ips() | allocations._reserved_ips(node_id))
    for ip in reversed(list(net.hosts())):
        s = str(ip)
        if s not in plage and s not in bloquees:
            return s
    return None


def script_shim(parent, shim_ip, subnet, start, end):
    """Script hôte POSANT le shim, idempotent et PERSISTANT. Rend (script_sh, commande_hote).

    Fonction PURE (aucun appel, aucun nœud) : c'est le point où le banc peut lire ce qui sera
    exécuté avant que ça ne le soit. Les trois propriétés qui comptent :

    1. **Idempotent** : `ip link add` seulement si l'interface manque, puis `addr replace` et
       `route replace` — rejouer le script ne doit jamais échouer, puisque systemd le rejoue à
       chaque démarrage et que l'orchestrateur le rejoue à chaque application de réglage.
    2. **Aucune route de subnet** : une route par CIDR de la PLAGE des conteneurs. Cf. l'en-tête
       du module — router le subnet casserait le LAN du site.
    3. **Persistant** : unité systemd oneshot `RemainAfterExit`, sur le modèle des unités
       `bobi-link-*` / `bobi-vlan-*` déjà posées par `routes/node_network.py`. Sans elle, les
       conteneurs redeviennent injoignables au premier redémarrage du nœud — c'est-à-dire
       exactement le symptôme d'origine, rejoué des mois plus tard, sans que le lien se fasse.
    """
    # Subnet validé (il doit être lisible) mais son PRÉFIXE n'est pas utilisé — cf. ci-dessous.
    ipaddress.ip_network(str(subnet).strip(), strict=False)
    routes = cidrs_de_plage(start, end)
    lignes = [
        "#!/bin/sh",
        "# Shim macvlan Bobi.Studio — posé par l'orchestrateur, ne pas éditer à la main.",
        "# Lève l'isolation parent/enfant macvlan sur un nœud qui porte AUSSI l'orchestrateur.",
        "set -e",
        "ip link show %s >/dev/null 2>&1 || ip link add %s link %s type macvlan mode bridge"
        % (SHIM_IFACE, SHIM_IFACE, parent),
        # ★ /32, JAMAIS LE PRÉFIXE DU SUBNET. Poser 192.0.2.254/24 sur le shim créerait une
        # SECONDE route connectée vers le /24 du site, en concurrence avec celle de la carte du
        # nœud : le noyau choisit alors selon la métrique et l'ordre, et peut envoyer tout le
        # trafic du site dans le shim. C'est la même catastrophe que la route de subnet évitée
        # plus bas, par un autre chemin — l'adresse, pas la route. En /32, le shim ne crée
        # aucune route connectée : seules les routes explicites ci-dessous l'empruntent.
        "ip addr replace %s/32 dev %s" % (shim_ip, SHIM_IFACE),
        "ip link set %s up" % SHIM_IFACE,
    ]
    for cidr in routes:
        # `route replace` plutôt que `add` : relancé, `add` rendrait « File exists » et, sous
        # `set -e`, tuerait le script au premier CIDR déjà posé — donc après un redémarrage le
        # shim n'aurait qu'une partie de ses routes, et seuls CERTAINS conteneurs répondraient.
        # `src` explicite : sans elle, le noyau choisirait comme adresse source celle de la
        # carte du nœud (la PARENTE du macvlan) — or un conteneur ne peut pas répondre à son
        # parent. L'aller partirait, le retour serait jeté, et le symptôme serait « ça ne
        # marche toujours pas » alors que le shim est bien posé.
        lignes.append("ip route replace %s dev %s src %s" % (cidr, SHIM_IFACE, shim_ip))
    script = "\n".join(lignes) + "\n"

    unite = (
        "[Unit]\nDescription=Shim macvlan Bobi.Studio (hôte vers conteneurs)\n"
        "After=network-pre.target docker.service\nWants=network-pre.target\n"
        "[Service]\nType=oneshot\nRemainAfterExit=yes\nExecStart=%s\n"
        "[Install]\nWantedBy=multi-user.target\n" % SHIM_SCRIPT
    )
    cmd = (
        "set -e; cat > %s <<'BOBISH'\n%sBOBISH\n"
        "chmod +x %s; sh %s; "
        "cat > /etc/systemd/system/%s.service <<'BOBIUNIT'\n%sBOBIUNIT\n"
        "systemctl daemon-reload; systemctl enable %s.service"
        % (SHIM_SCRIPT, script, SHIM_SCRIPT, SHIM_SCRIPT,
           SHIM_UNIT, unite, SHIM_UNIT)
    )
    return script, cmd


def script_retrait_shim():
    """Commande hôte RETIRANT le shim (retour en mode interne, ou nœud qui n'est plus tout-en-un).

    Tout est `|| true` : on retire ce qui existe et on ne tombe pas sur ce qui a déjà disparu.
    Un retrait qui échoue à moitié laisserait une interface sans routes, ou des routes vers une
    interface absente — un trou noir silencieux pour les adresses des conteneurs.
    """
    return (
        "systemctl disable --now %s.service 2>/dev/null || true; "
        "rm -f /etc/systemd/system/%s.service %s; "
        "systemctl daemon-reload 2>/dev/null || true; "
        "ip link del %s 2>/dev/null || true"
        % (SHIM_UNIT, SHIM_UNIT, SHIM_SCRIPT, SHIM_IFACE)
    )


def appliquer_shim(node, parent, shim_ip, subnet, start, end):
    """Pose le shim sur le nœud via l'agent. Rend (ok, message)."""
    from . import node_driver
    if not (parent and shim_ip and subnet):
        return False, "parent, IP du shim et subnet requis"
    if not cidrs_de_plage(start, end):
        return False, "plage des conteneurs illisible (%s → %s) : aucune route à poser" % (start, end)
    _script, cmd = script_shim(parent, shim_ip, subnet, start, end)
    # ★ `host_exec` REND TROIS VALEURS : (rc, stdout, stderr) — même contrat que host_ops.ssh_run.
    # Dépaqueter en deux levait un ValueError au premier appel RÉEL (plantage en 500 sur la Z8 le
    # 2026-10-27), et aucun de mes bancs ne l'a vu parce qu'ils doublaient tous `node_driver` :
    # une doublure rend ce que son auteur croit, pas ce que la fonction rend. Garde depuis :
    # `tests/verif_host_exec_arite.py`, qui compare TOUS les sites d'appel à la vraie signature.
    rc, out, err = node_driver.host_exec(node, cmd, timeout=30)
    if rc != 0:
        return False, "pose du shim échouée (rc=%s) : %s" % (rc, (err or out or "").strip()[:200])
    return True, "shim %s posé sur %s (%s)" % (SHIM_IFACE, parent, shim_ip)


def retirer_shim(node):
    """Retire le shim du nœud (best-effort). Rend (ok, message)."""
    from . import node_driver
    rc, out, err = node_driver.host_exec(node, script_retrait_shim(), timeout=30)
    return (rc == 0, "shim retiré" if rc == 0
            else "retrait du shim échoué (rc=%s) : %s" % (rc, (err or out or "").strip()[:200]))


def etat_shim(node):
    """Constat du shim SUR LE NŒUD : {present, ip, routes, requis, mode, raison}.

    On mesure, on ne déduit pas : un shim « appliqué » en base peut avoir été supprimé à la main,
    et une unité systemd peut être désactivée sans que rien ne le dise. `routes` = nombre de
    routes effectivement posées vers l'interface, qu'on compare à ce que la plage exige.

    ★ `raison` : POURQUOI il est requis et absent. Relevé par la session Z8 le 2026-10-27 — elle
    lisait `requis: true, present: false` sans rien pour savoir si c'était une panne, une attente
    ou un refus. Un état qui constate un manque sans dire ce qui le lève oblige son lecteur à
    aller lire le code, et c'est un état incomplet.

    C'est un CODE, jamais une phrase : l'interface traduit (même règle que les refus de
    `node_agent` §Disques). `pool_hors_subnet` = le pool d'IP n'est pas sur le subnet de la carte,
    donc le shim routerait vers des adresses que le nœud ne porte pas — à corriger avant.
    `a_poser` = tout est cohérent, il reste le geste.
    """
    from . import node_driver
    etat = {"mode": node_net_mode(node), "requis": shim_requis(node),
            "present": False, "ip": "", "routes": 0, "unite": False, "raison": None}
    cmd = ("ip -o -4 addr show dev %s 2>/dev/null | awk '{print $4}'; echo '--'; "
           "ip route show dev %s 2>/dev/null | wc -l; echo '--'; "
           "systemctl is-enabled %s.service 2>/dev/null || true"
           % (shlex.quote(SHIM_IFACE), shlex.quote(SHIM_IFACE), shlex.quote(SHIM_UNIT)))
    try:
        rc, out, _err = node_driver.host_exec(node, cmd, timeout=15)
    except Exception as e:
        log.debug("etat_shim: %s", e)
        return etat
    blocs = [b.strip() for b in (out or "").split("--")]
    if blocs and blocs[0]:
        etat["present"] = True
        etat["ip"] = blocs[0].splitlines()[0].strip()
    if len(blocs) > 1 and blocs[1].strip().isdigit():
        etat["routes"] = int(blocs[1].strip())
    if len(blocs) > 2:
        etat["unite"] = blocs[2].strip() == "enabled"
    if etat["requis"] and not etat["present"]:
        # ★ UNE SEULE RAISON, « a_poser », et c'est volontaire.
        # Il y avait ici un second motif, « pool_hors_subnet », qui masquait le bouton quand le
        # subnet des conteneurs différait de l'IP de gestion du nœud. Il était FAUX : le shim
        # porte lui-même une adresse DANS le subnet des conteneurs, sur leur L2, et les joint en
        # direct — le nœud n'a pas à les porter. Ce motif empêchait donc exactement la
        # configuration que le shim sert (relevé par la session Tests globaux le 2026-10-27).
        # Le seul empêchement réel — aucune adresse libre pour le shim — est constaté par la
        # route au moment d'agir, avec la plage concernée, plutôt que DEVINÉ ici à partir d'une
        # approximation du subnet.
        etat["raison"] = "a_poser"
    return etat


# ─── Publication de ports (mode interne) ────────────────────────────────────────────────────
# ★ CE QUE L'INVENTAIRE DU 2026-10-27 A IMPOSÉ, contre l'intuition de départ
# (`docs/chantiers/RESEAU_CONTENEURS.md`) :
#
# 1. La publication NE PEUT PAS être une propriété du MODE. « En interne, on publie tout »
#    exposerait `:8081` et `:8082` — le contrôle complet des conteneurs — sur le LAN.
# 2. Elle ne peut pas non plus être une propriété du PLUGIN seul : `stream_in` en mode appel ne
#    publie rien, en mode écoute si ; `multiview` dépend de son `tsl_mode`. La même image, selon
#    sa configuration, a besoin d'un port ou n'en a pas besoin.
#
# Donc : DÉCLARÉE PAR LE MANIFESTE, RÉSOLUE AU DÉPLOIEMENT d'après la config de l'instance.
# Contrat dans `plugins/AUTHORING.md` (« Ports publiés »).
#
# Et une règle qui n'est pas négociable : rien n'est publié en mode SITE. Le conteneur y a sa
# propre IP sur le LAN, donc rien à republier ; le faire quand même prendrait des ports de
# l'HÔTE pour un service déjà joignable, et deux conteneurs du même type se marcheraient dessus
# pour rien.
PORT_PROTOS = ("tcp", "udp")


def ports_declares(deploy_type):
    """Déclarations `ports` du manifeste d'un type (liste, [] si aucune ou type inconnu).

    Deux registres, dans cet ordre : les PLUGINS (`plugins/<type>/plugin.json`) puis les
    SERVICES (`services/<id>/manifest.json`). La passerelle WebRTC est un service et vit dans
    un conteneur — c'est même le seul composant qu'un NAVIGATEUR joint en direct, donc celui
    qui a le plus besoin d'un port publié. Ne regarder que le registre des plugins l'aurait
    laissée muette, et la page de monitoring serait restée noire sans message.
    """
    if not deploy_type:
        return []
    from . import plugins
    man = plugins.get(deploy_type) or {}
    decl = man.get("ports")
    if not isinstance(decl, list):
        from . import core_plugins
        try:
            svc = next((m for m in core_plugins.manifest_list()
                        if m.get("id") == deploy_type), None) or {}
        except Exception as e:
            log.warning("ports_declares(%s): registre des services illisible : %s",
                        deploy_type, e)
            svc = {}
        decl = svc.get("ports")
    return decl if isinstance(decl, list) else []


def _condition_remplie(cond, params):
    """`when` = {clé_de_config: [valeurs acceptées]} — TOUTES doivent être remplies (ET).

    Comparaison sur la REPRÉSENTATION TEXTUELLE : une config vient du JSON, où `true`, `"true"`
    et `1` se croisent selon qui a écrit la valeur (manifeste, interface, macro, import de
    projet). Comparer les types ferait dépendre la publication d'un port du chemin par lequel
    la valeur est arrivée — une différence que personne ne peut voir.
    """
    if not isinstance(cond, dict):
        return True
    for cle, attendues in cond.items():
        if not isinstance(attendues, (list, tuple)):
            attendues = [attendues]
        courante = str((params or {}).get(cle, "")).strip().lower()
        if courante not in {str(a).strip().lower() for a in attendues}:
            return False
    return True


def ports_a_publier(deploy_type, params, node=None):
    """Ports à publier pour cette instance → [{hote, conteneur, proto, desc}].

    Vide si le nœud n'est pas en mode « réseau interne » (cf. l'en-tête de section).

    Le port de l'HÔTE est le MÊME que celui du conteneur. C'est le seul choix qu'un exploitant
    puisse deviner : une source de reportage qu'on configure pour pousser « vers le serveur, en
    SRT sur 9000 » doit tomber sur 9000. Un port hôte réattribué dynamiquement serait invisible
    partout où l'adresse est écrite à la main — c'est-à-dire chez le client.

    Un port qui vaut 0, qui manque ou qui n'est pas un entier est IGNORÉ sans bruit : c'est la
    façon dont `multiview` dit « pas de TSL direct » (`tsl_port: 0`).
    """
    if node is not None and not node_net_interne(node):
        return []
    out = []
    for d in ports_declares(deploy_type):
        if not isinstance(d, dict) or not _condition_remplie(d.get("when"), params):
            continue
        # Trois sources, et l'ordre est celui de la spécificité : la config DE L'INSTANCE,
        # puis un RÉGLAGE global, puis une valeur fixe. Le réglage sert aux composants dont le
        # port est un réglage d'installation et non une propriété du conteneur — la passerelle
        # WebRTC (`webrtc_http_port`) est exactement ce cas.
        if d.get("param"):
            brut = (params or {}).get(d["param"])
        elif d.get("setting"):
            from . import settings as _st
            brut = _st.get(d["setting"])
        else:
            brut = d.get("port")
        try:
            num = int(brut)
        except (TypeError, ValueError):
            continue
        if not 0 < num < 65536:
            continue
        proto = str(d.get("proto") or "tcp").lower()
        if proto not in PORT_PROTOS:
            log.warning("ports_a_publier(%s): protocole inconnu %r — déclaration ignorée",
                        deploy_type, proto)
            continue
        out.append({"hote": num, "conteneur": num, "proto": proto,
                    "desc": str(d.get("desc") or "")})
    return out


def conflits_de_ports(node_id, vmid, a_publier):
    """Ports déjà publiés par un AUTRE conteneur du même nœud → [(port, proto, vmid, hostname)].

    ★ POURQUOI ON LE CALCULE AU LIEU DE LAISSER DOCKER REFUSER. Docker refuserait, avec « port
    is already allocated » — un message vrai et inutilisable : il ne dit ni qui détient le port,
    ni que c'est un autre conteneur de la même installation. Deux `stream_in` en écoute sur 9000,
    c'est la configuration la plus naturelle du monde (deux sources de reportage), et l'exploitant
    doit s'entendre dire LEQUEL occupe le port, pas aller lire `docker ps`.
    """
    from .database import db_get_containers
    pris = {}
    for c in (db_get_containers() or []):
        if c.get("node_id") != node_id or c.get("vmid") == vmid:
            continue
        cfg = c.get("deploy_config")
        if isinstance(cfg, str):
            import json as _json
            try:
                cfg = _json.loads(cfg)
            except Exception:
                cfg = {}
        cfg = cfg or {}
        for p in ports_a_publier(cfg.get("type"), cfg.get("params") or {}):
            pris[(p["hote"], p["proto"])] = (c.get("vmid"), c.get("hostname"))
    return [(p["hote"], p["proto"]) + pris[(p["hote"], p["proto"])]
            for p in a_publier if (p["hote"], p["proto"]) in pris]


def inspect_reseau(node, name):
    """Inspection du réseau `name` sur le nœud → dict Docker, ou None s'il n'existe pas."""
    import json as _json
    import shlex as _sh
    from . import node_driver
    _rc, out, _e = node_driver.host_exec(
        node, "docker network inspect %s 2>/dev/null" % _sh.quote(name), timeout=15)
    try:
        arr = _json.loads(out) if (out or "").strip() else []
        return arr[0] if arr else None
    except Exception:
        return None


def conteneurs_attaches(node, node_id, name):
    """vmid des conteneurs de CE nœud attachés au réseau `name` — constat sur l'hôte, trié.

    Le constat est fait sur Docker et non sur la base : c'est Docker qui décide qui tombe quand
    le réseau disparaît. Un conteneur présent en base mais détaché n'a pas à être compté, et un
    conteneur attaché mais absent de la base ne peut pas être « gardé » — il est seulement retiré.
    """
    net = inspect_reseau(node, name) or {}
    att = {a.get("Name") for a in (net.get("Containers") or {}).values() if a.get("Name")}
    if not att:
        return []
    from .database import db_get_containers
    return sorted(c["vmid"] for c in (db_get_containers() or [])
                  if c.get("node_id") == node_id and c.get("docker_name") in att)


def detacher_conteneurs(node, vmids):
    """Retire ces conteneurs de l'HÔTE en les GARDANT EN BASE. → liste des vmid détachés.

    ★ CE QUE CETTE FONCTION EXISTE POUR NE PLUS FAIRE (28/10, bascule de la Z8 à 20:45).
    Le changement de mode réseau appelait `destroy_compute`, donc `db_delete_container` : les 12
    conteneurs de production du nœud ont été SUPPRIMÉS DE LA BASE, `deploy_config` compris. Il n'y
    avait aucune sauvegarde ; les configurations n'ont été récupérées que parce que le WAL SQLite
    n'avait pas encore été reporté dans le fichier principal. Changer un paramètre de réseau n'est
    en aucun cas une décision d'effacer la configuration de production — c'est le réseau Docker
    qui ne se modifie pas sous ses attachés, pas la base qui doit être purgée.

    On garde donc tout ce qui fait l'identité et la configuration du conteneur (`deploy_config`,
    projet, `instance_uuid`, jeton d'agent, emplacement) et on ne jette que ce qui est devenu
    FAUX : l'adresse, qui appartenait au réseau d'avant, et la signature de spec, qui ferait
    croire au prochain déploiement que le conteneur en place est conforme.

    Le conteneur est alors dans l'état « absent » que `start_compute` sait déjà rattraper : il
    relit `deploy_config` et redéploie (c'est le chemin de `recreer_compute`). Rien de nouveau
    n'est inventé ici — c'est le chemin de recréation existant, appliqué au bon moment.
    """
    import shlex as _sh
    from . import node_driver
    from .database import (db_get_container, db_update_docker_ip, db_update_spec_sig,
                           db_update_status)
    faits = []
    for v in sorted(set(vmids or [])):
        c = db_get_container(v) or {}
        nom = (c.get("docker_name") or "").strip()
        if not c:
            continue
        try:
            if nom:
                # Arrêt gracieux d'abord : le script a le temps de rendre ses mappings MXL
                # (même raison que dans `recreer_compute`).
                node_driver.host_exec(node, "docker stop -t 12 %s >/dev/null 2>&1; "
                                            "docker rm -f %s >/dev/null 2>&1"
                                      % (_sh.quote(nom), _sh.quote(nom)), timeout=60)
            db_update_docker_ip(v, "")      # l'IP appartenait au réseau d'avant
            db_update_spec_sig(v, None)     # → le prochain déploiement RECRÉE, il ne croit pas conforme
            db_update_status(v, "stopped")
            faits.append(v)
        except Exception as e:
            log.warning("detacher_conteneurs: %s : %s", v, e)
    return faits


def redeployer_conteneurs(vmids, raison=""):
    """Redéploie ces conteneurs depuis leur `deploy_config` (après un changement de réseau).

    Synchrone et SÉRIALISÉ par vmid (`verrou_vmid`) : l'appelant le lance dans un thread de fond.
    Best-effort par conteneur — un échec sur l'un ne doit pas priver les autres de leur
    redéploiement, et chaque issue est ALERTÉE (sinon un conteneur resté à terre après une
    bascule serait un trou silencieux dans la production).
    """
    from .database import db_add_alert, db_get_container
    from .vmlocks import verrou_vmid
    ok, ko = [], []
    for v in sorted(set(vmids or [])):
        c = db_get_container(v) or {}
        try:
            from .docker_compute import start_compute
            with verrou_vmid(v, op="bascule-reseau"):
                r = bool(start_compute(v))
        except Exception as e:
            log.warning("redeployer_conteneurs: %s : %s", v, e)
            r = False
        (ok if r else ko).append(v)
        db_add_alert("alert.net.bascule_redeploy_ok" if r else "alert.net.bascule_redeploy_echec",
                     "info" if r else "error", vmid=v, node_id=c.get("node_id"), kind="net",
                     params={"vmid": v, "h": c.get("hostname") or "", "raison": raison})
    return ok, ko


def detruire_reseau(node, node_id, name, raison, **params):
    """Détruit le réseau `name` du nœud ; ses conteneurs sont DÉTACHÉS, pas effacés. → liste des vmid.

    Les conteneurs tombent forcément : un réseau Docker ne se modifie pas sous ses attachés.
    C'est pour ça que l'appelant doit l'avoir ANNONCÉ — et que l'alerte posée ici le dit, avec
    le compte. Mais ils restent EN BASE avec leur configuration (cf. `detacher_conteneurs`) :
    ils sont à redéployer, pas à reconfigurer de zéro. Les conteneurs non suivis en base sont
    retirés aussi : sans ça, `docker network rm` échoue et le réseau reste, dans son ancien état,
    en ayant l'air d'avoir été recréé.
    """
    import shlex as _sh
    from . import node_driver
    from .database import db_add_alert
    tombes = conteneurs_attaches(node, node_id, name)
    detacher_conteneurs(node, tombes)
    node_driver.host_exec(node,
        "for c in $(docker network inspect -f '{{range .Containers}}{{.Name}} {{end}}' %s "
        "2>/dev/null); do docker rm -f \"$c\" >/dev/null 2>&1; done; "
        "docker network rm %s >/dev/null 2>&1"
        % (_sh.quote(name), _sh.quote(name)), timeout=60)
    db_add_alert(raison, "warning", node_id=node_id, kind="net",
                 params=dict({"name": name, "n": node.get("name"),
                              "count": len(tombes)}, **params))
    return tombes


def divergence_reseau(node, node_id, name, driver, parent="", subnet="", gateway=""):
    """Ce que changerait une reconfiguration, SANS rien toucher.
    → {diverge, champs: [{quoi, avant, apres}], conteneurs: [vmid…]}

    ★ SÉPARÉ DE L'ACTION DEPUIS LE 2026-10-27, et voici pourquoi.
    `recreer_si_divergent` détruisait d'autorité. Sur la Z8, un clic sur « Reconfigurer le
    réseau » quinze secondes après un redémarrage a donc détruit un réseau qui marchait, avec ses
    conteneurs, pour le recréer dans un autre subnet. Le geste était légitime, son COÛT ne
    l'était pas : personne n'avait dit à l'opérateur qu'il allait perdre ses conteneurs.
    Pouvoir CONSTATER avant d'agir est ce qui permet de le lui dire.
    """
    etat = {"diverge": False, "champs": [], "conteneurs": []}
    net = inspect_reseau(node, name)
    if not net:
        return etat
    cfg = (net.get("IPAM", {}).get("Config") or [{}])[0]
    actuels = {"subnet": cfg.get("Subnet") or "", "gateway": cfg.get("Gateway") or "",
               "parent": (net.get("Options") or {}).get("parent") or "",
               "driver": net.get("Driver") or ""}
    voulus = {"subnet": subnet, "gateway": gateway, "parent": parent, "driver": driver}
    for quoi, apres in voulus.items():
        # Une valeur voulue VIDE n'est pas une demande de changement : c'est « laisse tel quel ».
        if apres and actuels[quoi] != apres:
            etat["champs"].append({"quoi": quoi, "avant": actuels[quoi], "apres": apres})
    etat["diverge"] = bool(etat["champs"])
    if etat["diverge"]:
        from .database import db_get_containers
        att = {a.get("Name") for a in (net.get("Containers") or {}).values() if a.get("Name")}
        etat["conteneurs"] = sorted(c["vmid"] for c in (db_get_containers() or [])
                                    if c.get("node_id") == node_id
                                    and c.get("docker_name") in att)
    return etat


def recreer_si_divergent(node, node_id, name, driver, parent="", subnet="", gateway=""):
    """Détruit le réseau s'il existe avec d'AUTRES paramètres — sinon `ensure_network`, qui est
    create-si-absent, ne le mettrait JAMAIS à jour (cas vécu : passerelle restée à l'IP de
    l'hôte). Le PILOTE fait partie de la comparaison : c'est lui qui change quand on bascule de
    mode, et un réseau du bon nom dans le mauvais mode est exactement le cas que l'agent refuse
    par un 409 — autant le résoudre ici, où l'on sait quels conteneurs tombent.

    ⚠ DESTRUCTEUR. L'appelant doit avoir constaté (`divergence_reseau`) et obtenu l'accord de
    l'exploitant : cf. le 2026-10-27 sur la Z8.
    """
    etat = divergence_reseau(node, node_id, name, driver, parent, subnet, gateway)
    if not etat["diverge"]:
        return []
    _av = next((c["avant"] for c in etat["champs"] if c["quoi"] == "gateway"), "?")
    return detruire_reseau(node, node_id, name, "alert.net.macvlan_recree",
                           gw_avant=_av or "?", gw=gateway)


def appliquer_mode_interne(node, node_id, d):
    """Passe le nœud en « réseau interne » : bridge Docker privé, joint nativement par l'hôte.

    Trois choses y sont faites dans cet ordre, et l'ordre compte : refuser si le mode n'a pas de
    sens ici, démonter ce qui appartenait au mode précédent (réseau et shim), puis seulement
    créer. Créer d'abord laisserait un nœud avec deux réseaux et un shim orphelin routant vers
    des adresses qui n'existent plus.
    """
    import ipaddress as _ip
    from . import node_driver
    from .config import NET_INTERNE_SUBNET_DEFAUT
    from .database import db_update_node, db_add_alert

    # 1. Refus, en nommant la raison — le mode interne n'a de sens que sur un tout-en-un.
    ok, raison, avert = mode_interne_possible(node)
    if not ok:
        return 409, {"error": raison}

    subnet = ((d.get("subnet") or "").strip()
              or (node.get("container_net_subnet") or "").strip()
              or NET_INTERNE_SUBNET_DEFAUT)
    try:
        reseau = _ip.ip_network(subnet, strict=False)
        hotes = list(reseau.hosts())
        if len(hotes) < 3:
            raise ValueError("trop petit pour une passerelle et des conteneurs")
    except Exception as e:
        return 400, {"error": "subnet interne « %s » invalide : %s" % (subnet, e)}
    subnet = str(reseau)
    # Passerelle = PREMIER hôte du subnet. C'est l'adresse que Docker pose sur le pont côté
    # hôte, donc celle par laquelle les conteneurs sortent — et celle que `allocations
    # .interne_bounds` saute en commençant au deuxième. Les deux dérivations doivent rester
    # d'accord : si elles divergent, l'orchestrateur attribue à un conteneur l'adresse de sa
    # propre passerelle.
    gateway = str(hotes[0])
    name = (d.get("name") or "bobi-interne").strip()

    # 1bis. ★ CE QUE LA BASCULE COÛTE, DIT AVANT DE LE FAIRE — et refusé sans accord explicite.
    #
    # Ce gate manquait ici alors qu'il existait sur le chemin macvlan, et la session Z8 l'a payé
    # le 28/10 à 20:45 : un POST sans `confirme`, envoyé en croyant d'après l'interface que le
    # serveur demanderait confirmation, a démonté le réseau et emporté les 12 conteneurs de
    # production du nœud. L'interface, elle, faisait bien deux temps — mais une confirmation qui
    # ne vit que dans le navigateur ne protège rien : c'est le serveur qui détruit.
    ancien = (node.get("docker_network") or "").strip()
    _ancien_existe = bool(inspect_reseau(node, ancien)) if (ancien and ancien != name) else False
    a_tomber = conteneurs_attaches(node, node_id, ancien) if _ancien_existe else []
    _div = divergence_reseau(node, node_id, name, "bridge", subnet=subnet, gateway=gateway)
    a_tomber = sorted(set(a_tomber) | set(_div["conteneurs"]))
    # ★ LE GATE PORTE SUR LA DESTRUCTION, PAS SUR LES CONTENEURS (corrigé le 2026-10-30).
    # Il était indexé sur `a_tomber` : zéro conteneur attaché ⇒ aucune confirmation. Sur la
    # nouvelle Z8, un changement de subnet a donc détruit un réseau interne qui marchait, la
    # recréation a échoué, et le nœud est resté SANS réseau de conteneurs — sans qu'un seul
    # écran l'ait annoncé. Détruire le seul réseau de conteneurs d'un nœud est un coût en
    # soi : il n'y a pas de conteneur à perdre aujourd'hui, mais il y a tout à perdre si la
    # recréation échoue, et c'est précisément ce qui s'est produit.
    detruira = bool(_div["diverge"]) or _ancien_existe
    if (a_tomber or detruira) and not d.get("confirme"):
        return 409, {
            "error": ("passer ce nœud en « réseau interne » coupe %d conteneur(s) : un réseau "
                      "Docker ne se modifie pas sous ses attachés. Leur configuration est "
                      "CONSERVÉE (projet, réglages, identité) et ils sont redéployés sur le "
                      "nouveau réseau — mais ils seront interrompus." % len(a_tomber))
                     if a_tomber else
                     ("ce réglage DÉTRUIT le réseau « %s » de ce nœud avant de le recréer. "
                      "Aucun conteneur n'y est attaché — mais si la recréation échoue, le nœud "
                      "se retrouve SANS réseau de conteneurs, et plus rien n'y sera déployable."
                      % (name if _div["diverge"] else ancien)),
            "confirmation_requise": True,
            "detruit_reseau": (name if _div["diverge"] else ancien) if detruira else "",
            "conteneurs": a_tomber,
            "changements": _div["champs"],
        }

    # 2. Démonter le mode précédent. Un réseau de l'ancien mode porte un autre nom (macvlan :
    # « bobimacvlan ») : il ne serait jamais recréé, juste laissé là, avec ses conteneurs
    # attachés et injoignables.
    tombes = []
    if ancien and ancien != name:
        tombes = detruire_reseau(node, node_id, ancien, "alert.net.mode_bascule",
                                  mode=NET_MODE_INTERNE)
    tombes += recreer_si_divergent(node, node_id, name, driver="bridge",
                                    subnet=subnet, gateway=gateway)
    # Le shim n'a plus lieu d'être : l'hôte joint un bridge nativement. Laissé en place, il
    # garderait des routes vers la plage du SITE sur une interface macvlan sans enfants.
    if (node.get("container_net_shim_ip") or "").strip():
        retirer_shim(node)

    # 3. Créer.
    ok_n, res = node_driver.ensure_network(node, name, subnet=subnet, gateway=gateway,
                                          driver="bridge")
    if not ok_n:
        # ★ ÉCHEC APRÈS DESTRUCTION : NE PAS LAISSER LA BASE AFFIRMER UN RÉSEAU DISPARU.
        # C'est ce trou qui a rendu la nouvelle Z8 illisible pendant deux heures le 30/10 :
        # l'étape 3 n'écrivant qu'en cas de succès, `nodes.docker_network` gardait le nom et le
        # subnet d'avant. L'API, la page Réglages et le badge « ⚙ Configuration » annonçaient
        # donc un réseau complet quand Docker n'avait plus que bridge/host/none. On remet la
        # base d'accord avec le constat, et on ALERTE : jusqu'ici le seul témoin de la panne
        # était un toast dans le navigateur de celui qui avait cliqué.
        db_update_node(node_id, docker_network="")
        db_add_alert("alert.net.reseau_absent_apres_echec", "error", node_id=node_id, kind="net",
                     params={"name": name, "n": node.get("name"), "err": str(res)[:200]})
        return 500, {"error": "création du réseau interne échouée : %s" % res}
    db_update_node(node_id, docker_network=name, container_net_mode=NET_MODE_INTERNE,
                   container_net_subnet=subnet, container_net_shim_ip="")
    db_add_alert("alert.net.interne_pret", "info", node_id=node_id, kind="net",
                 params={"name": name, "n": node.get("name"), "subnet": subnet})
    # 4. Remettre les conteneurs debout SUR LE NOUVEAU RÉSEAU. Ils n'ont jamais quitté la base :
    # `start_compute` les voit « absents » et redéploie depuis leur `deploy_config` (chemin de
    # `recreer_compute`), et `allocate_container_ip` leur donne une adresse du nouveau subnet —
    # l'ancienne, hors plage, n'est plus collante. En tâche de fond : douze redéploiements ne
    # tiennent pas dans une requête HTTP. `containers_removed` est conservé pour les appelants
    # existants, mais il NOMME désormais des conteneurs détachés, pas effacés.
    detaches = sorted(set(tombes))
    resp = {"ok": True, "mode": NET_MODE_INTERNE, "name": name, "subnet": subnet,
            "gateway": gateway, "agent": res, "containers_removed": detaches,
            "containers_detached": detaches}
    if detaches and d.get("redeployer") is not False:
        import threading as _th
        _th.Thread(target=redeployer_conteneurs, args=(detaches, "bascule mode interne"),
                   daemon=True).start()
        resp["redeploy_lance"] = detaches
    if avert:
        resp["warning"] = avert
    return 200, resp


# ═══ « Sortie Internet » : une carte dédiée, et des conteneurs qui sortent par elle ═════════
#
# LE BESOIN (validé le 2026-10-27) : dédier une interface (ex. `eno2`) en DHCP vers Internet, et
# dire à un conteneur de streaming de sortir par là — pas par le réseau du site.
#
# ★ ÉCART ASSUMÉ PAR RAPPORT AU PREMIER PLAN, et c'est le cœur de ce module.
# Le plan d'origine donnait au conteneur UN SECOND RÉSEAU Docker, portant sa route par défaut.
# Deux réseaux sur un conteneur, c'est Docker qui choisit la passerelle — par `gw-priority` sur
# les versions récentes, par l'ordre d'attachement sur les autres. La route par défaut d'un
# conteneur de production dépendrait donc de la version du moteur Docker installée sur le nœud,
# et le symptôme d'une erreur serait « le flux part par le mauvais lien », sans rien dans les
# journaux. C'est le piège que le plan signalait lui-même.
#
# On fait donc l'inverse, et plus simple : le conteneur garde UN SEUL réseau, et c'est l'HÔTE qui
# route sa sortie, par ROUTAGE PAR SOURCE — une règle `ip rule from <ip du conteneur>` vers une
# table dédiée dont la route par défaut passe par la carte Internet. Conséquences :
#   · aucune ambiguïté de passerelle, aucune dépendance à la version de Docker ;
#   · le choix est par CONTENEUR, et se change sans recréer le conteneur (une règle de routage,
#     pas une interface) ;
#   · le NAT est déjà là : la règle MASQUERADE que Docker pose pour le subnet du bridge couvre
#     la sortie par n'importe quelle interface ;
#   · le DNS continue de passer par le résolveur de l'hôte (donc par le site), ce qui est
#     généralement voulu : on change le chemin des DONNÉES, pas celui des noms.
#
# ⚠ CE QUE ÇA EXIGE : que l'hôte soit dans le chemin, donc le mode « réseau interne ». En mode
# site (macvlan), le conteneur est une machine du LAN avec sa propre passerelle et l'hôte n'est
# jamais traversé — aucune règle de l'hôte ne peut rien y changer. On refuse plutôt que de poser
# des règles qui ne serviraient à rien.
#
# ⚠ ET CE QU'ON NE FAIT PAS : toucher à la table de routage PRINCIPALE de l'hôte. Si le client
# DHCP de la carte Internet y installe une route par défaut qui concurrence celle du site, c'est
# la configuration réseau du nœud — on la SIGNALE (`etat_sortie`), on ne la réarrange pas en
# silence. Retirer une route par défaut sous un contrôleur de production, pour « bien faire »,
# est exactement le genre de geste dont le symptôme n'a plus aucun rapport avec la cause.
SORTIE_TABLE = 1031          # table de routage dédiée. FIXE : les règles doivent survivre aux
                             # redémarrages et rester lisibles au shell (`ip route show table 1031`).
SORTIE_SCRIPT = "/usr/local/sbin/bobi-sortie.sh"
SORTIE_LISTE = "/etc/bobi-sortie/ips"
SORTIE_UNIT = "bobi-sortie"
ROLE_INTERNET = "internet"


def iface_internet(node_id):
    """Nom de la carte portant le rôle « internet » sur ce nœud, ou "" si aucune."""
    if not node_id:
        return ""
    from .database import db_get_node_interfaces
    for r in (db_get_node_interfaces(node_id) or []):
        if (r.get("role") or "") == ROLE_INTERNET:
            return (r.get("ifname") or "").strip()
    return ""


def sortie_possible(node):
    """Ce nœud peut-il offrir une « Sortie Internet » ? → (ok, raison_si_non).

    Deux conditions, et les deux sont des faits de routage, pas des préférences :
    1. mode « réseau interne » — sinon l'hôte n'est pas dans le chemin du conteneur (cf. l'en-tête
       de section) et aucune règle posée ici n'aurait d'effet ;
    2. une carte déclarée « internet » — sans elle, il n'y a pas d'autre sortie que celle du site,
       et « sortir par ailleurs » ne veut rien dire.
    """
    if not node_net_interne(node):
        return False, ("la « Sortie Internet » demande le mode « réseau interne » : en mode "
                       "« réseau du site », un conteneur a sa propre passerelle sur le LAN et "
                       "ne traverse pas l'hôte — aucune règle de routage de l'hôte ne peut "
                       "changer son chemin.")
    if not iface_internet((node or {}).get("id")):
        return False, ("aucune carte déclarée « Internet » sur ce nœud — Réseau → Interfaces, "
                       "rôle « Internet » sur la carte dédiée.")
    return True, None


def script_sortie(iface):
    """Script hôte de la « Sortie Internet ». Rend (script_sh, commande_hote).

    Fonction PURE : le banc lit ce qui sera exécuté avant que ça ne le soit.

    Trois propriétés, chacune pour une panne précise :

    1. **La passerelle est RELUE À CHAQUE PASSAGE.** Elle vient d'un bail DHCP : la figer dans un
       fichier la rendrait fausse au premier renouvellement qui change de routeur, et le flux
       partirait dans le vide sans que rien ne bouge côté orchestrateur. Pas de passerelle lue =
       on sort en 0 sans rien casser (lien débranché, bail pas encore obtenu) : c'est un état
       transitoire normal, pas une erreur.
    2. **La liste des conteneurs est un FICHIER**, écrit par l'orchestrateur. Les règles `ip rule`
       ne survivent pas à un redémarrage ; le minuteur les repose depuis cette liste. Sans ça, un
       nœud redémarré renverrait tout le trafic par le site — silencieusement, et seulement
       jusqu'au prochain redéploiement, donc de façon intermittente.
    3. **Les règles EN TROP sont retirées.** Un conteneur repassé en sortie « nœud » dont la règle
       n'aurait pas été retirée (orchestrateur arrêté au mauvais moment) continuerait de sortir
       par Internet. On réconcilie sur la liste, on ne se contente pas d'ajouter.
    """
    script = """#!/bin/sh
# Sortie Internet Bobi.Studio — posé par l'orchestrateur, ne pas éditer à la main.
# Routage PAR SOURCE : les conteneurs listés dans %(liste)s sortent par %(iface)s.
set -e
IFACE=%(iface)s
TABLE=%(table)d
LISTE=%(liste)s

# Passerelle VIVANTE de la carte Internet (bail DHCP) — relue à chaque passage.
GW=$(ip -4 route show default dev "$IFACE" 2>/dev/null | awk '{print $3; exit}')
if [ -n "$GW" ]; then
  ip route replace default via "$GW" dev "$IFACE" table "$TABLE"
fi

# Règles voulues = le fichier. On ajoute ce qui manque, on retire ce qui n'y est plus.
VOULUES=$(grep -E '^[0-9.]+$' "$LISTE" 2>/dev/null | sort -u)
EXISTANTES=$(ip -4 rule show | awk -v t="$TABLE" '$0 ~ ("lookup " t)' \\
             | sed -n 's/.*from \\([0-9.]*\\).*/\\1/p' | sort -u)
for ip in $VOULUES; do
  echo "$EXISTANTES" | grep -qx "$ip" || ip rule add from "$ip" lookup "$TABLE"
done
for ip in $EXISTANTES; do
  echo "$VOULUES" | grep -qx "$ip" || ip rule del from "$ip" lookup "$TABLE" 2>/dev/null || true
done
""" % {"iface": iface, "table": SORTIE_TABLE, "liste": SORTIE_LISTE}

    unite = (
        "[Unit]\nDescription=Sortie Internet Bobi.Studio (routage par source)\n"
        "After=network-online.target docker.service\nWants=network-online.target\n"
        "[Service]\nType=oneshot\nRemainAfterExit=yes\nExecStart=%s\n"
        "[Install]\nWantedBy=multi-user.target\n" % SORTIE_SCRIPT
    )
    # Minuteur : c'est lui qui rattrape un bail renouvelé avec une AUTRE passerelle. Sans lui, le
    # correctif tiendrait jusqu'au premier renouvellement — soit quelques heures, soit des
    # semaines, ce qui est la pire des deux (la panne survient longtemps après le geste).
    minuteur = (
        "[Unit]\nDescription=Rafraîchit la Sortie Internet Bobi.Studio (bail DHCP, règles)\n"
        "[Timer]\nOnBootSec=30s\nOnUnitActiveSec=30s\nAccuracySec=5s\n"
        "[Install]\nWantedBy=timers.target\n"
    )
    cmd = (
        "set -e; mkdir -p %s; touch %s; "
        "cat > %s <<'BOBISORTIE'\n%sBOBISORTIE\n"
        "chmod +x %s; sh %s; "
        "cat > /etc/systemd/system/%s.service <<'BOBIU'\n%sBOBIU\n"
        "cat > /etc/systemd/system/%s.timer <<'BOBIT'\n%sBOBIT\n"
        "systemctl daemon-reload; systemctl enable %s.service; "
        "systemctl enable --now %s.timer"
        % (SORTIE_LISTE.rsplit("/", 1)[0], SORTIE_LISTE,
           SORTIE_SCRIPT, script, SORTIE_SCRIPT, SORTIE_SCRIPT,
           SORTIE_UNIT, unite, SORTIE_UNIT, minuteur, SORTIE_UNIT, SORTIE_UNIT)
    )
    return script, cmd


def script_retrait_sortie():
    """Commande hôte retirant la Sortie Internet : minuteur, unité, script, règles et table.

    Les règles d'abord : laisser des `ip rule` vers une table désormais vide enverrait le trafic
    des conteneurs concernés dans un trou noir — pire que de ne jamais avoir posé la sortie.
    """
    return (
        "for ip in $(ip -4 rule show | awk -v t=%(table)d '$0 ~ (\"lookup \" t)' "
        "| sed -n 's/.*from \\([0-9.]*\\).*/\\1/p'); do "
        "ip rule del from \"$ip\" lookup %(table)d 2>/dev/null || true; done; "
        "ip route flush table %(table)d 2>/dev/null || true; "
        "systemctl disable --now %(u)s.timer 2>/dev/null || true; "
        "systemctl disable --now %(u)s.service 2>/dev/null || true; "
        "rm -f /etc/systemd/system/%(u)s.service /etc/systemd/system/%(u)s.timer %(s)s; "
        "systemctl daemon-reload 2>/dev/null || true"
        % {"table": SORTIE_TABLE, "u": SORTIE_UNIT, "s": SORTIE_SCRIPT}
    )


def ips_sortie_internet(node_id):
    """IP des conteneurs de ce nœud qui doivent sortir par Internet (colonne `containers.egress`).

    On ne liste que les conteneurs qui ONT une IP : sans elle il n'y a pas de règle à écrire, et
    une ligne vide dans le fichier ferait échouer le script au premier `ip rule add`.
    """
    from .database import db_get_containers, container_egress, EGRESS_INTERNET
    out = []
    for c in (db_get_containers() or []):
        if c.get("node_id") != node_id or container_egress(c) != EGRESS_INTERNET:
            continue
        ip = (c.get("docker_ip") or c.get("ip") or "").strip()
        if ip:
            out.append(ip)
    return sorted(set(out))


def appliquer_sortie(node, ips=None):
    """Pose (ou rafraîchit) la Sortie Internet du nœud et la liste des conteneurs. (ok, message).

    Idempotent : rejouable à chaque déploiement. L'écriture de la liste et l'exécution du script
    sont faites dans le MÊME appel hôte — en deux appels, un échec entre les deux laisserait une
    liste à jour que rien n'applique, ou l'inverse.
    """
    from . import node_driver
    ok, raison = sortie_possible(node)
    if not ok:
        return False, raison
    iface = iface_internet(node.get("id"))
    ips = ips_sortie_internet(node.get("id")) if ips is None else ips
    _script, cmd = script_sortie(iface)
    liste = "\n".join(ips) + ("\n" if ips else "")
    cmd = ("mkdir -p %s; cat > %s <<'BOBIIPS'\n%sBOBIIPS\n"
           % (SORTIE_LISTE.rsplit("/", 1)[0], SORTIE_LISTE, liste)) + cmd + "; sh " + SORTIE_SCRIPT
    rc, out, err = node_driver.host_exec(node, cmd, timeout=40)
    if rc != 0:
        return False, "pose de la Sortie Internet échouée (rc=%s) : %s" % (
            rc, (err or out or "").strip()[:200])
    return True, "Sortie Internet par %s — %d conteneur(s)" % (iface, len(ips))


def poser_sortie(vmid, egress):
    """Change la SORTIE d'un conteneur et applique les règles sur son nœud. → (ok, message).

    ★ Pourquoi un seul point d'entrée : trois appelants (l'interface, les macros, le
    déploiement) doivent refuser pour les mêmes raisons et appliquer de la même façon. Trois
    copies de cette logique, c'est deux copies qui se périment — et la panne serait « ça marche
    depuis l'interface mais pas depuis une macro », ce qui n'évoque rien du vrai problème.

    Rien n'est détruit ni recréé : c'est une règle de routage sur l'hôte. Le conteneur ne bronche
    pas, les flux en cours continuent (le changement ne vaut que pour les NOUVELLES connexions,
    conntrack gardant les anciennes sur leur chemin — à dire à l'exploitant, pas à masquer).
    """
    from .database import (db_get_container, db_get_node, db_set_container_egress,
                           EGRESS_MODES, EGRESS_INTERNET, container_egress)
    if egress not in EGRESS_MODES:
        return False, "sortie inconnue : %s" % (egress,)
    c = db_get_container(vmid)
    if not c:
        return False, "conteneur %s introuvable" % (vmid,)
    node = db_get_node(c.get("node_id")) if c.get("node_id") else None
    if not node:
        return False, "conteneur %s sans nœud" % (vmid,)
    if egress == EGRESS_INTERNET:
        ok, raison = sortie_possible(node)
        if not ok:
            return False, raison
        if not (c.get("docker_ip") or c.get("ip") or "").strip():
            # Sans IP, il n'y a pas de source à router. On refuse au lieu d'enregistrer une
            # intention que rien n'appliquera : l'exploitant croirait son conteneur sorti par
            # Internet alors qu'il sortirait par le site.
            return False, ("conteneur %s sans IP (pas encore déployé ?) : il n'y a pas d'adresse "
                           "source à router" % (vmid,))
    avant = container_egress(c)
    db_set_container_egress(vmid, egress)
    ok, msg = appliquer_sortie(node)
    if not ok:
        db_set_container_egress(vmid, avant)      # on ne laisse pas la base mentir sur l'hôte
        return False, msg
    return True, msg


def etat_sortie(node):
    """Constat de la Sortie Internet SUR LE NŒUD : {iface, passerelle, regles, table, concurrente}.

    `concurrente` = la carte Internet porte AUSSI la route par défaut de la table principale.
    Ce n'est pas une erreur et on n'y touche pas (cf. l'en-tête de section) — mais c'est la
    différence entre « seuls les conteneurs désignés sortent par là » et « tout l'hôte sort par
    là », et personne ne devrait avoir à le deviner.
    """
    from . import node_driver
    etat = {"iface": iface_internet((node or {}).get("id")), "passerelle": "",
            "regles": 0, "table": SORTIE_TABLE, "concurrente": None, "minuteur": False}
    if not etat["iface"]:
        return etat
    i = shlex.quote(etat["iface"])
    cmd = ("ip -4 route show default table %d 2>/dev/null | awk '{print $3; exit}'; echo '--'; "
           "ip -4 rule show | grep -c 'lookup %d' || true; echo '--'; "
           "ip -4 route show default dev %s 2>/dev/null | wc -l; echo '--'; "
           "systemctl is-enabled %s.timer 2>/dev/null || true"
           % (SORTIE_TABLE, SORTIE_TABLE, i, shlex.quote(SORTIE_UNIT)))
    try:
        _rc, out, _e = node_driver.host_exec(node, cmd, timeout=15)
    except Exception as e:
        log.debug("etat_sortie: %s", e)
        return etat
    blocs = [b.strip() for b in (out or "").split("--")]
    if blocs and blocs[0]:
        etat["passerelle"] = blocs[0].splitlines()[0].strip()
    if len(blocs) > 1 and blocs[1].isdigit():
        etat["regles"] = int(blocs[1])
    if len(blocs) > 2 and blocs[2].isdigit():
        etat["concurrente"] = int(blocs[2]) > 0
    if len(blocs) > 3:
        etat["minuteur"] = blocs[3].strip() == "enabled"
    return etat


# ─── Exposition aux MACROS (contrat CLAUDE.md « Exposer aux macros (OBLIGATOIRE) ») ─────────
# Une fonction de l'orchestrateur s'expose comme un service : `MACRO_PROVIDER` + `run_action` /
# `read_state`, et le module s'inscrit dans `core_plugins.CORE_PROVIDERS`.
#
# Pourquoi la Sortie Internet DOIT y être : elle se change à chaud, sans rien détruire. C'est
# donc exactement le genre de bascule qu'une production veut automatiser — « quand on part en
# reportage, le streamer sort par la 4G ; au retour, par le site ». Sans exposition, la capacité
# existe et personne ne peut s'en servir au bon moment : un échec silencieux FONCTIONNEL.
MACRO_PROVIDER = {
    "id": "container_net",
    "label": "Réseau des conteneurs",
    "actions": [
        {"id": "set_egress", "label": "Choisir la sortie réseau d'un conteneur",
         "availability": "project", "params": [
             {"key": "vmid", "label": "Conteneur", "type": "select", "options_hook": True},
             {"key": "egress", "label": "Sortie", "type": "select",
              "options": [{"value": "node", "label": "Réseau du nœud"},
                          {"value": "internet", "label": "Sortie Internet"}],
              "default": "node"},
         ]},
    ],
    "states": [
        {"id": "sortie_internet_conteneurs", "label": "Conteneurs sortant par Internet",
         "type": "number"},
        {"id": "sortie_internet_prete", "label": "Sortie Internet opérationnelle",
         "type": "bool"},
    ],
}


def action_options(action_id, param_key, pid=None):
    """Listes vivantes des paramètres `options_hook` — nom imposé par le contrat de
    `core_plugins.service_actions` (`action_options(action_id, key[, pid])`). Ici : les
    conteneurs du parc. Un nom approchant (`macro_options`) ne serait JAMAIS appelé, et le menu
    de la macro resterait vide sans qu'aucune erreur n'apparaisse."""
    if action_id == "set_egress" and param_key == "vmid":
        from .database import db_get_containers
        return [{"value": str(c["vmid"]),
                 "label": "%s (%s)" % (c.get("hostname") or c["vmid"], c["vmid"])}
                for c in (db_get_containers() or []) if c.get("vmid")]
    return []


def run_action(action_id, params, ctx):
    if action_id != "set_egress":
        raise RuntimeError("action inconnue : %s" % action_id)
    try:
        vmid = int((params or {}).get("vmid"))
    except (TypeError, ValueError):
        raise RuntimeError("conteneur à choisir")
    ok, msg = poser_sortie(vmid, str((params or {}).get("egress") or "node").strip().lower())
    if not ok:
        # Une macro qui échoue DOIT le dire : avaler l'erreur ici donnerait une macro
        # « réussie » dont l'effet n'a pas eu lieu — le pire des deux mondes en production.
        raise RuntimeError(msg)
    return {"ok": True, "message": msg}


def read_state(state_id, ctx):
    from .database import db_get_nodes
    if state_id == "sortie_internet_conteneurs":
        return sum(len(ips_sortie_internet(n.get("id"))) for n in (db_get_nodes() or []))
    if state_id == "sortie_internet_prete":
        # VRAI seulement si un nœud peut RÉELLEMENT sortir : mode interne, carte déclarée, et une
        # passerelle lue sur cette carte. Les deux premiers sont de la configuration ; la
        # troisième est une mesure — un câble débranché ou un bail non obtenu doit se voir.
        for n in (db_get_nodes() or []):
            if sortie_possible(n)[0] and etat_sortie(n).get("passerelle"):
                return True
        return False
    raise RuntimeError("état inconnu : %s" % state_id)
