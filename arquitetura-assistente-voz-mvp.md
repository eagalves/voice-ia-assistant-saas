# Arquitetura MVP - Assistente de Voz (Local + Escalavel)

## Objetivo
Construir um assistente **simples** para uso local que:
- escuta sua fala no microfone,
- transcreve com **STT local**,
- envia texto para **LLM via API key**,
- responde **apenas em texto**.

A base deve permitir troca futura de provedores (local ou API) com poucas alteracoes.

---

## Principios de Design
1. **Coesao alta por modulo**: cada modulo tem uma responsabilidade clara.
2. **Baixa interdependencia**: modulos conversam por contratos simples.
3. **Sem overengineering**: nada de microsservicos ou filas no MVP.
4. **Troca de provedor por configuracao**: sem reescrever fluxo principal.

---

## Escopo do MVP (Agora)
- Fonte de audio: microfone local.
- STT: local.
- LLM: remoto via API key.
- Saida: texto na tela.
- Memoria: historico curto da sessao.

Fora do escopo inicial:
- TTS (resposta em voz).
- Integracao com Teams/chamadas.
- Multiusuario.
- Deploy em nuvem.

---

## Modulos (coesos e com baixa interdependencia)

### 1) AudioInput
**Responsabilidade**
- Capturar audio do microfone local.
- Detectar inicio/fim de fala (VAD simples).

**Entradas**
- Configuracao de dispositivo de microfone.

**Saidas**
- Trechos de audio prontos para transcricao.

**Dependencias**
- Nenhuma dependencia dos modulos de IA.

---

### 2) STTAdapter
**Responsabilidade**
- Converter audio em texto.
- Expor resultado padronizado para o orquestrador.

**Contrato (conceitual)**
- Entrada: trecho de audio (bytes PCM ou ndarray float32, resamplado para 16 kHz antes do envio).
- Saida: texto transcrito, idioma (opcional), confianca (float 0-1).

**Implementacoes previstas**
- `LocalSTT` (agora): modelo Whisper via `faster-whisper`, carregado como singleton por processo.
- `ApiSTT` (futuro): STT por key.

**Politica de confianca**
- `STT_CONFIDENCE_WARN` (padrao 0,80): aceitar trecho com aviso visual na UI.
- `STT_CONFIDENCE_BLOCK` (padrao 0,60): bloquear e solicitar repeticao.
- Para perfil `medical`: `STT_CONFIDENCE_BLOCK` nao pode ser configurado abaixo de 0,70.

**Regras**
- O orquestrador conhece apenas o contrato STT, nunca a implementacao concreta.
- Log de WARNING obrigatorio quando sample rate de captura diferir de 16 kHz.

---

### 3) ConversationState
**Responsabilidade**
- Manter contexto curto da sessao (ultimas mensagens).
- Controlar janela de contexto por numero de mensagens (`MAX_CONTEXT_MESSAGES`) **e** por estimativa de tokens (`MAX_CONTEXT_TOKENS`).

**Entradas**
- Mensagem do usuario (vinda do STT).
- Resposta do assistente (vinda do LLM).

**Saidas**
- Historico resumido para o LLM.

**Regra de truncagem**
- Descarte prioriza mensagens mais antigas.
- System prompt e mensagem mais recente do usuario sao sempre preservados.
- Logger emite evento quando truncagem por tokens for acionada.

**Dependencias**
- Sem dependencia direta de AudioInput, STT ou LLM.

---

### 4) LLMAdapter
**Responsabilidade**
- Enviar prompt/contexto ao modelo e retornar resposta textual.

**Contrato (conceitual)**
- Entrada: prompt atual + historico de conversa.
- Saida: resposta em texto (streaming ou completa) + metadados (latencia total, `time_to_first_token`, tokens, provider e modelo usados).

**Implementacoes previstas**
- `ApiLLM` (agora): provider remoto via key (OpenAI, Gemini).
- `OllamaLLM` (implementado): modelo local via HTTP.

**Politica de retries**
- Apenas erros tecnicos (`LLM_ERR_UNAVAILABLE`) disparam retry.
- Backoff exponencial com jitter: base `LLM_RETRY_BACKOFF_BASE_S`, cap `LLM_RETRY_BACKOFF_MAX_S`.
- Erros logicos (`LLM_ERR_LOGICAL`) nao tem retry e nao acionam fallback.

**Regra**
- O orquestrador usa apenas o contrato LLM.
- Streaming de tokens deve ser suportado quando o provider permitir.

---

### 5) Orchestrator
**Responsabilidade**
- Fluxo ponta a ponta com regras simples:
  1. receber audio,
  2. pedir transcricao ao STT,
  3. atualizar estado da conversa,
  4. chamar LLM,
  5. devolver texto para UI,
  6. registrar metricas basicas.

**Dependencias**
- Depende de contratos (`STTAdapter`, `LLMAdapter`, `ConversationState`).
- Nao depende de implementacoes concretas.

---

### 6) TextUI
**Responsabilidade**
- Interface minima do usuario.
- Exibir status e historico textual.

**Tela minima**
- Botao iniciar/parar.
- Campo de transcricao ao vivo.
- Campo de resposta da IA.
- Status: ouvindo, transcrevendo, processando, erro.

**Dependencias**
- So conversa com o Orchestrator.

---

### 7) Config
**Responsabilidade**
- Centralizar configuracoes de ambiente.

**Chaves sugeridas**
- `STT_PROVIDER=local|api`
- `LLM_PROVIDER=api|local`
- `LLM_API_KEY=...`
- `LANG=pt-BR`
- `MAX_CONTEXT_MESSAGES=<numero>`

**Regra**
- Nenhuma key hardcoded no codigo.

---

### 8) Logger/Metrics (basico)
**Responsabilidade**
- Registrar eventos essenciais para operacao e evolucao.

**Metricas minimas**
- Latencia de STT.
- Latencia de LLM.
- Erros por modulo.
- Contagem de requisicoes.

