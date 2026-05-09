# ADR 0001 — Evoluir o Assistente para Plataforma de Telemedicina SaaS

- **Status**: Aprovado (rev. 3 após refinamento de modelo de consumo)
- **Data**: 2026-05-08
- **Data revisão**: 2026-05-08 (rev. 3 — fronteira Controlador/Operador + divisão Fase 2/Fase 3)
- **Deadline decisão final**: 2026-06-15
- **Decisor**: time do projeto
- **Substitui**: nenhum
- **Substituído por**: nenhum

## Contexto

O MVP atual (issues 1-30 fechadas) é um assistente de voz desktop, single-user, single-tenant, com STT local (Whisper) e LLM via API (OpenAI/Gemini) ou local (Ollama). Atende um único profissional em uma única máquina. Sessão é mantida em RAM e perdida ao encerrar o processo. Perfil de domínio é escolhido por variável de ambiente no boot e vale para todo o ciclo de vida da aplicação.

A intenção do produto é integrar este assistente a um aplicativo web de telemedicina, com vários médicos atendendo simultaneamente em várias clínicas, transcrição em tempo real durante consulta remota e prontuário estruturado emitido ao final. O contexto regulatório brasileiro impõe requisitos arquiteturais não-triviais: LGPD (Lei 13.709/2018) trata dados clínicos como sensíveis, CFM Resolução 2.314/2022 regula telemedicina, e CFM Resolução 1.821/2007 regula prontuário eletrônico — incluindo assinatura digital ICP-Brasil.

A arquitetura do MVP foi desenhada com contratos abstratos (`STTService`, `LLMService`, `VoiceOrchestrator`, `ConversationState`) e perfis YAML versionados, antecipando explicitamente essa transição na seção "Fase C" do documento de arquitetura: "manter contratos, substituir implementações locais por serviços".

## Decisão

Evoluir o assistente para uma plataforma web em **duas fases macro com base no modelo de consumo**, preservando o núcleo de domínio do MVP intacto:

### Fase 2 — Componente integrável no app de telemedicina (escopo imediato)

O assistente passa a ser um **componente API HTTP + WebSocket** consumido pelo app de telemedicina do produtor (que é o **Controlador LGPD**; o assistente é **Operador**). CFM 1.821 e CFM 2.314 não se aplicam ao componente — aplicam-se ao app de telemedicina. Compliance pública (ICP-Brasil, KYC, ConsentService público, DSRPortal, DPO público) **fica fora do escopo do componente**.

- **Fase 2.0 — MVP do componente**: API+WS, multi-tenant *ready* (schema com `tenant_id NOT NULL DEFAULT default-uuid`, hash tag Redis, métricas com label), STT Deepgram Nova-2 streaming + LLM GPT-4o, AuditLog mínimo com hash chain, encryption-at-rest, observabilidade plena. Auth via API key + JWT entregue pelo app (sem IdP próprio).
- **Fase 2.1 — Componente em produção**: Multi-AZ, DR (RTO 1h, RPO 5min), runbooks, k6 semanal, DPA com providers, DPIA do componente, DPO compartilhado com a empresa. Hardening como Operador LGPD.

### Fase 3 — SaaS B2B Multi-Tenant (futuro, quando decidir vender separadamente)

Quando o produto for vendido a outros apps/clínicas como Tenants externos:

- **Fase 3.0 — Multi-Tenant Operacional**: Postgres RLS ligada (escrita-e-desligada na 2.x), IdP próprio (Keycloak), TenantResolver, RateLimiter/Quota por Tenant, ProfileCatalog por Tenant, Billing Stripe usage-based, Kill switch granular, Status page público.
- **Fase 3.1 — Add-on "Suite Compliance"** (opcional, vendido a Tenants sem app próprio): SignatureService ICP-Brasil cloud, IdentityService (KYC), ConsentService granular público, DSRPortal whitelabel, RetentionPolicy dual, envelope encryption per-Tenant, AuditLog em S3 Object Lock COMPLIANCE como source-of-truth.
- **Fase 3.2 — SKU Enterprise**: VPC dedicada por Tenant, DB-per-tenant, KMS dedicado, reabertura formal de A3 (hospital-on-prem) se ≥ 3 Tenants enterprise pedirem.

**Fronteira Controlador/Operador**: o assistente como Operador cumpre obrigações de Art. 39 (segurança, breach notification, DPA com sub-operadores, retenção curta). O Controlador (app de telemedicina) cumpre CFM 1.821/2.314, ICP-Brasil por evento, KYC, DSR, DPO público.

## Drivers

