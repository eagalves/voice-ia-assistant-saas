# PRD — Fase 2: Componente Assistente Integrado ao App de Telemedicina

> **Status**: Proposto (rev. 2 — fronteira Controlador/Operador corrigida).
> **Decisão arquitetural**: `docs/adr/0001-fase-2-telemedicina-saas.md`.
> **Arquitetura detalhada**: `arquitetura-assistente-voz-mvp.md`, seção "Aditivo Fase 2 e Fase 3".
> **Custos**: `custos.md`.
> **Vocabulário canônico**: `docs/glossario.md`.
> **Predecessor**: `PRD.md` (PRD do MVP, Fase 1, fechado).
> **Sucessor (futuro)**: `PRD-fase3.md` (SaaS B2B multi-tenant).
> **Deadline de decisão final**: 2026-06-15.

---

## Problem Statement

O MVP entregou um assistente de voz desktop, single-user, com domínio (Anamnese, sinais de alerta, perfis clínicos, Ciclo de perguntas, atribuição de Participantes) validado em uso local. O produtor está construindo um **app de telemedicina** próprio, e quer integrar este assistente como **componente** dentro desse app — não como SaaS independente.

O caminho atual não suporta esse uso por construção: captura é via microfone local (`sounddevice`), Sessão vive em RAM in-process, perfil é configurado por env no boot, não há API HTTP nem WebSocket, não há autenticação de chamadas vindas de outro sistema. Para virar componente integrável, o assistente precisa: aceitar conexão WebSocket de áudio do navegador (ou do servidor do app), expor API REST para criar/encerrar Sessão, autenticar via API key + JWT do app de telemedicina, gerar conteúdo estruturado (transcrição, draft de Prontuário, alertas) e devolver via API/WS.

**Fronteira clara**: o assistente é **Operador LGPD**, não sistema de prontuário. Ele processa áudio sob instrução do app de telemedicina (Controlador) e devolve conteúdo estruturado. CFM 1.821 e CFM 2.314 (prontuário eletrônico, telemedicina) **não se aplicam ao assistente** — aplicam-se ao app. ICP-Brasil, KYC do Paciente, ConsentService público, DSRPortal e DPO público de telemedicina **ficam com o app**, não com o assistente.

---

## Solution

Componente Python servidor com API HTTP + WebSocket bidirecional consumido pelo app de telemedicina. Entregue em duas sub-fases:

### Fase 2.0 — MVP do componente

Estrutura técnica completa (API+WS, STT/LLM gerenciados, EventStream, observabilidade, AuditLog mínimo) com **multi-tenant ready** (schema com `tenant_id NOT NULL DEFAULT default-uuid`, hash tag Redis, label `tenant_id="default"` em métricas, RLS escrita-e-desligada). Pronto para virar SaaS na Fase 3.0 sem migration cara, mas aqui ainda single-tenant operacional (UUID default).

### Fase 2.1 — Componente em produção

Hardening: Multi-AZ, DR (RTO 1h, RPO 5min), runbooks, k6 semanal, AWS Secrets Manager com rotação, CI/CD pleno, três ambientes, suporte AWS Business. Compliance como Operador LGPD: DPA assinado com Deepgram/OpenAI/Anthropic, DPIA do componente, DPO indicado (compartilhado com a empresa).

### Quebras de contrato declaradas

- Orquestrador `process_segment(segment)` síncrono → `process_stream(source: AudioSource)` async (versão major).
- `MicrophoneSource` push síncrono → `AudioSource` async-iterable que ambos `MicrophoneSource` (Voice CLI) e `AudioGateway` (web) implementam.
- `STTService.transcribe(...)` legacy permanece coexistindo com novo `STTService.stream(...)`.

### Fora do escopo (vai para Fase 3 ou para o app de telemedicina)

