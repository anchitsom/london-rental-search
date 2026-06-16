"""EPC address-match threshold.

History: 0.75 (sweep-generated, too strict) to 0.60 (boundary recall) to 0.55
(Wave 1.7 round 1 audit).

The Wave 1.7 round 1 audit (epc_enrichment/REPORT.md) confirmed zero false
positives on the 35-listing control set when comparing 0.55 vs 0.60. Six of
fifteen null-EPC listings were rescued at 0.55 that 0.60 missed. The wider
unit gate in homehunt/epc_unit_gate.py is the precision floor; the threshold
controls recall on boundary residential matches.

The per-bedroom sanity rule in size_merge.py (size_sqft / bedrooms <= 800)
continues to protect against EPC size mismatches.
"""

EPC_MATCH_THRESHOLD: float = 0.55
