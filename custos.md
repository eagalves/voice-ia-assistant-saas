# Custos do Projeto — Assistente de Voz (Componente)

> Documento de referência consolidando todos os custos do **assistente como componente** integrado a um app de telemedicina.
> **Fronteira clara**: assistente é **Operador LGPD** que processa áudio/texto sob instrução do app de telemedicina (Controlador). Custos de prontuário oficial, ICP-Brasil, KYC, DPO público de telemedicina **ficam com o app**, não com o assistente.
> Câmbio de referência: **US$ 1 = R$ 5,50** (atualizar quando aplicar).
> Última revisão: 2026-05-08 (rev. 2 — fronteira Controlador/Operador corrigida).

---

## Sumário executivo

Estimativa para **10 médicos simultâneos no pico**, 30 min por Consulta, horário comercial concentrado (~88h Sessão/mês = 176 Consultas/mês = 5.280 min Sessão/mês).

| Cenário | Custo mensal | Descrição |
|---|---|---|
| **Fase 2.0 — MVP do componente** | **~US$ 380-450** (R$ 2.090-2.475) | Infra + IA managed otimizada + observabilidade básica. Componente integrado ao seu app. |
| **Fase 2.1 — Hardening produção** | **~US$ 750-900** (R$ 4.125-4.950) | + Multi-AZ, DR, runbooks, SES, suporte AWS, DPO compartilhado, DPIA do componente. |
| **Fase 3.0 — SaaS B2B multi-tenant** (futuro) | **~US$ 1.200-1.500/mês** marginal sobre 2.1 | Status page, IdP, billing, feature flags, isolation testing. Cresce com Tenants. |
| **Fase 3.1 — Add-on "Suite Compliance"** (opcional, vendido a Tenants sem app próprio) | **+US$ 250-450 + variável por Tenant que contrata** | ICP-Brasil cloud, KYC, ConsentService público, DSRPortal whitelabel — só quando Tenant pagar pelo add-on |

**Custo unitário por Sessão** (médio, sem ICP/KYC porque são do app de telemedicina):
- Plano Básico: ~US$ 0,11 (R$ 0,60)
- Plano Médio: ~US$ 0,28 (R$ 1,55)
- Plano Premium: ~US$ 0,55 (R$ 3,02)

> ⚠️ **Custo de desenvolvimento (tempo de dev) não está neste documento.** Aqui só custos operacionais e variáveis. Custo de tempo de engenharia para construir cada fase é o maior item de despesa no início e deve ser orçado separadamente.

---

## 0. Fronteira Controlador / Operador (importante)

O assistente é **Operador LGPD** — processa dados sob instrução do **Controlador** (app de telemedicina). Esta tabela determina quais custos ficam onde:

| Responsabilidade | Quem paga e implementa |
|---|---|
| Convocar Consulta, agendar, identificar Paciente | **App de telemedicina** |
| Capturar Consentimento granular do Paciente | **App de telemedicina** |
| KYC do Paciente (gov.br ou doc+selfie) | **App de telemedicina** |
| Confirmar identidade do Médico (CRM) | **App de telemedicina** |
| **Sistema de prontuário eletrônico oficial** (CFM 1.821) | **App de telemedicina** |
| **Assinatura ICP-Brasil** de prontuário/prescrição/atestado | **App de telemedicina** |
| Storage do prontuário oficial 20 anos (CFM 1.821 Art. 8º) | **App de telemedicina** |
| Atender DSR LGPD do Paciente | **App de telemedicina** |
| DPO público da operação de telemedicina | **App de telemedicina** |
| | |
| Capturar áudio + STT + LLM | **Assistente** |
| Gerar conteúdo estruturado (Anamnese, draft, alertas red flag) | **Assistente** |
| Devolver conteúdo via API HTTP/WS para o app | **Assistente** |
| AuditLog do componente (quem chamou API, quando, exportações) | **Assistente** |
| Encryption-at-rest do áudio gravado pelo componente | **Assistente** |
| DPA com providers (Deepgram, OpenAI) — sub-operadores | **Assistente** |
| DPIA do tratamento operado pelo componente | **Assistente** |
| Retenção curta de áudio bruto (30-90 dias) e transcrição (6-12 meses) | **Assistente** |
| DPO **interno** do componente (pode ser compartilhado com a empresa) | **Assistente** |