- Multi-tenant operacional, RLS ligada, ProfileCatalog por Tenant, RateLimiter por Tenant, Billing Stripe, Status page público, IdP próprio, JWT do Paciente com denylist — tudo Fase 3.0.
- ICP-Brasil cloud, IdentityService (KYC), ConsentService granular público, DSRPortal whitelabel — Fase 3.1 add-on opcional, ou responsabilidade do app de telemedicina.
- UI hospedada — não tem; o app de telemedicina é a UI.

---

## User Stories

### App de telemedicina (consumidor — produtor do produto)

1. Como dev do app, quero criar uma Sessão via `POST /v1/sessions` com `idempotency-key` e receber `{session_id, ws_url, ws_token}` para o navegador conectar. **`idempotency-key`**: TTL 24h, first-write-wins via `INSERT ... ON CONFLICT DO NOTHING`; POST duplicado com mesma chave retorna a sessão original.
2. Como dev do app, quero o navegador se conectar ao WebSocket do assistente com `ws_token` e enviar áudio Opus 20 ms 16 kHz mono.
3. Como dev do app, quero receber tokens de transcrição em streaming via WebSocket (`{"type":"token"}`) para mostrar legendas em tempo real.
4. Como dev do app, quero receber alertas de red flag em tempo real (`{"type":"red_flag"}`) para destacar na UI.
5. Como dev do app, quero buscar o draft de Prontuário gerado via `GET /v1/sessions/{id}/draft` para integrar no meu sistema de prontuário oficial.
6. Como dev do app, quero encerrar uma Sessão via `POST /v1/sessions/{id}/close` para liberar recursos do componente.
7. Como dev do app, quero que reconexão WebSocket recupere a Sessão pelos últimos 5s de áudio sem duplicar transcrição (frames com `seq`). **Garantia: at-least-once com dedupe por `event_id` no lado do servidor (Redis Streams). Handshake de reconexão**: após o cliente reconectar com `session_id`, o componente responde `{"type":"session_resumed","last_event_id":"<id>","last_seq":<N>}` indicando o último evento commitado no Stream — o cliente reenvia apenas frames com `seq > last_seq`. Sem ACK/NACK por frame; janela de reordenação de 5s.
8. Como dev do app, quero passar o Perfil ativo (`session_kind`) por chamada — o componente não decide perfil global no boot.

### Médico (usuário final, cliente do app de telemedicina)

9. Como Médico, quero falar normalmente durante a Consulta e ver a transcrição surgir com TTFT < 1,5s no app, sem distração técnica.
10. Como Médico, quero a transcrição diferenciar minhas falas das do Paciente (diarização por roteamento — duas streams WS separadas).
11. Como Médico, quero o assistente conduzir Anamnese guiada quando Perfil for `medical`, cobrindo os 8 campos obrigatórios sem que eu esqueça.
12. Como Médico, quero ser avisado em destaque quando um sinal de alerta (red flag) for detectado, para não passar batido em sintoma crítico.
13. Como Médico, quero o draft de Prontuário gerado ao fechar a Sessão para revisar e assinar **no meu app de telemedicina** (que cuida da assinatura ICP-Brasil).
14. Como Médico, quero a Sessão sobreviver a uma queda momentânea de rede do Paciente sem perder histórico.
15. Como Médico, quero continuar em modo `doctor_followup` após o Paciente sair, para refinar o Prontuário com o assistente sozinho.
16. Como Médico, quero digitar manualmente quando o STT falhar duas vezes — o app de telemedicina expõe este caminho consumindo a API do componente.
17. Como Médico, quero ver status do componente (ouvindo, transcrevendo, processando, fallback) refletido na UI do app via eventos WS (`{"type":"status"}`).

### Paciente (usuário do app de telemedicina)

18. Como Paciente, quero clicar no link do app de telemedicina e entrar na Consulta — quem cuida da minha autenticação é o app, não o componente.
19. Como Paciente, quero minha conexão de rede instável não derrubar a Consulta — o componente preserva Sessão pelo `session_id`.

