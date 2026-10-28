# ADR 0003 — Pós-correção de transcrição por LLM como lista de edições, nunca reescrita

- **Status**: Aceito
- **Data**: 2026-10-27
- **Módulo**: `src/assistente_voz/stt_correction.py` (`STT_POSTCORRECT_*`)

## Contexto

O Whisper erra de forma característica em pt-BR clínico: trocas por similaridade fonética ("lousa há tanas" → "losartana"), segmentação errada e termos técnicos grafados errado. Um LLM que lê a frase inteira detecta isso pelo contexto. O caminho óbvio — pedir ao LLM "o texto corrigido" — produziu paráfrases em modelos pequenos (qwen2.5:3b troca "depois do" por "após o" mesmo instruído a não fazê-lo), o que é inaceitável para evidência clínica: fidelidade ao que foi dito vale mais que fluência.

## Decisão

O LLM devolve apenas um array JSON `[{"de": "trecho exato", "para": "correção"}]` e o programa aplica as edições, sob guardrails por edição (o trecho precisa existir literalmente e como palavra inteira; `para` vazio é rejeitado; no-op ignorado; no máximo 8 edições de até 4 palavras; similaridade fonética ≥ 0,40 entre `de` e `para`) e no resultado (distância de edição em palavras ≤ `max(4, 0,35 × nº de palavras)`). Qualquer falha de LLM ou parse é fail-open: a transcrição original é mantida. Transcrições com confiança ≥ 0,85 pulam a correção. Decodificação determinística (`temperature=0`) no Ollama.

## Alternativas rejeitadas

- **Reescrita completa pelo LLM**: impossível impedir paráfrase estrutural por prompt; qualquer deslize troca o registro do paciente.
- **Correção embutida no STT (hotwords/`initial_prompt` apenas)**: ajuda, mas não corrige segmentação errada nem erros fora do vocabulário listado; as duas técnicas são complementares (ADR 0002).
- **Diff livre pós-reescrita com limiar de WER**: detecta paráfrase só depois do fato e perde correções legítimas em frases curtas ("Tomo los artanas." → WER 0,67 legítimo).

## Consequências

- A correção é local e auditável: `CorrectionResult` registra edições aplicadas/rejeitadas e divergência; códigos `CORRECTION_*` entram no registro de erros estáveis.
- Correções que exigem reescrever mais de 4 palavras contíguas nunca serão aplicadas — limitação aceita.
- Latência do LLM entra no caminho entre finais consecutivos no Voice CLI; o recurso é opt-in (`STT_POSTCORRECT_ENABLED=1`) e indicado para ditado de prontuário, não para cadência conversacional.