**CFM 1.821 e CFM 2.314 não se aplicam ao assistente** porque ele não é "sistema de prontuário eletrônico" nem "operador de telemedicina" — é gerador de conteúdo que o sistema de prontuário consome. As normas se aplicam ao **app de telemedicina**.

---

## 1. Custos de IA (variáveis com uso)

### 1.1 STT — Speech-to-Text

Provider primário: **Deepgram Nova-2** streaming pt-BR (decisão fixada na ADR).

| Modo | Preço | Uso típico |
|---|---|---|
| Streaming | US$ 0,0043/min | Tempo real durante a Consulta |
| Pre-recorded (batch) | US$ 0,0029/min | Retranscrição offline; Plano Básico |
| Whisper local (se reabrir Ollama) | depende de GPU | Modo soberania de dados |

**Estimativa mensal a 10 simultâneas**:
- 5.280 min Sessão × US$ 0,0043 = **US$ 22,70/mês** (streaming, Médio/Premium)
- 5.280 min × US$ 0,0029 = **US$ 15,30/mês** (batch, Plano Básico)

### 1.2 LLM — Large Language Model

Provider primário: **OpenAI GPT-4o**. Fallback técnico: **Anthropic Sonnet 4.5**.

| Modelo | Input (US$/1M tok) | Output (US$/1M tok) | Custo relativo | Uso |
|---|---|---|---|---|
| GPT-4o | US$ 2,50 | US$ 10,00 | 16× | Premium; red flag; Prontuário em Médio |
| GPT-4o-mini | US$ 0,15 | US$ 0,60 | 1× (referência) | Básico; fluxos estruturados em Médio |
| Sonnet 4.5 | US$ 3,00 | US$ 15,00 | ~10× | Fallback técnico |
| Haiku 3.5 | US$ 1,00 | US$ 5,00 | ~3× | Alternativa de fallback |

**Estimativa mensal a 10 simultâneas**:
- Sessão típica de 30 min gera ~5k input + 2k output tokens.
- 176 Sessões × 7k tokens = ~1,2M input + ~352k output.
- **Plano Básico** (GPT-4o-mini, 1 chamada por Sessão): **~US$ 0,40/mês**.
- **Plano Médio** (mix mini + GPT-4o, com prompt cache): **~US$ 10-15/mês**.
- **Plano Premium** (GPT-4o predominante, com prompt cache): **~US$ 40-70/mês**.

### 1.3 Prompt caching (já decidido)

System prompt do Perfil cacheado reduz custo de input em 50%. Economia: ~30% dos tokens de input ≈ **US$ 4-15/mês** dependendo do plano.

### 1.4 Cenário alternativo descartado: Ollama local em GPU

Para registro (descartado para Fase 2 mas reaberto como SKU on-prem na ADR):

| Setup | Custo mensal | Notas |
|---|---|---|
| GPU AWS A10G (g5.xlarge, 24/7) | **US$ 850** | Sub-utilização ~88% no cenário 10 simultâneas |
| GPU própria (capex 36 meses) | US$ 140 + US$ 80 energia = **US$ 220** | Capex inicial R$ 25-35k |
| GPU spot (AWS) | **US$ 350-430** | **Inaceitável** para consulta clínica (interrupção) |
| Vast.ai / RunPod | **US$ 220-360** | Compliance LGPD problemática |

**Conclusão**: Ollama vence apenas com **≥ 30-50 médicos simultâneos sustentados**. Gatilho de migração: **200 mil min/mês de STT por região**.