### SRE (operador do componente)

20. Como SRE, quero traces W3C distribuídos do navegador ao LLM passando pelo `AudioGateway` e `SttPool`, para diagnosticar TTFT alto sem grep de log.
21. Como SRE, quero structured logs JSON com `correlation_id`, `session_id`, `tenant_id`, para filtrar incidente por Sessão em segundos.
22. Como SRE, quero teste de carga semanal k6 (50 sessões WS de 30 min) em ambiente dedicado, para detectar regressão antes de cliente.
23. Como SRE, quero rollback em < 5 min via blue-green, para não conviver com release ruim em produção.
24. Como SRE, quero runbooks por SLO violado em `docs/runbooks/`, para o on-call não improvisar.
25. Como SRE, quero PagerDuty configurado antes do primeiro paciente real (Fase 2.1).
26. Como SRE, quero PITR Postgres (7 dias na 2.0; 35 dias na 2.1) e snapshot diário cross-region, para responder a desastre dentro de RTO 1h e RPO 5min.
27. Como SRE, quero AWS Secrets Manager com rotação automática, para não conviver com chave em variável de ambiente.
28. Como SRE, quero migrations Alembic versionadas com `downgrade()` obrigatório, para que rollback de release nunca trave em schema.
29. Como SRE, quero auto-scaling com gatilho `sessions_active_per_instance` (não CPU), para escalar antes da fila de Sessão crescer.
30. Como SRE, quero monitor de uso por componente com alerta a 70% do teto de provider (Deepgram concurrent, OpenAI tokens/min), para escalar antes de bater.

### DPO interno (do componente)

31. Como DPO do componente, quero DPA assinado com Deepgram/OpenAI/Anthropic antes de qualquer paciente real, para que a transferência de dados sensíveis seja legal.
32. Como DPO do componente, quero ver a trilha de auditoria do componente (chamadas, sessões, exportações) com hash chain por 5 anos.
33. Como DPO do componente, quero a DPIA do tratamento que o componente executa assinada antes do primeiro paciente real, com revisão anual.
34. Como DPO do componente, quero notificar o Controlador (app de telemedicina) em < 24h em caso de breach detectado no componente (Art. 39 LGPD).
35. Como DPO do componente, quero a base legal de tratamento (tutela da saúde + execução de contrato com Controlador) **documentada** por finalidade no Profile YAML. **A base legal é decidida pelo Controlador (app de telemedicina) via DPA — o componente apenas documenta no YAML qual base se aplica ao tratamento que executa; não cabe ao componente determinar a base legal.**

### Dev do componente

36. Como dev, quero contrato `AudioSource` async-iterable implementado por `MicrophoneSource` (Voice CLI) e `AudioGateway` (web), para que o orquestrador seja agnóstico ao transporte.
37. Como dev, quero `STTService.stream(...)` como contrato adicional ao `transcribe(...)` legacy, para usar Voice CLI offline sem refatorar o web.
38. Como dev, quero `SessionContext` passado a todo resolver desde a 2.0 com `tenant_id="default"`, para que a Fase 3.0 só precise mudar cardinalidade de Tenants.
39. Como dev, quero stable error codes com namespace formal `<DOMAIN>_<COMPONENT>_<REASON>` e registry centralizado em `errors.py`, para evitar duplicação acidental. **Escopo: apenas códigos novos introduzidos na Fase 2; códigos legados (`STT_EMPTY`, `LLM_ERR_UNAVAILABLE` etc.) são preservados sem rename para não quebrar testes, observabilidade e contratos existentes.**
40. Como dev, quero estado de Sessão como append-only stream (`XADD session:{tenant}:{id}:events`) e `ConversationState` derivado por reduce, para nunca ter race condition em read-modify-write.
41. Como dev, quero `Profile.plan_features` (Básico/Médio/Premium) como toggles que o orquestrador consulta, para diferenciação operacional sem branches no código de domínio.

