# ADR 0002 — Whisper local (CPU) como STT de desenvolvimento e backend batch interino do servidor, configurado para acurácia

- **Status**: Aceito
- **Data**: 2026-10-27
- **Relacionada**: ADR 0001 (Deepgram Nova-2 continua sendo o STT primário de produção da Fase 2)

## Contexto

A ADR 0001 fixa Deepgram Nova-2 streaming como STT primário da Fase 2 por elasticidade e WER em pt-BR clínico. Nenhum provider gerenciado pode ser ligado antes do DPA (Issue #12), e o componente precisava de um transcritor real para: (a) o Voice CLI de desenvolvimento, (b) a bateria de qualidade reproduzível em CI (fixtures TTS pt-BR com ground truth), e (c) o tracer bullet ponta a ponta do servidor (WS → transcrição → draft) sem credenciais externas. A GPU da máquina de desenvolvimento é inutilizável (driver antigo), então tudo roda em CPU.

## Decisão

Manter `faster-whisper` local, em CPU, como implementação de referência do contrato `WhisperBackend` do MVP e expô-lo ao servidor da Fase 2 atrás do mesmo protocolo `BatchSTTBackend` (`LocalWhisperBatchBackend` + adaptador `BatchOnlyStreamingBackend`), selecionável por `STT_SERVER_BACKEND=local_whisper`. A configuração padrão prioriza acurácia sobre latência: escada de temperatura (re-decode quando falham `compression_ratio`/`avg_logprob`), normalização de loudness e remoção de DC, filtro de alucinações pt-BR (boilerplate de legendas) com supressão de eco do `initial_prompt`, `initial_prompt`/`hotwords` com vocabulário clínico, resample 44.1/48 kHz → 16 kHz com FIR anti-aliasing. `small` é o modelo do caminho ao vivo; `medium` o do caminho batch/benchmark.

## Alternativas rejeitadas

- **Esperar o Deepgram para ter qualquer transcrição no servidor**: deixaria a integração com o app de telemedicina sem caminho demoável e sem bateria de regressão até a assinatura do DPA.
- **`temperature=0.0` fixo (config anterior)**: menor latência, mas alucina em silêncio ("O que é o que é…") nos modelos `tiny`/`small` e não recupera decodes ruins; o benchmark A/B em `scripts/benchmark_stt.py` mostrou WER 0.137 → 0.000 em captura baixa com a config nova (`small`).
- **GPU/CUDA**: indisponível no ambiente atual; a decisão de servir STT self-hosted em produção continua regida pelo gatilho de 200 mil min/mês da ADR 0001.

## Consequências

- Em CPU, um segmento de 4 s leva segundos para transcrever: o servidor agrupa frames por janela/silêncio (`SessionTranscriber`) e usa timeout de 120 s por segmento neste backend, não os 800 ms do Deepgram. O SLO "≤ 15 s por bloco" é meta apenas para o backend gerenciado.
- A qualidade medida (`scripts/benchmark_results_*.json`) é sobre TTS sintético (Microsoft Maria), não fala espontânea de consulta; a bateria de 50 frases clínicas com Deepgram (Issue #11) continua obrigatória antes do go-live.
- O caminho local é a opção de "soberania de dados" prevista na ADR 0001 (A3/A5 reabertos como SKU on-prem) e, por isso, continua mantido mesmo após a entrada do Deepgram.
