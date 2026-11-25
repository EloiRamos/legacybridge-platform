# SKARVIK OES 4 (simulator)

Fictional order-exchange system of the fictional Norrvik Industrial AB, simulated for the LEGACYBRIDGE Flagship.
Contract: `docs/contracts/SKARVIK_EXCHANGE_CONTRACT.md` (`skarvik-exchange/1.0`). Frozen by `SKARVIK_MANIFEST.sha256`.

```
uv run pytest -q          # simulator tests (invariants S-I1…S-I7, every fault, control plane)
docker build -t lbf/skarvik-oes:4-flagship .
```