---

## Implementation Decisions

### Decisões fixadas

- **STT primário**: Deepgram Nova-2 streaming pt-BR. Fallback batch: OpenAI Whisper API offline. **Timeout Deepgram**: 800ms por segmento de áudio; estouro aciona fallback Whisper API para aquele segmento (frame re-enfileirado). **Degradação de sessão ativa**: se Deepgram cair durante sessão Premium/Médio, componente degrada graciosamente para batch — sessão continua viva, emite `{"type":"status","stt_mode":"fallback_batch"}` via WS; SLO de emergência: draft disponível em até 5 min após encerramento (mesmo contrato do Plano Básico normal). Fallback é transitório no Premium/Médio (retorna a streaming quando Deepgram recupera); no Básico é o modo permanente normal.
- **LLM primário**: GPT-4o. Fallback técnico: Sonnet 4.5. Gemini removido.
- **Codec WS**: Opus 20 ms, 16 kHz, mono.
- **VAD**: client-side no browser, fallback server-side.
- **Auth**: dois tokens com ciclos de vida distintos: (a) **JWT do app** (`Authorization: Bearer <jwt>`) — autentica apenas o `POST /v1/sessions` e o `POST /v1/sessions/{id}/token/refresh`; verificado offline via JWKS, sem roundtrip por request; o componente rejeita JWTs com `exp - iat > 900s` (15 min) para limitar janela de abuso pós-revogação; (b) **`ws_token`** — token opaco (UUID) armazenado no Redis com TTL = `session_max_minutes` do plano (60 min Básico/Médio, 240 min Premium); validado apenas no estabelecimento da conexão WS e na renovação, **não por frame**. O WebSocket usa exclusivamente o `ws_token`. **Renovação**: endpoint REST `POST /v1/sessions/{id}/token/refresh` autenticado com JWT do app — retorna novo `ws_token`, invalida o anterior atomicamente no Redis sem fechar o WS; o app entrega o novo token ao browser via canal próprio. O componente emite `{"type":"token_expiring_in":300}` via WS para avisar o app. **Revogação imediata**: `POST /v1/sessions/{id}/close` apaga o token do Redis em <1s. Se o app não chamar close (crash), o token expira junto com o hard cap da Sessão — janelas idênticas por design; risco residual formalmente aceito no DPIA. JWT inválido retorna 401; `ws_token` inválido encerra o WS com close code 4001.
- **Cloud**: AWS `sa-east-1`. ECS Fargate.
- **Postgres**: shared single-DB com `tenant_id NOT NULL DEFAULT default-uuid` em todas as tabelas. RLS escrita mas desligada. **Contenção de isolamento**: todo acesso ao banco passa por `TenantRepository` que injeta `WHERE tenant_id = :tid` obrigatoriamente via `SessionContext` — nenhum módulo acessa o banco diretamente. Teste de isolamento cross-tenant (query sem filtro de tenant deve falhar ou retornar vazio) entra no CI desde a 2.0. Em Fase 2.0/2.1 há apenas um tenant (UUID default), então vazamento cross-tenant é impossível operacionalmente — a contenção existe para garantir que a Fase 3.0 (quando novos tenants forem adicionados) não herde queries sem filtro.
- **Redis**: Cluster com hash tag `{tenant_id}:{session_id}` (single-node físico aceito).
- **Fila**: `asyncio.Queue` por processo para áudio em tempo real; Redis Streams para pipeline pós-Sessão. **Durabilidade de áudio**: frames de áudio são efêmeros por design — o componente não os persiste. A janela de 5s de reconexão é mantida no cliente (browser buffer); ao reconectar, o cliente reenvia esses frames. Se o container morrer com frames na queue ainda não processados, esses frames são considerados perdidos e o cliente os reenvia na reconexão. O Redis Stream registra eventos de transcrição (já processados pelo STT), não frames brutos de áudio.
- **Migrations**: Alembic versionado + reversível; CI bloqueia migration sem `downgrade()`.
- **Prompt caching**: somente system prompt estático; **proibido cachear contexto de Sessão em `medical`**.
- **Encryption-at-rest**: master key única no KMS gerenciado. TLS obrigatório em todas as conexões externas (incluindo Deepgram e OpenAI).
- **Residência de dados (Deepgram)**: Deepgram processa em US por padrão. O DPA com Deepgram deve incluir cláusulas de adequação para transferência internacional de dados sensíveis (Art. 33 LGPD) — padrão contratual ou mecanismo equivalente reconhecido pela ANPD. Este é requisito bloqueador hard do DPA, não detalhe operacional.
- **AuditLog**: mínimo Postgres com hash chain. Retenção 5 anos. Eventos cobertos: criação/encerramento de Sessão, geração de draft, exportação, login (chamada de API com API key). **Imutabilidade na Fase 2.x**: hash chain detecta adulteração mas não a impede — um DBA com acesso pode reescrever em cascade. Mitigações aceitas: (a) acesso DBA restrito a ≤ 3 pessoas nomeadas, (b) CloudTrail RDS registra toda operação privilegiada, (c) export diário de checkpoints do hash chain para S3 na Fase 2.1. **Risco residual deve ser formalmente aceito e assinado no DPIA antes do primeiro paciente real.** S3 Object Lock COMPLIANCE (imutabilidade técnica plena) é earmarked para Fase 3.1.
- **Hard caps**: Sessão 4h; warning a 30 min de silêncio; auto-finalize 6h. **Plan features por sessão**: `Profile.plan_features` (toggles Básico/Médio/Premium) são imutáveis após criação da Sessão — mudança de plano contratado pelo Controlador vale apenas na próxima criação de Sessão.
- **Escala em camadas**: baseline 10 simultâneas, auto-scale até 100 sem ação humana, > 100 exige ação humana (upgrade contratual de provider, instância maior, Cluster Redis).
- **Custo unitário alvo**: Sessão 30 min Premium ≈ US$ 0,55. Revisão mensal; alerta > 1,5×.