1. **Mercado-alvo** (telemedicina BR) é multi-clínica por natureza; arquitetura desktop é beco sem saída comercial.
2. **Compliance LGPD/CFM é não-negociável e tem implicações arquiteturais profundas** — não pode ser "adicionada depois" sem retrabalho grande (criptografia, trilha de auditoria e isolamento de tenant precisam estar nos alicerces).
3. **Validação de domínio já feita.** O MVP validou a parte difícil — anamnese guiada, red flags, fluxo de entrevista, perfis de domínio, atribuição de participantes. A transição é de empacotamento e escala, não de produto.
4. **Elasticidade e WER em vocabulário clínico pt-BR.** O argumento real para mover STT/LLM para serviços gerenciados não é "latência absoluta" — Whisper local em GPU dedicada atinge latência viável. O argumento real é **(a)** elasticidade (cada Tenant abre N Sessões simultâneas; CapEx de GPU por pico é proibitivo no estágio atual) e **(b)** WER em vocabulário clínico pt-BR significativamente melhor em providers comerciais com modelos especializados (Deepgram Nova-2). A latência absoluta entra como consequência, não como driver; o SLO TTFT p95 < 1,5s é atingível tanto com managed quanto com self-hosted bem dimensionado. O gatilho documentado de migração para self-hosted é 200 mil min/mês STT por região, quando a economia inverte.

## Alternativas consideradas

### A1 — Continuar como app desktop distribuído (Electron/Tauri)
Descartada. Não escala para múltiplos médicos simultâneos do produto-alvo, exige instalação por máquina, não suporta consulta remota (paciente em outro lugar) nativamente, e duplica todo o esforço de compliance por instalação.

### A2 — Big bang direto para Fase 2.2 completa
Descartada. Escopo grande demais para um único ciclo, atrasa validação de mercado, e fatiar permite aprender (e ajustar contratos entre fases) sem retrabalho destrutivo.

### A3 — Manter STT local (cliente) e mover só LLM para servidor
Descartada para o caso telemedicina remota: captura de áudio do navegador inviabiliza Whisper local no cliente, e o ganho de "privacidade de áudio local" é ilusório quando o áudio precisa atravessar a rede para chegar ao servidor onde o médico está remoto.

**Reaberta parcialmente como SKU enterprise on-prem futuro (não no escopo da 2.0–2.2)**: hospitais/redes que exigem que **nenhum áudio saia da rede da instituição** ganham uma SKU on-prem onde STT roda em GPU dedicada dentro da rede do hospital, mas o LLM sai (anonimizado) ou também roda local. Distinção crítica: A3 desktop/cliente está descartado; A3 hospital-on-prem permanece no roadmap como produto de fronteira, com modelo de receita licença + suporte (não SaaS recorrente). Documentado para evitar perda dessa opção comercial em rodadas futuras de discovery.

### A4 — Hospedar STT em GPU própria em vez de managed service
Considerada para Fase 2.1+. Permanece no roadmap como otimização de custo, mas não bloqueia 2.0. Decisão adiada — managed STT na 2.0 valida produto sem assumir complexidade operacional de GPU. **Gatilho documentado: 200 mil min/mês de STT por região**.

### A5 — Ollama como provider primário de produção web
Descartada para Fase 2. Em volume baixo (10 simultâneas), Ollama em GPU dedicada é objetivamente mais caro (US$ 220-850/mês vs US$ 22/mês de Deepgram managed) por causa da sub-utilização da GPU 24/7. Acrescenta complexidade operacional (CUDA OOM, fila de modelo, atualização manual) e qualidade pior em pt-BR clínico (modelos abertos 7B têm format compliance e reasoning inferiores ao GPT-4o). Reaberta como SKU "soberania de dados" / on-prem (vide A3) quando hospital exigir áudio nunca sair da rede da instituição. Voice CLI mantém Ollama como ferramenta de dev/teste sem custo de API.

## Consequências

### Positivas
- Reuso de ~70% do código atual (todos os módulos de domínio + contratos abstratos).
- Latência ponta a ponta volta a ser viável (managed STT/LLM em data center).
- Compliance pode ser endereçada de forma centralizada em vez de duplicada em cada máquina.
- Modelo de negócio SaaS recorrente em vez de licença desktop.
- Telemetria centralizada permite melhoria contínua orientada por dados reais.

### Negativas
- Custos operacionais recorrentes (managed STT/LLM são pagos por minuto / token).
- Dependência de provedores externos (Deepgram, OpenAI, Gemini etc.) com seus próprios SLAs.
- Superfície de ataque aumenta (servidor exposto, dados centralizados).
- Time precisa ganhar competência em operação de SaaS (CI/CD, observabilidade, on-call, segurança).
- LGPD/CFM exigem investimento contínuo, não one-shot (DPO, auditorias).
- **Aumento de complexidade regulatória sustentada**: DPO formal, DPIA anual, DPA com providers, auditorias periódicas. Custo recorrente de compliance que não diminui com escala — só dilui.
- **Lock-in em provider STT/LLM**: contratos longos (DPA, BR-residency negociada caso a caso) tornam migração custosa mesmo com adapters. Mitigação: adapters atrás de contrato + gatilho de migração documentado, mas o custo de switching é real.
- **Dependência de SLA externo no caminho crítico**: cada Sessão depende de Deepgram + OpenAI estarem online. Provedor com 99,9% mensal = ~43 min/mês de janela. Composto com outros componentes, exige fallback de digitação ativo e prática operacional para acionar.