**Regra**
- Sem logs com dados sensiveis completos.

---

## Dependencias entre Modulos (visao simples)
TextUI -> Orchestrator -> (STTAdapter, LLMAdapter, ConversationState, Logger)
AudioInput -> Orchestrator

Observacoes:
- `AudioInput` nao chama LLM.
- `STTAdapter` nao conhece UI.
- `LLMAdapter` nao conhece audio.
- Isso reduz acoplamento e facilita troca de tecnologia.

---

## Fluxo do MVP (runtime)
1. Usuario inicia escuta na UI.
2. AudioInput capta fala e gera trecho.
3. Orchestrator envia trecho ao STTAdapter.
4. STT retorna texto.
5. ConversationState atualiza historico.
6. Orchestrator chama LLMAdapter com contexto.
7. LLM retorna resposta textual.
8. UI mostra transcricao e resposta.

---

## Estrategia de Evolucao sem refatoracao pesada

### Fase A (agora)
- `STT_PROVIDER=local`
- `LLM_PROVIDER=api`
- Interface textual e fluxo unico.

### Fase B (futuro proximo)
- Adicionar `ApiSTT` mantendo mesmo contrato.
- Adicionar fallback de LLM provider por configuracao.

### Fase C (nuvem)
- Manter contratos.
- Substituir implementacoes locais por servicos quando preciso.
- Escalar Orchestrator sem mudar semantica dos modulos.

---

## Decisoes para evitar overengineering
1. Um processo/aplicacao unica no MVP.
2. Sem event bus/fila neste momento.
3. Sem banco complexo: memoria de sessao em RAM e opcional arquivo local simples.
4. Contratos pequenos e estaveis entre modulos.
5. Interface minimalista.

---

## Checklist de Qualidade do MVP
- [ ] STT local funcionando em pt-BR.
- [ ] LLM remoto funcionando com API key.
- [ ] Resposta textual ponta a ponta.
- [ ] Chaves em variavel de ambiente.
- [ ] Latencia e erros logados.
- [ ] Troca de provider por configuracao (sem mudar orquestrador).

---

## Resumo Executivo
Arquitetura enxuta: **AudioInput + STTAdapter + ConversationState + LLMAdapter + Orchestrator + TextUI + Config + Logger**.

Com isso, voce comeca local e simples hoje, mantendo a porta aberta para trocar STT/LLM entre local e API, e subir para nuvem depois sem reescrever o nucleo.

---

## Aditivo Pos-Issues (estado implementado)

Este aditivo registra evolucoes feitas apos a rodada base de issues 1-13.

### LLM providers atualmente suportados
- `LLM_PROVIDER=api|openai|gemini|ollama`.
- OpenAI, Gemini e Ollama coexistem no mesmo contrato de `LLMAdapter`/`LLMService`.
- O boot valida chave por provider remoto (`LLM_API_KEY` ou `GEMINI_API_KEY`) e regras de fallback.

### LLM local implementado
- Provider local: `LLM_PROVIDER=ollama`.
- Transporte: HTTP local para `OLLAMA_BASE_URL`.
- Formato: API de chat com mensagens estruturadas (`system`, `user`, `assistant`).
- Modelos separados por provider:
  - `OPENAI_MODEL`
  - `GEMINI_MODEL`
  - `OLLAMA_MODEL`
- Fallback opcional por chamada:
  - `LLM_FALLBACK_ENABLED=1`
  - `LLM_FALLBACK_PROVIDER=gemini|openai`
- Regra de fallback: apenas para erros tecnicos de disponibilidade do provider primario.
- Notificacao em desenvolvimento: exibir aviso de fallback quando `LLM_NOTIFY_FALLBACK=1`.
- Retries permanecem globais em `LLM_RETRIES`; timeout do Ollama fica especifico em `OLLAMA_TIMEOUT_S`.
- A resposta do servico expoe provider e modelo realmente usados.

### Proximos incrementos planejados
- Issue 19: testes de regressao para assertividade do perfil medical (anti-inferencia). **[Done]**
- Issue 20: encerramento limpo do worker de LLM no `voice_cli`.
- Issue 21: presets operacionais de latencia (local vs qualidade) — incluindo `WHISPER_MODEL` como principal alavanca de latencia STT.
- Issue 22: observabilidade de fallback de sample rate no Windows — WARNING/INFO adicionados ao `MicrophoneSource`. **[Done]**
- Issue 30: bug latente em `STTService.transcribe()` direto — falta parametro `sample_rate` para audio capturado fora de 16 kHz.
- Issue 23: classificador formal de erros tecnicos vs logicos no fallback de LLM (`LLM_ERR_UNAVAILABLE` / `LLM_ERR_LOGICAL`).
- Issue 24: backoff exponencial com jitter nos retries de LLM (`LLM_RETRY_BACKOFF_BASE_S`, `LLM_RETRY_BACKOFF_MAX_S`).
- Issue 25: limite de contexto por tokens alem de numero de mensagens (`MAX_CONTEXT_TOKENS`).
- Issue 26: singleton de carregamento do modelo Whisper para evitar reload por sessao.
- Issue 27: streaming de tokens do LLM para a UI com metrica de `time_to_first_token`.
- Issue 28: dois limiares de confianca STT (`STT_CONFIDENCE_BLOCK`, `STT_CONFIDENCE_WARN`) para ambientes ruidosos.
- Issue 29: agrupamento de trechos incertos na revisao final do perfil medical (`MAX_REVIEW_ITEMS`).

### Captura e transcricao em tempo real
- `AudioInput` recebeu `MicrophoneSource` com fallback de sample rate para melhorar compatibilidade no Windows.
- Foi adicionado um pipeline de streaming em `live_caption` com 3 threads:
  1. produtor de audio,
  2. segmentador VAD,
  3. worker de transcricao.
- O STT aceita bytes PCM e ndarray float32 (resample para 16 kHz no adaptador).