---

## 2. Custos de infraestrutura cloud (AWS sa-east-1)

### 2.1 Compute — ECS Fargate (Server)

- 2 instâncias para HA + load balancing.
- 1 vCPU + 2 GB RAM cada; auto-scaling até 4-6 no pico de 100 simultâneas.

| Item | Mensal |
|---|---|
| 2 instâncias 24/7 | US$ 71 |
| Headroom auto-scale | +US$ 10-20 |
| **Total compute** | **~US$ 85/mês** |

### 2.2 RDS Postgres

- db.t4g.medium em sa-east-1; 200 GB SSD com PITR.

| Item | Mensal |
|---|---|
| Single-AZ Fase 2.0 | **~US$ 95** |
| Multi-AZ Fase 2.1+ | **~US$ 165** |

### 2.3 ElastiCache Redis

| Setup | Mensal |
|---|---|
| Single-node 2.0 | **~US$ 50** |
| Cluster Fase 3.0 | **~US$ 150** |

### 2.4 S3 — Storage (áudio + AuditLog do componente)

Retenção do áudio no componente é **curta** (30-90 dias para retranscrição offline; depois descarta ou Tenant-Controlador resgata).

| Item | Volume | Mensal |
|---|---|---|
| Áudio Opus retenção 90 dias | ~24 GB rotativos | US$ 0,55 |
| AuditLog do componente | ~5 GB/mês | US$ 0,15 |
| Glacier opcional para AuditLog longo | trivial | US$ 0,40 |
| Requests + lifecycle | trivial | US$ 1-2 |
| **Total S3** | | **~US$ 3-5/mês** |

### 2.5 ALB + Egress

| Item | Mensal |
|---|---|
| ALB | US$ 18 |
| LCUs (10 simultâneas) | US$ 5 |
| Egress (~20 GB/mês) | US$ 3 |
| **Total** | **~US$ 26/mês** |

### 2.6 Secrets Manager + KMS + CloudWatch

| Item | Mensal |
|---|---|
| Secrets (~10) | US$ 4 |
| KMS (encryption-at-rest) | US$ 5-10 |
| CloudWatch logs | US$ 4-6 |
| **Total** | **~US$ 15-20/mês** |

### 2.7 Backups e DR

| Item | Mensal |
|---|---|
| PITR Postgres | incluído |
| Snapshot diário cross-region (Fase 2.1+) | US$ 30-50 |

### Total infraestrutura AWS

| Fase | Mensal |
|---|---|
| **Fase 2.0** | **~US$ 270-300** |
| **Fase 2.1** (Multi-AZ + DR) | **~US$ 380-440** |
| **Fase 3.0** (Cluster Redis + multi-tenant) | **~US$ 480-560** |

---

## 3. Custos de observabilidade

### 3.1 Stack OpenTelemetry + structlog + Prometheus + Loki + Tempo

| Setup | Mensal |
|---|---|
| Grafana Cloud free tier (recomendado para começar) | **US$ 0** (até bater limites) |
| Grafana Cloud pago (após crescimento) | **~US$ 49-150** |
| Self-hosted (em AWS) | infra ~US$ 50-100 + custo de manutenção |

### 3.2 Error tracking — Sentry

- Free: 5k events/mês.
- Team: US$ 26/mês (50k events).

### 3.3 Status page (Fase 3.0)

- BetterStack: US$ 29/mês.

### 3.4 PagerDuty / Opsgenie

- Opsgenie free: 5 usuários.
- PagerDuty: US$ 21/usuário/mês.

**Estimativa total observabilidade**:
- **Fase 2.0**: US$ 0-26/mês.
- **Fase 2.1**: US$ 50-100/mês (Grafana pago + Sentry Team + on-call).
- **Fase 3.0**: +US$ 30 status page.

---

## 4. Compliance do componente (escopo reduzido pela fronteira)

