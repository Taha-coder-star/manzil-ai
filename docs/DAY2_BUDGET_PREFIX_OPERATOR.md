# Preserve preceding budget operators

D2-06: English voice V03 contained an up-to signal, but the budget update saved around8crore (7.2-8.8crore). The response and search used that range and returned an above-budget9.5crore option. The parent normalizer already reads operators following an extracted amount in its own message, but evidence carried no preceding text.

Carry paired before/after context for each occurrence from the same verified source message. Read only known operators immediately before an amount span when the span has no operator itself. Keep existing repeated-amount disagreement handling: ask instead of choosing. Do not read an operator from another message or earlier clause. No model-generated value, changed tolerance or expanded search policy.

Tests cover prefix max/min/around, unrelated prefixes, a full English speech-chain request whose tool budget span omits up-to, cross-message isolation and conflicting repeated occurrences. The end-to-end English test verifies max8crore with no lower bound and matching search filters. Live evidence is in workspace output/day2-english-budget-operator-evidence.json; transcripts are not exported. No migrations, provider settings or production data changes. CI/review/deployment/live rerun remain required.