### Voice CLI operacional
- Existe entrypoint dedicado (`voice_cli`) para sessao de voz local.
- O fluxo STT final alimenta uma fila de requisicoes de LLM.
- Um worker dedicado processa o LLM em paralelo para nao bloquear captura/transcricao.

### Higiene de repositorio aplicada
- O artefato acidental de worktree em `.claude/worktrees/...` deixou de ser rastreado.
- O caminho foi adicionado ao `.gitignore` para evitar recorrencia.

---

## Perfil Medical MVP + SLOs Operacionais

### Perfil por configuracao
- Definir perfil do assistente por variavel de ambiente.
- Chave sugerida: `ASSISTANT_PROFILE=medical|general|sales`.
- No MVP, sem troca de perfil em runtime pela UI.

### Regras versionadas por perfil
- Manter regras em arquivos separados e versionados (exemplo: `profiles/medical.yaml`).
- Cada perfil possui contrato de saida obrigatorio proprio.

### Contrato obrigatorio de saida (perfil medical)
- Resumo do caso.
- Perguntas faltantes.
- Sinais de alerta.
- Hipoteses possiveis.
- Exames sugeridos para discutir com profissional.
- Orientacao final de seguranca.

### Coleta de anamnese (perfil medical)
- Campos obrigatorios iniciais: 8.
- Lista: queixa principal, inicio, duracao, intensidade, sintomas associados, medicacoes em uso, alergias, sinais de alerta atuais.
- Gerar prontuario parcial com aviso explicito de pendencias quando faltarem campos.
- Limitar a coleta a no maximo 3 perguntas por ciclo antes de emitir resumo parcial.

### Politica STT no MVP (seguranca e robustez)
- Priorizar precisao sobre latencia de modelo.
- Meta de latencia STT: p95 <= 2,5s por trecho.
- Limiar de confianca STT para bloqueio: 0,80.
- Abaixo do limiar: bloquear envio ao LLM e pedir repeticao.
- Maximo de 2 repeticoes por interacao.
- Apos 2 falhas: fallback para entrada manual por texto.
- Fallback manual com confirmacao curta obrigatoria antes de seguir.

### Politica de falhas (fail-soft)
- Realizar ate 2 tentativas com timeout curto por etapa.
- Em falha de STT: solicitar repeticao ao usuario.
- Em falha de LLM: exibir mensagem util e manter sessao ativa.
- Registrar erro por modulo para acompanhamento.

### SLO inicial objetivo (fase atual)
- STT: WER <= 10% (equivalente a cerca de 90% de acerto), medido em 50 frases fixas.
- Distribuicao recomendada das 50 frases: 30 em silencio e 20 com ruido leve.
- Latencia ponta a ponta: p95 < 4s (fim da fala ate resposta textual completa).
- Estabilidade: 30 minutos de uso continuo sem travar e taxa de erro tecnico < 2% por interacao.

### Sinais de alerta no Orchestrator
- Checagem obrigatoria de termos criticos antes do prontuario final.
- Quando houver sinal de alerta, incluir secao de alerta e orientacao explicita para atendimento presencial/urgencia.

---

## Como Trocar de Perfil

- A selecao de perfil no MVP e feita por variavel de ambiente `ASSISTANT_PROFILE`.
- Exemplos de perfil disponivel neste projeto:
  - `medical`
  - `clinical_pharmacist_interviewer`
  - `clinical_nurse_interviewer`
  - `general_interviewer`
- Para entrevistas, configure tambem `SENIORITY_LEVEL=junior|pleno|senior`.
- Referencias operacionais:
  - Guia de perfis: `profiles/README.md`
  - Exemplo de ambiente: `.env.example`

---

## Aditivo Fase 2 e Fase 3 — Componente Assistente + SaaS B2B

> Decisão arquitetural registrada em `docs/adr/0001-fase-2-telemedicina-saas.md`. Vocabulário canônico em `docs/glossario.md`. Esta seção é normativa: PRs que contradigam decisões fixadas aqui são bloqueados em revisão. Substitui o aditivo anterior (3 fases monolíticas) por divisão em duas fases macro com base no modelo de consumo: **Fase 2 — assistente como componente do app de telemedicina do produtor; Fase 3 — assistente como SaaS B2B vendido a Tenants externos**.

### Premissa

O MVP entrega o núcleo de domínio (Anamnese, sinais de alerta, perfis de domínio, Ciclo de perguntas, atribuição de Participantes) com contratos abstratos (`STTService`, `LLMService`, `ConversationState`, `VoiceOrchestrator`, `MedicalProfileEngine`, `InterviewFlow`, `SessionReview`, `ParticipantAttribution`).

Na Fase 2 esse núcleo é empacotado como **componente integrável** (API HTTP + WebSocket) consumido por um app de telemedicina (que é o Controlador LGPD; o assistente é Operador). Na Fase 3 o componente vira **plataforma SaaS B2B multi-tenant** vendida a outros apps/clínicas. **Os contratos de domínio núcleo permanecem estáveis em ambas as fases. Novos contratos paralelos podem ser introduzidos sem quebra dos existentes** — em particular, `STTService.stream(...)` é método adicional, não substituto; `STTService.transcribe(...)` legacy permanece para Voice CLI e retranscrição offline.

**Quebra admitida**: `MicrophoneSource` (push síncrono via callbacks) → `AudioGateway` (consumo assíncrono via WebSocket de longa duração) **não é preservação semântica de contrato**. Introduzimos abstração `AudioSource` async-iterable (`async for chunk in source`) que ambos passam a implementar; orquestrador troca `process_segment` síncrono por `process_stream` async (versão major).

### Modelo de consumo e fronteira Controlador/Operador

O assistente é **Operador LGPD** processando áudio/texto sob instrução do Controlador (app de telemedicina). Esta fronteira determina o que faz e o que não faz parte do escopo:

| Responsabilidade | Onde fica |
|---|---|
| Convocar/agendar Consulta, identificar Paciente | App de telemedicina (Controlador) |
| Capturar Consentimento granular do Paciente | App de telemedicina |
| KYC do Paciente (gov.br ou doc+selfie) | App de telemedicina |
| Confirmar identidade do Médico (CRM) | App de telemedicina |
| **Sistema de prontuário eletrônico oficial** (CFM 1.821) | App de telemedicina |
| **Assinatura ICP-Brasil** de prontuário/prescrição/atestado | App de telemedicina |
| Storage de prontuário oficial 20 anos (CFM 1.821 Art. 8º) | App de telemedicina |
| Atender DSR LGPD do Paciente | App de telemedicina |
| DPO público de telemedicina | App de telemedicina |
| | |
| Capturar áudio + STT + LLM | **Assistente (Operador)** |
| Gerar conteúdo estruturado (Anamnese, draft, alertas) | **Assistente** |
| Devolver via API HTTP/WS para o app | **Assistente** |
| AuditLog do componente (chamadas, sessões, exportações) | **Assistente** |
| Encryption-at-rest do áudio gravado pelo componente | **Assistente** |
| DPA com providers (Deepgram/OpenAI/Anthropic) | **Assistente** |
| DPIA do tratamento operado pelo componente | **Assistente** |
| Retenção curta (áudio bruto 30-90d, transcrição 6-12m) | **Assistente** |
| DPO interno (compartilhado com a empresa) | **Assistente** |

CFM 1.821 e CFM 2.314 **não se aplicam ao assistente** — ele não é sistema de prontuário nem operador de telemedicina; é gerador de conteúdo. As normas se aplicam ao app de telemedicina.

Quando o assistente virar SaaS B2B (Fase 3), Tenants externos podem ter perfis distintos: (a) Tenants que **têm app de telemedicina próprio** seguem o mesmo modelo; (b) Tenants que **não têm** podem contratar o **add-on "Suite Compliance"** (Fase 3.1) que oferece SignatureService ICP-Brasil cloud, IdentityService, ConsentService granular, DSRPortal whitelabel.

### Visão geral em 5 sub-fases

| Aspecto | MVP (Fase 1) | **Fase 2.0** componente MVP | **Fase 2.1** componente hardening | **Fase 3.0** SaaS multi-tenant | **Fase 3.1** add-on Compliance | **Fase 3.2** enterprise |
|---|---|---|---|---|---|---|
| Modelo | desktop single-user | API+WS para um app | idem 2.0 + produção | API+WS multi-tenant | + add-on Suite Compliance | + VPC dedicada |
| Transporte áudio | sounddevice | WebSocket+Opus 20 ms | idem | idem | idem | idem |
| Tenants | 1 implícito | 1 (default UUID) | 1 com `tenant_id="default"` em métricas/schema | N (RLS ligada) | N | N + isolated |
| Auth | nenhuma | API key + JWT do app | idem | IdP próprio (Keycloak); SSO federado | idem | idem |
| STT | Whisper local | Deepgram Nova-2 streaming pt-BR + fallback batch | idem + retenção curta | + quota por Tenant | idem | idem |
| LLM | API/Ollama | GPT-4o + Sonnet 4.5 fallback; cache só do system prompt | idem | + rate limit por Tenant | idem | idem + log auditável |
| Estado | RAM | Redis Streams append-only (`tenant:default:session:{id}:events`) | idem + Postgres backup | + RLS ligada operacionalmente | idem | + DB-per-tenant SKU |
| AuditLog | logs locais | mínimo Postgres com hash chain | idem + retenção 5 anos | + label tenant | + WORM em S3 Object Lock COMPLIANCE | idem |
| Compliance pública (ICP, KYC, DSR público) | n/a | **fora do escopo (no app)** | idem | idem | **add-on opcional** | idem + customização |
| Encryption | n/a | master key única | idem | + por Tenant (envelope) | idem | + KMS dedicado por SKU |
| Voice CLI | produto principal | dev/teste | idem | idem | idem | idem |

---

### Fase 2.0 — MVP do componente

**Objetivo.** Servir o assistente como componente HTTP+WebSocket consumido pelo seu app de telemedicina. Single-tenant (UUID default), mas com schema multi-tenant ready (preparado para Fase 3.0 sem migration cara).

**Novos módulos (servidor)**

- `AudioGateway`: WebSocket bidirecional do navegador. Aceita Opus 20 ms 16 kHz mono (decodificado server-side para PCM float32). Bounded queue 1s por Sessão; descarta frame antigo em overflow; emite `STT_OVERFLOW`. Diarização **por roteamento** (uma stream por Participante; médico e paciente em navegadores separados).
- `SessionManager`: ciclo de vida de Sessão. **Idempotency-key obrigatório** no `POST /v1/sessions` (`Redis SET NX`). Hard cap **4h**; warning a 30 min de silêncio; auto-finalize após 6h sem evento. Reconexão recupera Sessão por `session_id` (claims do JWT que o app entrega).
- `AuthService` (componente): valida **API key** do app + **JWT do app** carregando claims (médico, paciente, role). **Sem IdP próprio** (vem do seu app). API key com escopo (`sessions:write`, `sessions:read`).
- `SttPool`: workers async sobre `asyncio.Queue` por processo (descartável). Implementação primária `DeepgramStreamingSTT` (`STTService.stream`). Fallback batch `OpenAIWhisperApiSTT` para retranscrição offline a partir de áudio em S3.
- `LlmPool`: workers async com pool persistente a OpenAI/Anthropic. Prompt caching **somente** sobre o bloco estático do system prompt do Perfil; flag `allow_provider_prompt_cache: bool` por Profile, default `false` em `medical`.
- `MetricsExporter`: Prometheus com label `tenant_id="default"` desde dia um (preserva séries para 3.0).
- `AuditLogger` (mínimo): append-only Postgres com hash chain. Retenção 5 anos. Eventos: criação/encerramento de Sessão, exportação de transcrição, exportação de draft, chamada de API.
- `EventStream`: wrapper sobre `XADD session:{tenant}:{session_id}:events` em Redis Streams como **source of truth**. `ConversationState` deixa de ser hidratado/escrito read-modify-write e passa a ser **derivado por reduce**.
- `Observability`: OpenTelemetry com W3C trace-context propagado pelo WS frame; structured logs JSON via structlog com `correlation_id`, `session_id`, `tenant_id`; exporter para Loki via Vector. PII pseudoanonimizada (hash de IDs).

