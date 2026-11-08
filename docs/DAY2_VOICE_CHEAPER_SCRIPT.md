# Devanagari cheaper voice commands

D2-05: the 8 November V02 voice follow-up was transcribed successfully with a Devanagari cheaper signal, but neither the raw nor romanised command matched the standalone cheaper guard. No budget patch was saved and the search reused version42 / 8crore, repeating listings. The previous typed follow-up worked.

The guard previously tokenized only Roman Urdu/English. Existing normalise.romanise translates known Devanagari budget words but leaves command fillers such as some/show untranslated. Normalize through that existing budget vocabulary and a small command-local filler map before applying the same standalone allowlist. Return the original exact span for grounded requirements updates. Unknown words, extra requirements, negations, conditions and questions continue to require model extraction.

Pure tests cover Devanagari and mixed script affirmative commands plus negative/conditional/mixed requests. Real speech-chain tests with fake Scribe cover omitted model update, repeated searches and explicit update idempotency. A synthetic Devanagari cheaper phrase fails recognition on the deployed parent and passes locally; the production transcript was not exported.

No model/STT/provider settings, prices, migrations or data change. Candidate requires CI, review, deployment and failing-case live rerun. Formal voice cohort remains failed at V02 until that acceptance.