> ❌ **Removidos do escopo do assistente** (vão para o app de telemedicina): ICP-Brasil cloud, KYC do Paciente, ConsentService granular público, DSRPortal público, DPO público de telemedicina, retenção 20 anos do prontuário.

### 4.1 DPA com providers (Deepgram, OpenAI, Anthropic)

- Não-recorrente direto. Custo de negociação (tempo jurídico): R$ 3-10k uma vez.
- Possível upcharge contratual de provider (~5-10% sobre tarifa padrão para BR-residency / opt-out de treinamento).
- **Estimativa**: **US$ 10-30/mês** se houver upcharge formal.

### 4.2 Encryption-at-rest

Master key KMS gerenciado pela AWS. Já contabilizado em infra (US$ 5-10/mês).

### 4.3 AuditLog do componente

Append-only Postgres com hash chain. Cobre: chamadas de API, criação/encerramento de Sessão, exportação de transcrição/draft. Já contabilizado em infra.

### 4.4 DPIA do componente (escopo reduzido)

DPIA focada apenas no tratamento que o componente executa (transcrever, gerar draft, retenção curta). Mais barata que DPIA da plataforma de telemedicina inteira.

| Item | Custo |
|---|---|
| Elaboração inicial | R$ 3.000-8.000 uma vez |
| Revisão anual | R$ 1.000-2.500/ano |
| **Mensal amortizado** | **~US$ 18-30** |

### 4.5 DPO compartilhado (interno da empresa)

Como Operador, o assistente precisa de DPO indicado (Art. 41 §1º). Pode ser **compartilhado com a empresa** (mesma pessoa que é DPO da telemedicina).

| Setup | Mensal alocado ao componente |
|---|---|
| DPO compartilhado dentro da mesma empresa | **US$ 100-200** (rateio) |
| DPO externo dedicado ao componente | US$ 270-545 |

### 4.6 Auditoria de segurança do componente (anual)

| Item | Anual | Mensal amortizado |
|---|---|---|
| Pen test do componente | R$ 8-20k | US$ 120-300 |
| Auditoria LGPD do escopo Operador | R$ 3-8k | US$ 45-120 |
| **Total** | | **~US$ 165-420** |

### Total compliance do componente

| Item | Mensal |
|---|---|
| DPA upcharge | US$ 10-30 |
| DPIA amortizado | US$ 18-30 |
| DPO compartilhado | US$ 100-200 |
| Auditoria + pen test amortizado | US$ 165-420 |
| **Total compliance Fase 2.1** | **~US$ 290-680/mês** |

Compare com a versão antes da fronteira corrigida (US$ 890-1.680/mês) — **redução de ~60%** porque ICP-Brasil e KYC saíram.

---

## 5. Custos do **app de telemedicina** (fora do assistente, listado para informação)

Estes custos **são seus**, mas estão no app de telemedicina, não no componente. Não entram no custo unitário por Sessão do assistente; entram no custo unitário do **produto telemedicina** que você vende ao Médico/clínica final.

### 5.1 ICP-Brasil — Assinatura digital qualificada

Provider recomendado: **Soluti (Bird ID)**.

| Item | Custo |
|---|---|
| Certificado em nuvem por médico | R$ 25-60/ano |
| Por assinatura individual | R$ 0,30-1,00 |
| Carimbo de tempo (TSA) | R$ 0,15-0,40 por carimbo |
| **Total estimado (10 médicos, 1.760 Prontuários/mês)** | **R$ 920/mês ≈ US$ 167** |

**Hack de custo**: agregar anotações de mesma Sessão num único PDF assinado uma vez no fechamento (não viola CFM 1.821 Art. 7º se cada anotação carrega autor/hora/conteúdo). Reduz custo em ~80%.

### 5.2 KYC do Paciente

- **gov.br**: API gratuita (recomendado).
- Provider comercial: US$ 0,30-1,00 por verificação.

**Estimativa** (cache por Paciente): **US$ 200-300/mês** estabilizado.