**Substituições e mudanças**

- `MicrophoneSource` (desktop, Voice CLI) e `AudioGateway` (web) ambos implementam `AudioSource` async-iterable. Orquestrador troca `process_segment(segment)` síncrono por `process_stream(source)` async — **quebra de contrato declarada**, versão major.
- `STTService` ganha **método adicional** `stream(audio_chunks_iter, on_partial, on_final, *, sample_rate=16000) -> AsyncIterator[STTEvent]`.
- `ConversationState`: deque local segue como projeção; fonte da verdade é Redis Stream. Idempotência via `event_id` UUID.
- `ProfileResolver.resolve(ctx: SessionContext)` onde `SessionContext{tenant_id, session_id, session_kind, seniority, actor_role}`. `tenant_id="00000000-...-default"` na 2.0; `tenant_id` real na 3.0 sem mudança de assinatura.
- Toda tabela criada na 2.0 já com `tenant_id UUID NOT NULL DEFAULT '00000000-...-default'`. **RLS escrita mas desligada** (`RLS_ENABLED=false`); ligada na 3.0 sem migration.

**Novos contratos**

- `AudioSource` async-iterable: `async def __aiter__(self) -> AsyncIterator[AudioChunk]`. `AudioChunk = (bytes, sample_rate, capture_ts, seq)`.
- `STTService.stream(...) -> AsyncIterator[STTEvent]` onde `STTEvent ∈ {partial(text, conf), final(text, conf, ts), endpoint}`.
- WebSocket framing tipado bidirecional: inbound `{"type":"audio_chunk"|"control",...}`; outbound `{"type":"token"|"status"|"final"|"error"|"trace",...}`. Schema versionado em `schemas/ws/v1/`.
- API REST versionada `/v1/sessions`, `/v1/sessions/{id}`, `/v1/sessions/{id}/draft`, etc. OpenAPI gerada.
- Eventos Redis Stream `session:{tenant_id}:{session_id}:events`: `session_started`, `participant_message`, `assistant_token`, `assistant_final`, `red_flag`, `stt_low_confidence`, `stt_overflow`, `session_ended`. Schema JSON em `schemas/events/v1/`.
- Stable error code namespace formal `<DOMAIN>_<COMPONENT>_<REASON>` (ex.: `STT_DEEPGRAM_TIMEOUT`, `LLM_OPENAI_RATE_LIMIT`, `WS_GATEWAY_OVERFLOW`).

**SLOs (2.0)**

- TTFT p95 < 1,5s (Deepgram + GPT-4o streaming).
- WS reconnect < 3s sem perda de Sessão.
- 10 simultâneas baseline; auto-scale para 100 sem ação humana.
- Trilha de auditoria sem gaps em 100% das Sessões.

---

### Fase 2.1 — Componente em produção (hardening + Operador LGPD)

**Objetivo.** Mesmo escopo da 2.0 endurecido para produção como Operador LGPD.

**Adicionado**

- Multi-AZ Postgres + PITR 35 dias.
- Snapshot diário cross-region.
- RTO 1h, RPO 5min.
- Testes de carga semanais k6 (50 sessões WS de 30 min); SLO falha bloqueia release.
- Runbooks por SLO em `docs/runbooks/`.
- PagerDuty/Opsgenie configurado.
- AWS Secrets Manager com rotação automática.
- CI/CD pleno: GitHub Actions → ECR → ECS Fargate; gate manual prod; Trivy bloqueante.
- Três ambientes (dev efêmero por PR, staging seedado, prod).
- Suporte AWS Business.
- DPA assinado com Deepgram/OpenAI/Anthropic — **bloqueador hard antes do primeiro paciente real**.
- DPIA do componente assinada antes do primeiro paciente real.
- DPO indicado (compartilhado com a empresa); base legal LGPD = tutela da saúde + execução de contrato com Controlador.
- AuditLog com retenção 5 anos confirmada.

**SLOs (2.1)**

- 99,5% uptime mensal do componente.
- DR: restore em < 1h em ambiente paralelo.
- DPA com providers ativo em 100% dos casos antes de produção real.

---

### Fase 3.0 — SaaS B2B Multi-Tenant (futuro)

**Objetivo.** Vender o assistente como serviço a múltiplos Tenants (apps de telemedicina, clínicas, redes).

**Adicionado**

- IdP próprio: **Keycloak self-hosted** (recomendado) ou Auth0. SSO federado por clínica entra como story neste momento.
- Postgres RLS **ligada** via `current_setting('app.tenant_id', true)` + middleware `SET LOCAL` em toda transação. Lint de PR no CI bloqueia tabela sem `tenant_id NOT NULL`.
- `TenantResolver` middleware extraindo `tenant_id` do JWT.
- `RateLimiter` / Quota por Tenant: limita tokens/min de LLM, minutos/dia de STT, sessões abertas. Ramping na criação de Sessão (5/min/Tenant).
- `ProfileCatalog` por Tenant: cada Tenant escolhe entre Perfis publicados; customização YAML via API admin com audit.
- `BillingExporter`: integrado a Stripe Billing usage-based; webhook `invoice.failed` aciona kill switch com 7 dias grace.
- `KillSwitch` granular: tabela `tenants(status)` + flag Redis `KILL_SWITCH:{tenant_id}` para ação síncrona.
- Status page público (BetterStack ou Statuspage).
- Feature flags via OpenFeature + Unleash; flag-set por Tenant.
- Métricas e logs ganham label `tenant_id` real (não mais `"default"`).
- Filas STT/LLM com fairness por Tenant (token bucket).
- Redis Cluster com `WAIT 1 100` em escritas críticas.
- Token JWT do Paciente RS256 com TTL/denylist/nonce (quando assistente vira IdP de algum fluxo).

