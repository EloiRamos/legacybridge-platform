# HALDEN ERP 3.1 — import provenance

HALDEN ERP 3.1 is a purpose-built **simulator** of a fictional legacy ERP. It was built
in LEGACYBRIDGE v1. This project reuses it **unchanged**.

| Item | Value |
|---|---|
| Source repository | LEGACYBRIDGE v1 published candidate |
| Source revision | `0a896d10c0d702ea295f7666d632237c80781b2a` (tree `b44d81a48da60b9ad7ad620d1f87f421d7da3be7`) |
| Source path | `legacy-erp/` (35 tracked files) |
| Destination | `legacy-systems/halden-erp/` (byte-identical copy) |
| Also imported | `docs/LEGACY_API.md` → `docs/contracts/HALDEN_LEGACY_API.md` (byte-identical) |
| Manifest | `legacy-systems/HALDEN_IMPORT.sha256` (36 entries: 35 HALDEN files + the API document) |
| Verification | `shasum -a 256 -c legacy-systems/HALDEN_IMPORT.sha256` and `python3 tools/verify_imports.py` (cross-checks every hash against the git blob at the source revision) |

## Relationship to the earlier manifest

The predecessor's earlier manifest (35 entries) matches the published revision for 32 files.
Three files differ by documentation-only edits made before publication:

| File | Published (imported) SHA-256 | Nature |
|---|---|---|
| `README.md` | `f06b9e540e4efb28b41663366ada69edae9dda1517cc468513e76591232fa0b1` | container name wording |
| `acceptance/http_acceptance.sh` | `273a583e6488e65651d408e2747c6c92a140de5a92f17ceeb6aab25db32b7ec6` | observation-file heading text |
| `src/test/java/com/brightwater/halden/GatewayRouteClassificationContractTest.java` | `464dffac940a5a7b83c0bc30faeb1dfe19bc1fdfe0e0fbd1db018e581f941fd0` | Javadoc wording |

No production Java source differs. This project binds to the published bytes.

## Image

The image is rebuilt from the imported bytes (`docker build -t lbf/halden-erp:3.1-platform
legacy-systems/halden-erp`); the 23 Java contract tests run inside the build. The rebuilt image is
not claimed to equal the image of the predecessor.

## Rules

- No file under `legacy-systems/halden-erp/` is ever edited.
- The v1 Python bridge (`errors.py`, `models.py`, `normalize.py`, `client.py`) is imported the same
  way into `backend/src/lbf/adapters/halden/v1bridge/` with its own manifest
  (`backend/src/lbf/adapters/halden/V1BRIDGE_IMPORT.sha256`); the only added file there is an empty
  `__init__.py`.
