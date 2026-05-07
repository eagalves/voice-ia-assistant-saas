# Presets Operacionais de Latência

Este arquivo documenta três configurações predefinidas para ajustar o trade-off entre
velocidade de resposta e qualidade de transcrição/geração.

Copie o bloco desejado para o seu `.env` e ajuste conforme o hardware disponível.

---

## Resumo comparativo

| Variável                    | baixa_latencia   | balanceado     | qualidade       |
|-----------------------------|-----------------|----------------|-----------------|
| `WHISPER_MODEL`             | `base`          | `small`        | `medium`        |
| `WHISPER_FINAL_MODEL`       | `base`          | `small`        | `medium`        |
| `WHISPER_PARTIAL_MODEL`     | `tiny`          | `tiny`         | `base`          |
| `WHISPER_BEAM_SIZE`         | `1`             | `3`            | `5`             |
| `STT_CONFIDENCE`            | `0.55`          | `0.65`         | `0.80`          |
| `STT_MAX_RETRIES`           | `1`             | `1`            | `2`             |
| `OLLAMA_MODEL`              | `qwen2.5:0.5b`  | `qwen2.5:3b`   | `qwen2.5:7b`    |
| `OLLAMA_TIMEOUT_S`          | `15`            | `30`           | `60`            |
| `LLM_RETRIES`               | `1`             | `2`            | `2`             |
| `LLM_RETRY_BACKOFF_BASE_S`  | `0.5`           | `1.0`          | `1.0`           |
| `LLM_RETRY_BACKOFF_MAX_S`   | `5.0`           | `10.0`         | `10.0`          |
| Perfil médico?              | **NÃO**         | com ressalva   | **SIM**         |
| Latência estimada (CPU)     | 3–8s            | 8–20s          | 20–60s+         |
| Latência estimada (GPU)     | < 2s            | 2–5s           | 4–12s           |

> **Nota sobre SLO**: o SLO de p95 < 4s é praticamente impossível de atingir com
> inferência local em CPU para qualquer preset. Para atender ao SLO, use GPU ou
> substitua `LLM_PROVIDER=ollama` por `LLM_PROVIDER=openai` ou `gemini`.

---

## Preset 1 — baixa_latencia

**Objetivo**: minimizar tempo de primeira resposta em hardware limitado.  
**Trade-off**: WER pode se aproximar do limite de 10%; respostas do LLM menos elaboradas.  
**Quando usar**: smoke tests, demos rápidos, entrevistas gerais em CPU lento.  
**Quando NÃO usar**: perfil `medical` — WER de `base` com `beam_size=1` pode comprometer precisão clínica.

```dotenv
# ── STT ──────────────────────────────────────────────────────────────────────
WHISPER_MODEL=base
WHISPER_FINAL_MODEL=base
WHISPER_PARTIAL_MODEL=tiny
WHISPER_BEAM_SIZE=1
WHISPER_DEVICE=cpu
WHISPER_COMPUTE=int8

STT_LANGUAGE=pt
STT_CONFIDENCE=0.55
STT_MAX_RETRIES=1

# ── Microfone ─────────────────────────────────────────────────────────────────
MIC_SAMPLE_RATE=16000
MIC_CHUNK_FRAMES=1024

# ── LLM (Ollama local) ────────────────────────────────────────────────────────
LLM_PROVIDER=ollama
OLLAMA_MODEL=qwen2.5:0.5b
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_TIMEOUT_S=15

LLM_RETRIES=1
LLM_RETRY_BACKOFF_BASE_S=0.5
LLM_RETRY_BACKOFF_MAX_S=5.0

LLM_FALLBACK_ENABLED=0
```

---

## Preset 2 — balanceado

**Objetivo**: equilíbrio entre latência e qualidade aceitável para uso geral.  
**Trade-off**: latência de 8–20s em CPU; WER dentro do SLO para pt-BR com `small`.  
**Quando usar**: entrevistas clínicas, uso diário em CPU moderno ou GPU fraca.  
**Quando usar com ressalva**: perfil `medical` — aceitável apenas se `STT_CONFIDENCE` for elevado
manualmente para `0.80` no `.env` (sobrepõe este preset para o campo STT).

```dotenv
# ── STT ──────────────────────────────────────────────────────────────────────
WHISPER_MODEL=small
WHISPER_FINAL_MODEL=small
WHISPER_PARTIAL_MODEL=tiny
WHISPER_BEAM_SIZE=3
WHISPER_DEVICE=cpu
WHISPER_COMPUTE=int8

STT_LANGUAGE=pt
STT_CONFIDENCE=0.65
STT_MAX_RETRIES=1

# ── Microfone ─────────────────────────────────────────────────────────────────
MIC_SAMPLE_RATE=16000
MIC_CHUNK_FRAMES=1024

# ── LLM (Ollama local) ────────────────────────────────────────────────────────
LLM_PROVIDER=ollama
OLLAMA_MODEL=qwen2.5:3b
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_TIMEOUT_S=30

LLM_RETRIES=2
LLM_RETRY_BACKOFF_BASE_S=1.0
LLM_RETRY_BACKOFF_MAX_S=10.0

LLM_FALLBACK_ENABLED=0
```