**SLOs (3.0)**

- Isolamento: 0 leakage cross-tenant em testes de penetração no CI semanal.
- Fairness: latência p95 do Tenant N não degrada > 20% quando Tenant M satura.
- DSR: 100% processados em < 15 dias (responsabilidade do Tenant; assistente colabora fornecendo dados).

---

### Fase 3.1 — Add-on "Suite Compliance" (opcional)

**Objetivo.** Vendido como add-on contratado a Tenants que **não têm app de telemedicina próprio** e precisam que o assistente cubra ICP-Brasil, KYC, ConsentService público, DSRPortal.

**Adicionado (apenas para Tenants que ativam)**

- `ConsentService` granular versionado (cláusulas opt-in independentes: tratamento clínico, gravação, retenção, providers, ambiente privado CFM 2.314).
- `IdentityService`: KYC do Paciente via gov.br ou doc+selfie; hash do resultado por Sessão.
- `SignatureService`: assinatura **por evento clínico** com certificado ICP-Brasil em nuvem (Soluti/Bry/Vidaas) por API.
- `DSRPortal`: portal whitelabel para titular acionar direitos LGPD; SLA 15 dias.
- `RetentionPolicy` dual: clínico 20 anos / não-clínico segue Tenant; recusa padronizada de eliminação.
- `EncryptionAtRest` envelope per-Tenant: master vira KEK; DEK rotacionada anualmente.
- AuditLog em S3 Object Lock COMPLIANCE como source-of-truth; Postgres-as-cache.

**Cobrança**: add-on cobrado separado por Tenant que ativa (preço sugerido R$ 1.500-2.500/mês). Margem ~80% sobre custos de provider de assinatura/KYC.

**SLOs (3.1)**

- 100% das Sessões em Tenants com add-on ativo: Consentimento granular registrado.
- 100% dos Prontuários finais em add-on: assinatura ICP-Brasil válida com cadeia por evento.
- KYC Paciente com cache; revalidação anual.
- Verificação periódica de hash chain S3 sem gaps.

---

### Fase 3.2 — SKU Enterprise

**Objetivo.** Atender Tenants enterprise (hospital/rede) que exigem isolamento físico ou compliance reforçada.

**Adicionado**

- VPC dedicada por Tenant.
- DB-per-tenant Postgres com migration tooling.
- KMS dedicado por SKU.
- Reabertura formal de A3 (hospital-on-prem) como roadmap separado se ≥ 3 Tenants enterprise pedirem.

**Cobrança**: ≥ 3× preço shared.

---

### Planos do componente assistente

Diferenciação **clínica/operacional**, não compliance (compliance é responsabilidade do Controlador/app).

| Eixo | Básico | Médio | Premium |
|---|---|---|---|
| STT | batch pós-Sessão | streaming | streaming + diarização |
| LLM principal | GPT-4o-mini | mix mini + GPT-4o | GPT-4o predominante |
| Red flag em tempo real | ❌ (assíncrono) | ✅ | ✅ |
| Assistente proativo | ❌ | parcial (template + LLM) | pleno |
| Cap Sessão | 60 min | 60 min | 4 h |
| Custo unitário/Sessão | US$ 0,11 | US$ 0,28 | US$ 0,55 |
| Mensal × 176 Sessões | US$ 19 | US$ 49 | US$ 97 |

**Riscos clínicos a comunicar em contrato**:
- Plano Básico **não detecta red flag em tempo real**. Inadequado para urgência/emergência. Termo de uso obrigatório.
- Em todos os planos, o Prontuário gerado é **conteúdo estruturado** entregue ao app de telemedicina; validade jurídica vem da assinatura do app (ICP-Brasil), não do assistente.

Implementação: `Profile.plan_features` schema com toggles que o orquestrador consulta antes de cada chamada (STT mode, LLM modelo, red flag em tempo real, assistente proativo, cap, etc.).

---

### Dimensionamento e escala em camadas

Política para "escalar fácil" sem reescrever:

| Camada | Capacidade | Como cresce | Ação humana |
|---|---|---|---|
| **Baseline** | 10 sessões simultâneas | configuração inicial fixa | nenhuma |
| **Auto-scale** | até 100 simultâneas | políticas de auto-scaling configuradas | nenhuma; alertas avisam |
| **Escala humana** | > 100 simultâneas | upgrade contratual de provider, instância maior, Cluster Redis | sim |

**Tetos por componente** (onde a escala bate em ação humana):

| Componente | Teto sem ação humana | O que destrava |
|---|---|---|
| ECS Fargate | quota AWS de vCPUs | request à AWS (horas) |
| RDS Postgres | conexões da instância (~200 em t4g.medium) | upgrade de instância — minutos de downtime |
| Redis single-node | ~10k ops/s | migrar para Cluster — janela de manutenção |
| **Deepgram** | concurrent connections do plano | **contato comercial — pode levar dias** |
| **OpenAI** | tokens-per-minute do Tier | sobe sozinho com histórico, mas semanas |
| ALB | limites altos por default | request à AWS |

**Configuração de runtime exposta como variáveis** (nada hardcoded):
`POSTGRES_POOL_MAX`, `REDIS_POOL_MAX`, `STT_CONCURRENT_MAX`, `LLM_CONCURRENT_MAX`, `WS_CONCURRENT_MAX`, `SESSION_MAX_OPEN_PER_INSTANCE`.

**Auto-scaling por métrica de domínio**: gatilho não é CPU, é **número de Sessões abertas por instância** (`sessions_active_per_instance`).

**Bounded queue por Sessão** (1s áudio): uma Sessão ruim não derruba as outras.

**Graceful shutdown**: scale-in espera Sessões abertas terminarem (drain até 30 min).

**Runbook**: `docs/runbooks/scale_out.md` (entregável da Fase 2.1) com checklist de "quando 80% do teto X, ação Y".

