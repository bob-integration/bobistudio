***REMOVED*** Brouillon d'Issue AMWA — `urn:x-nmos:transport:mxl` absent du registre des transports

⚠ **NON PUBLIÉE.** À ouvrir sur <https://github.com/AMWA-TV/nmos-parameter-registers/issues>
après relecture. Nous ne sommes pas membres AMWA : Issues oui, **pas de Pull Request**
(décision consignée — l'adhésion est décidée mais pas effective).

⚠ Une première rédaction attribuait l'entrée `usb` à BCP-007-02 : le commit du registre dit
« BCP-005-03 ». Vérifié et corrigé — dans une Issue publique, on cite le dépôt, on ne déduit pas.

Rédigée le 2026-09-09, après vérification sur le dépôt (`transports/README.md`, branche `main`)
ET sur la page publiée `specs.amwa.tv/nmos-parameter-registers/branches/main/transports/` :
huit URN listés, `mxl` n'y figure pas.

---

**Titre :** Transports register: `urn:x-nmos:transport:mxl` missing after BCP-007-03 v1.0.0

**Corps :**

BCP-007-03 "NMOS Support for MXL" was released as v1.0.0 on 2026-08-18 and normatively requires
the transport URN:

> An MXL Sender resource MUST set the `transport` attribute to `urn:x-nmos:transport:mxl`.
> An MXL Receiver resource MUST set the `transport` attribute to `urn:x-nmos:transport:mxl`.

However `urn:x-nmos:transport:mxl` does not appear in the Transports register. As of 2026-09-09 the register lists eight URNs (`rtp`, `rtp.mcast`, `rtp.ucast`, `dash`, `mqtt`, `websocket`, `ndi`,
`usb`), checked both in `transports/README.md` on `main` and on the published page.

This matters for interoperability rather than for us knowing what to emit — the URN is fixed by a
released specification, so there is no ambiguity about the value. The difficulty is on the
Controller side: `urn:x-nmos:` is the NMOS namespace, so an implementation emitting an
unregistered URN from that namespace is not covered by the register's allowance for
manufacturer-specific transports ("Manufacturers MAY use their own namespaces to indicate
transports which are not currently defined within the NMOS namespace"). A strict Controller
validating against the register may therefore reject otherwise-conformant resources.

The precedent suggests the entry is added by hand rather than as part of the specification's own
pull request: `urn:x-nmos:transport:usb` was added on 2025-10-23 in a standalone commit (0714e4e)
whose message reads "add the urn:x-nmos:transport:usb transport that seems to be missing in the
… pull-request". Searching the whole repository history, no commit on any branch has ever
introduced the string `transport:mxl`.

Would you like a PR adding the entry, or is one already in preparation? We are happy to supply
the details but are not currently AMWA members, so we are raising this as an issue rather than
opening a pull request.

A related but separate question — whether a planar uncompressed video type belongs in the Media
Types register — is raised in its own issue, so that this one, which is a straightforward
omission, is not held up by a discussion.

For context, we have an implementation serving BCP-007-03 resources (IS-04 v1.3 / IS-05 v1.2,
`mxl_domain_id` + `mxl_flow_id`, `domain_def.json`, BCP-004-01 receiver capabilities) and
validating against the specification's own JSON schemas.
