# Evidências do Gate DoD 2.0

`tests/fase2/test_gate_dod20.py` grava `dod20_evidence_<timestamp>.json` a cada execução
(Issue #11). Os arquivos são artefatos gerados: desde 2026-10-27 novos arquivos são
ignorados pelo git (`.gitignore`) e a evidência que embasa a decisão de avançar para a 2.1
deve ser anexada ao registro de release (ou commitada explicitamente com `git add -f`).

Os sete arquivos de 2026-05-10 já versionados são a evidência histórica da entrega das
issues #1–#12 e permanecem como estão.

Observação: os checks 1, 2 e 8 da suíte são estruturais; o caminho ponta a ponta real
(WS → transcrição → draft) é coberto por `tests/fase2/test_issue014_stt_pipeline.py`.