---

### Contratos da Fase 2 e Fase 3

**(a) Contratos núcleo do MVP — estáveis em todas as fases** (mudança exige nova ADR):

- `LLMService.stream(state, on_token) -> LLMResponse`
- `ConversationState.{add, messages, to_llm_messages, set_system, clear}`
- `MedicalProfileEngine.build_record(...)`
- `InterviewFlow.{next_question, accept_answer, finalize}`
- `ParticipantAttribution`, `SessionReview`
- Profile YAML schema (`profiles/schema/profile.schema.json`)
- `TurnResult` dataclass
- Stable error codes existentes (renomeação proibida; novos códigos adicionados)

**(b) Novos contratos paralelos** (introduzidos na 2.0, estáveis nas 2.1/3.x):

- `AudioSource` async-iterable.
- `STTService.stream(...)` (batch `transcribe()` legacy permanece).
- `SessionContext{tenant_id, session_id, session_kind, seniority, actor_role}`.
- WebSocket framing tipado bidirecional (`schemas/ws/v1/`).
- API HTTP versionada (`/v1/sessions`, etc.) com OpenAPI gerada.

**(c) Eventos e schemas Redis Streams**:

- Stream `session:{tenant_id}:{session_id}:events` com hash tag `{tenant_id}:{session_id}` (co-localização de Sessão num único shard quando virar Cluster).
- Stream `billing:usage` (Fase 3.0 append-only de minutagem STT e tokens LLM).
- Stream `audit:events` (mirror para fan-out a S3 WORM em Tenants com add-on Compliance).

---

### Decisões fixadas

- **STT primário**: Deepgram Nova-2 streaming pt-BR. Fallback batch: OpenAI Whisper API offline.
- **LLM primário**: OpenAI GPT-4o. Fallback técnico: Anthropic Sonnet 4.5. Gemini removido.
- **Codec WS**: Opus 20 ms, 16 kHz, mono. WebRTC adiado (TTS bidirecional).
- **VAD**: client-side (browser) com fallback server-side.
- **Auth Fase 2**: API key + JWT do app de telemedicina. **Sem IdP próprio**.
- **Auth Fase 3**: Keycloak self-hosted; SSO federado por clínica.
- **Cloud**: AWS `sa-east-1`. ECS Fargate.
- **Postgres**: shared+RLS na 3.0 default; DB-per-tenant SKU enterprise na 3.2.
- **Redis**: Cluster com hash tag `{tenant_id}:{session_id}` (single-node físico aceito até 3.0).
- **Fila**: `asyncio.Queue` por processo para áudio em tempo real; Redis Streams para pipeline pós-Sessão.
- **Migrations**: Alembic versionado + reversível; CI bloqueia migration sem `downgrade()`; expand-contract; CONCURRENTLY para índices.
- **Prompt caching**: somente system prompt estático; **proibido cachear contexto de Sessão em `medical`**.
- **Encryption**: master key única na 2.0; envelope per-Tenant na 3.1 (master vira KEK).
- **AuditLog**: mínimo Postgres com hash chain desde 2.0 (5 anos); WORM S3 Object Lock COMPLIANCE no add-on Compliance da 3.1.
- **TLS**: 1.2+ obrigatório (higiene, não compliance).
- **Custo unitário alvo**: Sessão 30 min Premium ≈ US$ 0,55. Revisão mensal; alerta > 1,5×.
- **Hard caps**: Sessão 4h; warning a 30 min de silêncio; auto-finalize 6h.
- **Escala**: baseline 10 simultâneas, auto-scale até 100, depois ação humana.

---

### Compliance LGPD/CFM por papel

**Assistente como Operador (Fase 2/3) — obrigações próprias**:
- DPA com Deepgram/OpenAI/Anthropic (bloqueador hard antes de paciente real).
- Encryption-at-rest do áudio gravado pelo componente.
- AuditLog com hash chain 5 anos.
- DPIA do tratamento operado.
- DPO indicado (LGPD Art. 41) — pode ser compartilhado com a empresa.
- Base legal: tutela da saúde (Art. 11 §2 II "a") + execução de contrato com Controlador.
- Notificação de breach ao Controlador (Art. 39).
- Sub-operadores declarados (Deepgram, OpenAI são sub-operadores do assistente).
- Retenção curta (áudio bruto 30-90d, transcrição 6-12m); descartar/devolver ao Controlador depois.

**App de telemedicina como Controlador — obrigações que o assistente NÃO cumpre**:
- Captura de Consentimento granular (CFM 2.314 inclui declaração de "ambiente privado e adequado").
- KYC do Paciente (CFM 2.314 Art. 4º §2).
- Assinatura ICP-Brasil **por evento** (CFM 1.821 Art. 7º) de prontuário/prescrição/atestado.
- Storage de prontuário 20 anos (CFM 1.821 Art. 8º).
- DPO público de telemedicina com email do canal (LGPD Art. 41 §1º).
- Atender DSR (LGPD Art. 18) com SLA 15 dias.
- DPIA da operação completa de telemedicina.

---

### Riscos e failure modes