### 5.3 DPO público da telemedicina

LGPD Art. 41. DPO da empresa de telemedicina (público, com email e canal para titular).

| Setup | Mensal |
|---|---|
| DPO interno (parte do time) | n/a — custo de salário |
| **DPO externo** | **R$ 2.000-5.000/mês** ≈ US$ 365-910 |

### 5.4 Storage do prontuário oficial 20 anos (CFM 1.821 Art. 8º)

Storage longo prazo de PDFs assinados.

| Item | Custo |
|---|---|
| Volume estimado: 1.760 PDFs/mês × 200 KB = 350 MB/mês = 4 GB/ano | trivial |
| Glacier Deep Archive 20 anos | **~US$ 0,001/GB/mês** = **trivial** |

### 5.5 Storage de prontuário e dados clínicos do app

Postgres do app de telemedicina (consulta, prescrição, agendamento, paciente cadastrado, etc.). Custo escala com base de pacientes; estimativa inicial **~US$ 50-150/mês**.

---

## 6. Custos de auth, billing e ferramentas SaaS (Fase 3+)

### 6.1 IdP — Fase 3.0

| Provider | Mensal |
|---|---|
| Keycloak self-hosted | infra ~US$ 30-50 |
| Auth0 Starter B2B | US$ 240+ |

**Recomendação**: Keycloak self-hosted.

### 6.2 Feature flags — Fase 3.0

| Provider | Mensal |
|---|---|
| Unleash self-hosted | infra ~US$ 20 |
| LaunchDarkly Starter | ~US$ 100 |

### 6.3 Stripe Billing — Fase 3.0

Cobrado da receita: **3,4% + R$ 0,40** por transação cartão BR. Não é despesa direta — afeta margem.

### 6.4 Email transacional

- **AWS SES**: US$ 0,10 por 1.000 emails ≈ **~US$ 1-5/mês**.

### 6.5 SMS opcional

- AWS SNS BR: ~US$ 0,02-0,06 por SMS ≈ **~US$ 35-105/mês** se notificar todas Sessões.

---

## 7. Custos de desenvolvimento e CI/CD

| Item | Mensal |
|---|---|
| GitHub Free / Team | US$ 0-20 |
| GitHub Actions | US$ 0-30 |
| ECR | US$ 0,50 |
| Domínio | R$ 5 ≈ US$ 1 |
| Suporte AWS Developer | US$ 29 |
| **Total** | **~US$ 30-80/mês** |

Fase 2.2+: Suporte AWS Business **US$ 100/mês**.

---

## 8. Jurídico e administrativo (mensal recorrente)

| Item | Mensal |
|---|---|
| Contador SaaS BR | R$ 500-1.500 ≈ US$ 90-270 |
| Consultoria jurídica pontual | R$ 0-3.000 |
| Tributos (% sobre faturamento) | 6-15% (não fixo) |
| **Total recorrente** | **R$ 500-4.500/mês ≈ US$ 90-820** |

Não-recorrente ano 1: **R$ 8-25k** (advogado para termos + INPI + DPIA + auditoria inicial).

---

## 9. Custos por plano do assistente (recalculados sem ICP/KYC)

Premissa: 10 simultâneas, 30 min, 8h/dia × 22 dias = 176 Sessões/mês.

| Item | Básico | Médio | Premium |
|---|---|---|---|
| **STT** | batch US$ 0,087 | streaming US$ 0,129 | streaming + diarização US$ 0,168 |
| **LLM** (com cache) | mini 1× US$ 0,003 | mini + GPT-4o US$ 0,09 | GPT-4o predominante US$ 0,35 |
| **Infra alocada** | US$ 0,02 | US$ 0,02 | US$ 0,03 |
| **Custo unitário/Sessão** | **US$ 0,11** | **US$ 0,28** | **US$ 0,55** |
| **Mensal × 176 Sessões** | **US$ 19** | **US$ 49** | **US$ 97** |