---

## Preset 3 — qualidade

**Objetivo**: máxima precisão de STT e respostas mais elaboradas do LLM.  
**Trade-off**: latência alta em CPU (20–60s+); requer GPU ou provider de API para SLO.  
**Quando usar**: perfil `medical`, consultas reais, ambiente com GPU ou LLM via API.  
**Quando NÃO usar**: smoke tests, máquinas sem GPU rodando modelos grandes.

```dotenv
# ── STT ──────────────────────────────────────────────────────────────────────
WHISPER_MODEL=medium
WHISPER_FINAL_MODEL=medium
WHISPER_PARTIAL_MODEL=base
WHISPER_BEAM_SIZE=5
WHISPER_DEVICE=cpu
WHISPER_COMPUTE=int8

STT_LANGUAGE=pt
STT_CONFIDENCE=0.80
STT_MAX_RETRIES=2

# ── Microfone ─────────────────────────────────────────────────────────────────
MIC_SAMPLE_RATE=16000
MIC_CHUNK_FRAMES=1024

# ── LLM (Ollama local — trocar por openai/gemini para SLO) ───────────────────
LLM_PROVIDER=ollama
OLLAMA_MODEL=qwen2.5:7b
OLLAMA_BASE_URL=http://localhost:11434
OLLAMA_TIMEOUT_S=60

LLM_RETRIES=2
LLM_RETRY_BACKOFF_BASE_S=1.0
LLM_RETRY_BACKOFF_MAX_S=10.0

# Opcional: fallback para API quando Ollama indisponivel
LLM_FALLBACK_ENABLED=1
LLM_FALLBACK_PROVIDER=gemini
LLM_NOTIFY_FALLBACK=1
```

---

## Preset para perfil médico com API

Combinação recomendada quando o SLO de 4s é obrigatório e há chave de API disponível.

```dotenv
# ── STT ──────────────────────────────────────────────────────────────────────
WHISPER_MODEL=small
WHISPER_FINAL_MODEL=small
WHISPER_PARTIAL_MODEL=tiny
WHISPER_BEAM_SIZE=5
WHISPER_DEVICE=cpu
WHISPER_COMPUTE=int8

STT_LANGUAGE=pt
STT_CONFIDENCE=0.80
STT_MAX_RETRIES=2

# ── LLM (API — atende SLO p95 < 4s) ─────────────────────────────────────────
LLM_PROVIDER=gemini
GEMINI_API_KEY=<sua-chave>
GEMINI_MODEL=gemini-2.0-flash

LLM_RETRIES=2
LLM_RETRY_BACKOFF_BASE_S=1.0
LLM_RETRY_BACKOFF_MAX_S=10.0

LLM_FALLBACK_ENABLED=0
```

---

## Alavancas de latência por módulo

### STT — maior impacto

| Modelo Whisper | Tamanho  | Latência típica (CPU) | WER pt-BR (aprox.) |
|----------------|----------|-----------------------|---------------------|
| `tiny`         | 39M      | 0.2–0.5s             | ~15–20% (alto)      |
| `base`         | 74M      | 0.4–1.0s             | ~10–12%             |
| `small`        | 244M     | 1.0–2.5s             | ~6–8%               |
| `medium`       | 769M     | 3.0–6.0s             | ~4–5%               |
| `large`        | 1.5G     | 6.0–12.0s            | ~3–4%               |

`WHISPER_BEAM_SIZE=1` reduz latência em ~30–50% com pequena perda de WER.

### LLM — impacto dependente de hardware

Com CPU apenas:
- Modelos < 1B params: 2–5s
- Modelos 3B: 8–20s
- Modelos 7B: 20–60s+

Com GPU (ex.: RTX 3060 12GB):
- Modelos 3B: 1–3s
- Modelos 7B: 3–8s

Provider de API (Gemini Flash, GPT-4o-mini): 1–3s independente de hardware local.

---

## Como medir o impacto de um preset

```bash
# Teste de texto (sem STT) — isola latência do LLM
python -m assistente_voz.voice_cli --text "Estou com dor de cabeça forte há dois dias"

# Teste de voz (30s) — latência ponta a ponta
python -m assistente_voz.voice_cli --duration 30
```

Os logs exibem `latency=XXXms` por resposta. Colete 10+ amostras e calcule o p95.

Para WER: rode a bateria de frases da Issue 13 com o preset escolhido e compare
o resultado com o SLO de WER ≤ 10%.
