# Sonde 2110 (analyseur de flux)

Analyseur de flux ST 2110 : « un receiver dont le but est la mesure ». La sonde s'abonne à n'importe quel flux vidéo déclaré au registre NMOS et rend en direct un **verdict de conformité ST 2110-21** (narrow / wide / hors norme) avec les métriques de timing réseau (Cinst, VRX, FPT, latence) et de transport. Elle sert aussi de base à la **surveillance longue durée** des signaux de production.

## Déployer une sonde

Depuis la page **Sonde** (`/probe`) : choisir un nœud et une **interface média dédiée** — la sonde exige sa propre carte/port réseau 2110, jamais celui du moteur 2110. Le verdict de conformité complet (Cinst/VRX) exige une interface en mode DPDK (horodatage matériel) ; en AF-XDP, seuls le transport et le contenu sont mesurés.

## Analyser un flux

1. La page **Sonde** liste les flux abonnables du registre NMOS (sorties 2110 locales et senders découverts sur le réseau).
2. Braquer la sonde sur un flux : elle s'y abonne (IS-05) et le rapport s'affiche en direct.
3. Lire le verdict : **narrow** (émetteur au gabarit strict), **wide** (toléré mais lâche), **hors norme** (avec la cause), plus les métriques Cinst/VRX/FPT/latence et les compteurs de transport (pertes, débit).

> **PTP requis pour le verdict absolu.** Sans horloge de référence commune (grandmaster PTP) entre l'émetteur et le nœud de la sonde, les mesures absolues (FPT, latence) ne sont pas significatives — se fier alors à Cinst et à l'amplitude VRX, qui restent valables.

## Mesurer l'audio

**Mesurer aussi la conformité audio (ST 2110-30)** étend le verdict au flux audio du même
sender (quand il en a un) : sans cette option, seule la vidéo (2110-20) est analysée.

## Surveillance longue durée

La section **Surveillance longue durée** de la page Sonde tient un **journal d'incidents horodatés** (entrée/sortie de chaque incident) : passage narrow→wide ou hors norme, gel d'image, image noire, silence audio, pertes de paquets, perte de verrouillage PTP du nœud, sonde injoignable. Les incidents remontent aussi en alertes sur le tableau de bord.

Les sondes déployées sont surveillées d'office ; un **receiver de production** (moteur 2110) peut aussi être marqué « surveillé » pour bénéficier du même journal, sans sonde dédiée (métriques de transport et de contenu seulement).

## Notes

- La sonde est **ponctuelle et sans impact** : elle ne fait que recevoir une copie multicast du flux ; on peut l'abonner et la désabonner à volonté.
- Une seule sonde par interface dédiée ; elle peut coexister avec le moteur 2110 du nœud (chacun sur sa propre carte).