**Preço sugerido por Sessão** (margem ~85-90%):

| Plano | Preço sugerido | Margem bruta |
|---|---|---|
| Básico | R$ 0,99/Sessão (~R$ 0,033/min) | ~89% |
| Médio | R$ 2,99/Sessão (~R$ 0,10/min) | ~91% |
| Premium | R$ 5,99/Sessão (~R$ 0,20/min) | ~91% |

> **Nota**: o Premium ficou **~30% mais barato** porque ICP-Brasil e KYC saíram do escopo do assistente. Você (no app de telemedicina) precificará a Sessão final ao Médico cobrindo (a) o assistente + (b) o seu próprio overhead de telemedicina (incluindo ICP/KYC).

---

## 10. Cenários de custo total mensal do **componente assistente**

### Cenário A — Fase 2.0, todos os Médicos no Plano Médio

| Categoria | Mensal |
|---|---|
| IA (STT + LLM mix) | US$ 50 |
| Infra AWS | US$ 280 |
| Observabilidade (Grafana free) | US$ 0 |
| Sentry (free) | US$ 0 |
| Suporte AWS Developer | US$ 29 |
| Email SES | US$ 3 |
| **Total Fase 2.0** | **~US$ 362** ≈ **R$ 1.991** |

### Cenário B — Fase 2.1, hardening produção

| Categoria | Mensal |
|---|---|
| IA (igual A) | US$ 50 |
| Infra AWS (Multi-AZ + DR) | US$ 410 |
| Observabilidade (Grafana pago) | US$ 49 |
| PagerDuty 2 usuários | US$ 42 |
| Suporte AWS Business | US$ 100 |
| Compliance Operador (DPA, DPIA, DPO compartilhado, pen test amortizado) | US$ 290-680 |
| Contador | US$ 145 |
| **Total Fase 2.1** | **~US$ 1.086-1.476** ≈ **R$ 5.973-8.118** |

### Cenário C — Fase 2.1 com Premium ativo (todos Médicos no plano Premium)

Idêntico ao B, mas com IA mais cara:

| Categoria | Mensal |
|---|---|
| IA (Premium, GPT-4o predominante) | US$ 97 |
| Demais (igual B) | US$ 989-1.379 |
| **Total Fase 2.1 Premium** | **~US$ 1.086-1.476** |

(IA é fração pequena; o que pesa é infra + compliance.)

### Cenário D — Fase 3.0 inicial (vendendo SaaS, 5 Tenants externos)

| Categoria | Mensal |
|---|---|
| Tudo do Cenário B | US$ 1.080-1.476 |
| Status page | US$ 30 |
| Keycloak infra | US$ 50 |
| Unleash infra | US$ 20 |
| Suporte AWS Business | (já contabilizado) |
| **Total Fase 3.0** | **~US$ 1.180-1.576** ≈ **R$ 6.490-8.668** |

### Cenário E — Fase 3.1 com add-on Suite Compliance ativo (1 Tenant comprou)

Custos adicionais quando um Tenant ativa o add-on:

| Categoria | Mensal por Tenant que ativa |
|---|---|
| ICP-Brasil cloud (Soluti) | US$ 167 |
| KYC gov.br + amortização | US$ 250 (estabilizado) |
| ConsentService + DSRPortal whitelabel | infra ~US$ 20 |
| **Total marginal por Tenant** | **~US$ 437** |

**Preço sugerido do add-on**: R$ 1.500-2.500/mês por Tenant. Margem ~80%.

### Cenário F — Crescimento para 100 simultâneas (Fase 2.1 Premium)

| Categoria | Mensal |
|---|---|
| IA (10× volume) | US$ 970 |
| Infra AWS (4× compute, 2× DB, Cluster Redis) | US$ 1.300 |
| Observabilidade pago | US$ 250 |
| Compliance Operador (escala lenta) | US$ 600 |
| Suporte AWS Business (10% do uso) | US$ 200 |
| **Total 100 simultâneas** | **~US$ 3.320** ≈ **R$ 18.260** |