### Módulos novos

- `AudioGateway` — termina WebSocket bidirecional (Opus → PCM). Bounded queue 1s/Sessão; overflow → emite evento `STT_OVERFLOW` via WS, dropa frames, mantém sessão viva (não fecha WS). Diarização por roteamento (duas streams WS separadas: Médico e Paciente). Adiciona `server_timestamp_ms` no `XADD` de cada evento — esse é o relógio de referência para ordenação cronológica do diálogo; `seq` é por-stream e serve apenas para dedup dentro da stream. **Sticky routing**: ambas as streams WS de uma Sessão devem cair no mesmo container ECS — ALB configurado com session affinity via header `X-Session-Id`; no drain, clientes reconectam e o ALB roteia ao novo container. **Janela de reordenação**: 200ms — eventos de streams diferentes chegados dentro desse intervalo são ordenados por `server_timestamp_ms` antes de commitar ao `ConversationState`.
- `SessionManager` — ciclo de vida; idempotency-key obrigatório; hard cap 4h; auto-finalize 6h. **Drain gracioso**: no shutdown do ECS task, emite `{"type":"drain","reconnect_in":30}` via WS, rejeita novas Sessões com 503, aguarda até 90s; LLM calls em voo são canceladas após 90s e o resultado parcial é commitado como evento `partial_complete` no Redis Stream antes do encerramento — o `ConversationState` nunca fica em estado aberto sem evento de fechamento. `ECS_CONTAINER_STOP_TIMEOUT` deve ser configurado para 120s (default ECS Fargate é 30s — sem essa configuração explícita, o drain de 90s é truncado). Métrica `sessions_active` exportada por task via CloudWatch custom metric — autoscaler não faz scale-in enquanto houver Sessões ativas.
- `AuthService` (componente) — valida API key + JWT do app. Sem IdP próprio.
- `SttPool / LlmPool` — workers async. Prompt caching só system prompt; flag `allow_provider_prompt_cache` por Profile.
- `EventStream` — `XADD session:{tenant_id}:{session_id}:events` em Redis Streams como source of truth durante a Sessão; `ConversationState` derivado por reduce em tempo real. **Snapshot periódico**: a cada 100 eventos ou 5 minutos (o que ocorrer primeiro), o estado atual é serializado em `session:{tenant_id}:{session_id}:state_snapshot` com TTL = `session_max_minutes` do plano. Na reconexão pós-crash, o novo container carrega o snapshot + reduce do delta (≤ 100 eventos); na reconexão pós-drain gracioso, o container serializa o estado antes de encerrar (Option C como bônus). Perda de contexto LLM máxima no crash abrupto: 100 eventos — aceitável clinicamente; o draft usa o stream completo. **Ciclo de vida pós-Sessão**: no `close` (ou `auto-finalize`), o draft é materializado atomicamente no Postgres com `draft_status ∈ {pending, ready, failed}`; retry automático 3× com backoff exponencial antes de marcar `failed`. Stream Redis expira em 24h após close. `GET /v1/sessions/{id}/draft` lê sempre do Postgres: `pending` → 202 com `retry-after:5`; `failed` → 500 com `DRAFT_MATERIALIZATION_FAILED` + dados parciais (`partial:true`); nunca lê stream Redis diretamente. Em `failed` persistente: alerta SRE + `POST /v1/sessions/{id}/draft/retry` válido enquanto sessão existir no Postgres; retry usa stream Redis (≤24h) ou AuditLog como fallback.
- `AuditLogger` (mínimo) — append-only Postgres com hash chain; retenção 5 anos.
- `MetricsExporter` — Prometheus com label `tenant_id="default"` desde dia 1.
- `Observability` — OpenTelemetry, structlog JSON, Loki via Vector.

