# ADR-F09 — HALDEN XML is parsed by the frozen v1 bridge (trust assumption carried from v1 ADR-8)

- Status: Accepted (RK-12; carries v1 ADR-8)
- Context: the HALDEN adapter wraps the v1 bridge, imported byte-for-byte and frozen (ADR-F01, `make verify-imports`).
  The bridge parses HALDEN's XML item pages with the standard library `xml.etree.ElementTree`
  (`backend/src/lbf/adapters/halden/v1bridge/client.py`, `parse_items_page`). Changing it would break the
  byte-identity that lets reviewers trust the import.
- Decision: keep the verbatim parser and treat HALDEN as a **trusted internal system** for XML well-formedness: it is
  the customer's own ERP on the internal network (here a simulator on the compose network), not an internet party.
  Every HALDEN response is still untrusted *data*: it is never executed, its text is escaped in the console, and text
  stored as evidence is made storable (control characters escaped, `storable_text`, defect D7-1).
- Size limit at transport (RK-12 mitigation): implemented.
  `CapturingTransport` (`backend/src/lbf/adapters/halden/transport.py`, outside the frozen bridge) refuses any HALDEN
  response body above `MAX_RESPONSE_BYTES` = 1 MiB (decoded): a larger declared `Content-Length` is refused before the
  body is read, other bodies are read incrementally and refused once over the limit, and a malformed
  `Content-Length` is refused. Nothing is truncated. The refusal is a `LegacyMalformedResponse`: reads fail as
  `BACKEND_MALFORMED` without a retry; an E8 whose response is refused is `UNCERTAIN` (never `NOT_DELIVERED`, never
  resent). Remaining: entity expansion *within* a body under the cap is still not bounded by the frozen parser
  ([KNOWN_LIMITATIONS](../KNOWN_LIMITATIONS.md) L-05).
- Contrast: SKARVIK acknowledgements arrive as files on a share that other parties can write; they are parsed with
  `defusedxml` (DTDs, entities and external references forbidden; 64 KiB cap) and hostile files are tested (SEC-11).
- Consequences: a compromised HALDEN could attempt XML entity expansion against the application; stdlib
  ElementTree does not resolve external entities, but internal entity expansion is not bounded. Mitigation if HALDEN
  were ever outside the trust boundary: a wrapping parser with `defusedxml` and a response size cap in the adapter
  (outside the frozen bridge).