| Risco | Mitigação | Fase |
|---|---|---|
| Provider STT cai | Timeout 5s; áudio em S3 para retranscrição offline; fallback de digitação manual | 2.0 |
| Provider LLM cai | Timeout inter-token 10s; fallback Sonnet 4.5; resposta `partial`; botão retry | 2.0 |
| Rede do paciente cai | Frames com `seq`; cliente bufferiza 5s; reenvia gap; banner após 30s | 2.0 |
| JWT expira no meio da Sessão | Refresh silencioso 30 min; falha vira read-only, nunca disconnect brusco | 2.0 |
| Clock skew | TTL relativo (`EXPIRE`); chrony obrigatório; alerta > 200ms | 2.0 |
| Thundering herd | Rate limiter 5/min/Tenant ramping; pool Redis pré-quente; idempotency-key | 2.0 |
| Buffer áudio overflow | Bounded queue 1s; descarta antigo; emite `STT_OVERFLOW` | 2.0 |
| Race em `SessionManager.create` | Idempotency-key obrigatório; Redis `SET NX` | 2.0 |
| WER pior que esperado pt-BR clínico | Bateria 50 frases pré-go-live; fallback automático de digitação | 2.0 |
| Migration longa em prod | CONCURRENTLY; coluna nova nullable + backfill batch; nunca DROP | 2.0 |
| Redis split-brain | Sentinel single-master 2.0; Cluster com `WAIT` na 3.0 | 2.0/3.0 |
| Vazamento cross-tenant | RLS via `current_setting`; middleware `SET LOCAL`; pen test no CI | 3.0 |
| Tenant satura quota | Fairness em fila; quota Stripe + alerta | 3.0 |
| Backup não restaura | Restore mensal em ambiente paralelo; runbook validado | 2.1 |
| Lock-in em provider | Adapters atrás de contrato; gatilho 200 mil min/mês STT | contínuo |
| Custo unitário explode | `cost_per_session_brl`; alerta > 1,5× baseline | contínuo |

---

### Trade-offs documentados

- **Managed STT vs Whisper-GPU self-hosted**: managed valida produto sem operar GPU. **Gatilho: 200 mil min/mês STT por região**. Em volume baixo (10 simultâneas), managed é objetivamente mais barato (US$ 22/mês vs US$ 220-850/mês de GPU).
- **Prompt caching**: ganho de latência e custo só no system prompt estático. Cache de Sessão **proibido em `medical`** por compliance.
- **Multi-tenant compartilhado vs VPC**: shared default na 3.0 (RLS + isolation tests). VPC dedicada SKU enterprise na 3.2 ao preço ≥ 3× shared.
- **LLM primário GPT-4o**: melhor pt-BR clínico observado. Sonnet 4.5 fallback. Gemini removido por inconsistência format compliance.
- **WS reconnect**: backoff 200ms → 3s; após 5 tentativas, UI mostra erro acionável.
- **BillingExporter (Fase 3.0)**: append em tempo real Redis Streams; agregação batch horária; mensal. Cobrança nunca trava Sessão.
- **Codec Opus vs PCM raw**: Opus reduz banda 10× com WER equivalente. WebRTC adiado para Fase 3.x (TTS bidirecional).
- **ICP-Brasil/KYC fora do escopo do componente**: responsabilidade do Controlador (app de telemedicina). No SaaS B2B (3.1), oferecido como add-on opcional.

---

### Próximos passos

1. Bateria de WER em 50 frases clínicas pt-BR comparando Deepgram Nova-2 vs OpenAI Whisper API vs Google Cloud STT (deadline 2026-06-01).
2. Decidir IdP da Fase 3 — Keycloak vs Auth0 (deadline pré-Fase 3.0; não bloqueia 2.0).
3. PRD da Fase 2 (`PRD-fase2.md`) e Fase 3 (`PRD-fase3.md`) consolidados (deadline 2026-06-15).
4. Backlog técnico: `ISSUES_FASE2.md` com 2.0 detalhada e 2.1 em granularidade épica; Fase 3 fica como roadmap de futuro.
5. DPA preliminar com Deepgram, OpenAI, Anthropic — começar negociação imediatamente (lead time ≥ 60 dias).
6. Template de DPIA do componente em `docs/compliance/dpia-componente.md` rascunhado.
7. Template de DPA interno entre o app de telemedicina e o componente assistente em `docs/compliance/dpa-internal.md` (mesmo sendo a mesma empresa, formaliza relação Controlador/Operador — reusa template para Fase 3).


## Aditivo — Estado implementado da Fase 2.0 (revisão de 2026-10-27)

Fluxo real do servidor após a revisão (tudo sem provider gerenciado; ver ADRs 0002–0007):

```
app (browser)                      assistente_de_voz (FastAPI)
  POST /v1/sessions ───────────▶  sessions router ─ SessionsRepository ─▶ Postgres
                                   │ ws_token {session_id, tenant_id, expires_at} ─▶ Redis
                                   │ AuditLog session.created
  WS /v1/sessions/{id}/audio ───▶  audio router: token ⇔ sessão/tenant, sessão aberta?
   frames {seq, data(PCM16)}       │ AudioGateway (dedupe seq/speaker, late-arrival, snapshot)
                                   │   └─ consumer ─▶ SessionTranscriber (janela/silêncio)
                                   │        └─ STTStreamService (circuit breaker)
                                   │             ├─ fake | local_whisper (CPU, thread)
                                   │             └─ Deepgram/Whisper API (Issue #19)
   ◀── transcript / red_flag ──    │ outbox (um sender task) ◀─ status/drain/token_expiring_in
                                   │ XADD type=frame|transcript ─▶ Redis Stream {tenant:session}
  POST /close ───────────────────▶ session_lifecycle.close_session_tx (também usado pelo reaper)
                                   │ revoke token + pub/sub, EXPIRE stream, draft pending,
                                   │ AuditLog session.closed → BackgroundTask MaterializationWorker
  GET /draft ────────────────────▶ 202 pending | 200 ready {transcript, red_flags, fields{}} | 500 failed
```

Módulos novos nesta revisão: `server/session_lifecycle.py`, `server/transcriber.py`,
`server/reaper.py`; `server/event_stream.RedisEventStream`; `server/metrics.render_metrics`
(`/metrics`). Migration `0006` (AuditLog `seq` + trigger append-only + `draft_feedback.
correction_ratio FLOAT`).

Invariantes acrescentados:

- Toda dependência de banco em router é `DbSession` (`scope="function"`): o commit precede a
  resposta e as BackgroundTasks.
- Toda escrita no WebSocket passa pela outbox da conexão; nenhuma task escreve no socket
  diretamente (exceto `close` por revogação).
- `close_session_tx` é a única transição `open → closed` (router e reaper).
- O draft nunca copia entradas de frame do stream; só `transcript` entra no conteúdo.