### Módulos do MVP preservados (núcleo estável)

`LLMService`, `ConversationState` (interface), `MedicalProfileEngine`, `InterviewFlow`, `ParticipantAttribution`, `SessionReview`, schema YAML de Perfil, `TurnResult`, stable error codes existentes. Quebra exige nova ADR.

### Contratos novos paralelos

- `AudioSource` async-iterable.
- `STTService.stream(audio_chunks_iter, on_partial, on_final, *, sample_rate=16000) -> AsyncIterator[STTEvent]` — método adicional; `transcribe(...)` legacy permanece.
- `SessionContext{tenant_id, session_id, session_kind, seniority, actor_role}`.
- WebSocket framing tipado bidirecional em `schemas/ws/v1/`.
- API HTTP versionada `/v1/sessions` com OpenAPI gerada.
- Eventos Redis Stream `session:{tenant_id}:{session_id}:events`.

### Planos do componente (diferenciação clínica/operacional)

| Eixo | Básico | Médio | Premium |
|---|---|---|---|
| STT | batch pós-Sessão | streaming | streaming + diarização |
| LLM principal | GPT-4o-mini | mix mini + GPT-4o | GPT-4o predominante |
| Red flag tempo real | ❌ assíncrono | ✅ | ✅ |
| Assistente proativo | ❌ | parcial | pleno |
| Cap Sessão | 60 min | 60 min | 4 h |
| Custo unitário | US$ 0,11 | US$ 0,28 | US$ 0,55 |

`Profile.plan_features` schema: toggles `stt_mode`, `llm_realtime_red_flag`, `llm_proactive_questions`, `llm_prontuario_model`, `session_max_minutes`. Default conservador em `medical`.

### Compliance como Operador (Fase 2.1)