Custo unitário Sessão: **~US$ 1,89** (overhead fixo dilui melhor em volume alto). Receita correspondente cresce 10× também (~R$ 105k em Premium) → margem ~83%.

---

## 11. Como reduzir custo (alavancas eficazes)

Em ordem de impacto:

1. **Roteamento de modelo por tarefa** (GPT-4o-mini para fluxos simples) → -US$ 60-100/mês a 10 simultâneas.
2. **Prompt caching** (já decidido) → -30% tokens input.
3. **STT batch para retranscrição offline** (já decidido) → 33% mais barato que streaming.
4. **Hard cap de Sessão + auto-finalize** (já decidido) → previne vazamento.
5. **Cache de respostas template** (Anamnese inicial) → -10-15% chamadas LLM.
6. **STT confidence threshold otimizado** → -5-10% chamadas LLM em ambiente ruidoso.
7. **Plano com compromisso de volume** (Deepgram Enterprise, OpenAI Tier) → -20-30%.
8. **Retenção curta de áudio** (30-90 dias no componente) vs longa → economia de storage marginal mas alinha LGPD.
9. **DPO compartilhado** com a empresa em vez de externo dedicado → -US$ 200-345/mês.
10. **Grafana Cloud free** enquanto comporta → US$ 0 vs US$ 49+.
11. **Keycloak self-hosted** (Fase 3) vs Auth0 → -US$ 240/mês.
12. **gov.br para KYC no app de telemedicina** vs provider comercial → -US$ 800/mês na fase de cadastro alta (custo do app, não do assistente).
13. **Migrar para Whisper local + LLM self-hosted** quando STT atingir 200 mil min/mês → ponto de virada de margem.

---

## 12. Custos não-monetários (orçar separadamente)

- **Tempo de engenharia** para construir cada fase. Maior custo no início.
- **Tempo de produto** para definir Profile YAMLs por especialidade.
- **Tempo jurídico** para revisar contratos, DPA com app de telemedicina (mesmo interno), termos.
- **Tempo do médico-consultor** para validar Profile clínico, red flags, anamnese.
- **Risco regulatório** — assistente como Operador pode ser penalizado se Controlador (app de telemedicina) sofrer breach e atribuir responsabilidade. Multa ANPD: 2% do faturamento, até R$ 50M.
- **Risco operacional** (incidente em consulta clínica derivado de erro do assistente).
- **Risco de lock-in** de provider STT/LLM.

---

## 13. Como manter este documento

- **Atualizar mensalmente** os valores reais de uso (consumo real ≠ estimativa).
- **Revisar premissa de câmbio** (US$ 1 = R$ 5,50) a cada trimestre.
- **Revisar contratos com providers** anualmente.
- **Adicionar nova linha** sempre que um custo recorrente for descoberto na operação.
- **Comparar com baseline** quando custo unitário por Sessão sair de ±20% do alvo (US$ 0,11 / 0,28 / 0,55).

---

## 14. Referências

- ADR: `docs/adr/0001-fase-2-telemedicina-saas.md`
- Arquitetura: `arquitetura-assistente-voz-mvp.md` (seção "Aditivo Fase 2 e Fase 3")
- PRD Fase 2 (componente): `PRD-fase2.md`
- PRD Fase 3 (SaaS B2B): `PRD-fase3.md`
- Glossário: `docs/glossario.md`
- Presets de latência (uso em dev): `docs/latency_presets.md`

**Pricing fontes** (atualizar periodicamente):
- Deepgram pricing: https://deepgram.com/pricing
- OpenAI pricing: https://openai.com/api/pricing
- AWS sa-east-1 pricing: https://aws.amazon.com/pricing
- ICP-Brasil providers (para o app de telemedicina, não para o assistente): Soluti, Bry, Vidaas, Certisign