### Neutras
- Captura de áudio passa a depender de WebRTC/MediaRecorder do navegador — qualidade variável por dispositivo.
- Sessão deixa de ser efêmera e passa a ser auditável — bom para compliance, exige política de retenção desde o dia 1.
- Voice CLI continua existindo como ferramenta de dev/teste, sem ser produto.

## Notas de implementação

- O MVP fecha na versão `0.x`. Fase 2.0 abre a `1.x`.
- Os contratos núcleo (`LLMService`, `ConversationState`, `MedicalProfileEngine`, `InterviewFlow`, `ParticipantAttribution`, `SessionReview`, schema YAML de Perfil, `TurnResult`, stable error codes existentes) são **públicos e estáveis** durante toda a Fase 2. Quebra exige nova ADR.
- **Novos contratos paralelos podem ser introduzidos sem quebra dos existentes.** Especificamente: (a) `STTService.stream(...)` é método adicional ao `STTService.transcribe(...)` legacy — não substituição; (b) `AudioSource` async-iterable é abstração nova que `MicrophoneSource` (desktop) e `AudioGateway` (web) passam a implementar — **a transição síncrona → async do orquestrador é quebra declarada e versionada como major**; (c) `SessionContext` é parâmetro novo passado a resolvers, sem alterar assinaturas internas dos módulos núcleo.
- Esta política operacional é: **núcleo estável, transporte e orquestração evoluem com versionamento explícito**.
- Profile resolver muda (boot global → por Sessão), mas o schema dos arquivos YAML não muda.
- Telemetria existente (`stt.latency_ms`, `llm.latency_ms`, `e2e.latency_ms`, `llm.ttft_ms`) é exportada para Prometheus na 2.0 — sem renomear métricas.
- Stable error codes existentes são preservados; novos códigos são adicionados, não renomeados.

## Critérios de aceitação

Esta ADR é considerada implementada e fechada quando, **cumulativamente**:

1. Aditivo Fase 2 e Fase 3 mergeado em `arquitetura-assistente-voz-mvp.md` com fronteira Controlador/Operador explícita.
2. Glossário canônico atualizado com novas entradas (`TenantContext`, `SessionContext`, `TurnResult`, `Prontuário draft`, `Diarização`, `Endpointing`, `Idempotency-key`, `Quota vs Rate limit`, `RPO`, `RTO`, `DPA`, `Tutela da saúde`, `Kill switch`, ajuste em `Consulta`/`Sessão` e `Consentimento`).
3. `PRD-fase2.md` (componente do app) consolidado e aprovado.
4. `PRD-fase3.md` (SaaS B2B + add-on Suite Compliance + SKU Enterprise) rascunhado com prioridade pós-Fase 2.
5. `custos.md` atualizado com fronteira Controlador/Operador e estimativas por fase.
6. Decisões fixadas (STT primário, LLM primário, codec WS, cloud, base legal LGPD primária, retenção dual, encryption strategy, escala em camadas) registradas neste documento e no aditivo.
7. Backlog inicial da Fase 2.0 (issues técnicas) criado e priorizado.
8. DPA com Deepgram/OpenAI/Anthropic em negociação ativa (lead time iniciado).
9. Template de DPA interno entre app de telemedicina e componente assistente rascunhado em `docs/compliance/dpa-internal.md`.

## Critérios de revisão

Esta ADR deve ser **revisada e potencialmente revisitada** quando:

- Volume de STT atingir 200 mil min/mês por região (gatilho de avaliação self-hosted).
- WER em produção exceder 12% sustentado por 4 semanas em qualquer Tenant `medical`.
- Custo unitário por Sessão exceder 1,5× baseline (US$ 0,825) em revisão mensal.
- ANPD emitir orientação que conflite com decisões aqui (DPA, residência, base legal).
- CFM publicar resolução nova que altere requisitos de telemedicina ou prontuário.
- Provedor primário (Deepgram ou OpenAI) descontinuar serviço crítico ou alterar termos materialmente.
- Mais de 3 Tenants enterprise solicitarem SKU on-prem (reabre A3 hospital-on-prem com prioridade).
- Decisão de **vender o assistente separadamente** (gatilho que ativa formalmente o roadmap da Fase 3, hoje provisório).

**Deadline de decisão final**: 2026-06-15. Após esta data, decisões fixadas viram base imutável até nova ADR.

## Referências

- `PRD.md` — PRD do MVP (Fase 1).
- `arquitetura-assistente-voz-mvp.md` — arquitetura do MVP, com aditivo da Fase 2.
- `ISSUES_MVP.md` — backlog do MVP (fechado).
- `docs/glossario.md` — vocabulário canônico do projeto.
- LGPD: Lei 13.709/2018.
- CFM Resolução 2.314/2022 (telemedicina).
- CFM Resolução 1.821/2007 (prontuário eletrônico).
- ICP-Brasil: MP 2.200-2/2001.