- DPA com Deepgram/OpenAI/Anthropic — bloqueador hard antes de paciente real.
- DPIA do componente — antes do primeiro paciente real.
- DPO indicado (compartilhado com a empresa) — Art. 41 LGPD.
- Base legal: o componente processa dados sensíveis **sob instrução do Controlador** que detém a base legal Art. 11 §2 II "a" (tutela da saúde). O componente como Operador não invoca Art. 11 autonomamente — opera sob Art. 39 LGPD (obrigações do operador). O DPA interno deve tornar inequívoca essa cadeia: Controlador detém base legal → instrui o componente → componente processa sob essa instrução. Sem esse DPA, o componente não pode tratar dados de saúde.
- Notificação de breach ao Controlador em < 24h (Art. 39 LGPD).
- Sub-operadores (Deepgram, OpenAI) declarados em DPA com Controlador.
- Retenção: **áudio bruto não é persistido pelo componente** — efêmero por design (frames descartados após processamento STT). Retenção de áudio (gravação de consulta, se houver) é responsabilidade do app de telemedicina. Transcrição: 6-12 meses. AuditLog: 5 anos.
- DPA interno entre app de telemedicina e componente (mesmo sendo a mesma empresa) — formaliza Controlador/Operador, reusa template para Fase 3.

### SLOs por sub-fase

- **2.0**: TTFT p95 < 1,5s **(Planos Médio e Premium apenas)**; Plano Básico: transcrição disponível em até 5 min após encerramento da Sessão; WS reconnect < 3s; auto-scale 10→100 sem ação humana; trilha de auditoria sem gaps em 100% das Sessões. **Gate de release da 2.0 (checklist completo — todos obrigatórios):** (1) smoke test de carga: 10 sessões WS simultâneas de 10 min com áudio sintético, TTFT e reconnect dentro do SLO; (2) AuditLog: hash chain sem gaps verificado em 100% das sessões do smoke test; (3) AuthService: JWT expirado/inválido retorna 401, `ws_token` inválido fecha WS com 4001; (4) TenantRepository: query sem `tenant_id` retorna vazio (teste de isolamento no CI); (5) EventStream: reconexão WS com handshake `session_resumed` + dedupe por `event_id` validado em teste de chaos (kill container durante sessão ativa).
- **2.1**: 99,5% uptime mensal; DR restore < 1h; DPA com providers ativo em 100% antes de produção real.

---

## Testing Decisions

Princípio: **um bom teste valida comportamento externo observável, não detalhes de implementação**.

### Módulos com cobertura de teste nova

- **AudioGateway**: WebSocket com Opus encode/decode; backpressure (overflow → `STT_OVERFLOW`); reconexão preservando `seq`.
- **SessionManager**: idempotency-key (POST duplo retorna mesma Sessão); hard cap 4h; auto-finalize 6h; recuperação por `session_id`.
- **AuthService** (componente): valida API key com escopo; valida JWT do app; rejeita JWT expirado/inválido.
- **STTService.stream**: emissão de `partial`/`final` com Deepgram mock; fallback batch quando provider falha.
- **EventStream + ConversationState**: estado derivado por reduce do Redis Stream; idempotência via `event_id`.
- **AuditLogger**: hash chain (event N referencia hash de event N-1); ausência de gap; verificação periódica.

### Testes de carga e resiliência

- **k6 semanal** (Fase 2.1): 50 sessões WS de 30 min; SLO falha bloqueia release.
- **Chaos**: derrubar provider STT/LLM em ambiente staging; Sessão preserva histórico, ativa fallback de digitação manual via app, reconecta.
- **DR drill mensal** (Fase 2.1): restore de Postgres a partir de PITR/snapshot em ambiente paralelo; medir RTO real.

### Testes de domínio (preservados do MVP)

- WER ≤ 10% em 50 frases fixas — bateria refeita com Deepgram Nova-2 antes de fechar 2.0.
- Bateria de RED_FLAG em Voice CLI (Whisper) **e** Web (Deepgram) — falha bloqueia go-live.
- E2E p95 < 4s preservado; TTFT p95 < 1,5s adicionado.

### Prior art

- Estrutura `tests/test_issueNN_<topic>.py` preservada com prefixo de fase (`tests/fase2/test_issueNNN_...py`).
- Padrão de mock de STT/LLM (issues 6, 7, 27 do MVP) — extender para mocks de Deepgram, OpenAI streaming, Redis Streams.

---

## Out of Scope

### Vai para Fase 3 (vender separadamente)

- IdP próprio do assistente (Keycloak/Auth0), SSO federado por clínica.
- TenantResolver, RLS Postgres ligada, ProfileCatalog por Tenant, RateLimiter/Quota por Tenant.
- BillingExporter Stripe usage-based.
- Kill switch granular por Tenant.
- Status page público.
- UI hospedada whitelabel.

### Vai para Fase 3.1 (add-on Suite Compliance)

- SignatureService ICP-Brasil cloud (Soluti/Bry/Vidaas).
- IdentityService (KYC do Paciente via gov.br ou doc+selfie).
- ConsentService granular público.
- DSRPortal whitelabel.
- RetentionPolicy dual com expurgo automático.
- Envelope encryption per-Tenant.
- AuditLog em S3 Object Lock COMPLIANCE.

### Responsabilidade do app de telemedicina (Controlador)

- Convocar/agendar Consulta, identificar Paciente, autenticar Médico (CRM).
- **Sistema de prontuário eletrônico oficial** (CFM 1.821).
- **Assinatura ICP-Brasil** de prontuário/prescrição/atestado.
- Storage de prontuário 20 anos.
- Atender DSR LGPD do Paciente.
- DPO público de telemedicina.
- DPIA da operação completa.

### Permanece fora do produto indefinidamente

- Validação médica formal — produto entrega assistente, não substitui profissional.
- Tradução automática — produto é pt-BR.
- Modelos de IA proprietários — dependemos de providers comerciais.

---

## Further Notes

### Documentos relacionados

- `PRD.md` — PRD do MVP (Fase 1, fechado).
- `arquitetura-assistente-voz-mvp.md` — arquitetura completa, com aditivo Fase 2/Fase 3.
- `docs/adr/0001-fase-2-telemedicina-saas.md` — ADR com critérios de aceitação e revisão.
- `docs/glossario.md` — vocabulário canônico (uso obrigatório).
- `custos.md` — estimativas de custo por fase, plano e cenário.
- `PRD-fase3.md` — PRD da Fase 3 (SaaS B2B + add-on Suite Compliance + SKU Enterprise).

### Próximos passos com deadlines

1. Bateria de WER em 50 frases clínicas pt-BR (deadline 2026-06-01).
2. PRD da Fase 2 aprovado (deadline 2026-06-15).
3. Backlog técnico da Fase 2.0 priorizado em `ISSUES_FASE2.md` (deadline 2026-06-15).
4. DPA preliminar com Deepgram, OpenAI, Anthropic — começar negociação imediatamente (lead time ≥ 60 dias).
5. Template de DPIA do componente em `docs/compliance/dpia-componente.md` rascunhado pelo DPO designado (deadline 2026-06-30).
6. Template de DPA interno em `docs/compliance/dpa-internal.md`.

### Custo unitário alvo

Plano Premium 30 min ≈ US$ 0,55 (sem ICP/KYC — esses são do app de telemedicina, não do componente). Documentar e revisar mensalmente em `custos.md`.

### Risco regulatório residual do componente

Mesmo como Operador, riscos permanecem:
- ANPD pode questionar tratamento por sub-operadores (Deepgram/OpenAI) — DPA cobre, mas auditoria contratual recorrente.
- Multa LGPD: 2% do faturamento, até R$ 50M por infração — mitigação via DPA, AuditLog, retenção curta.
