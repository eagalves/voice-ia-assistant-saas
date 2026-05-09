# PRD — Fase 3: Assistente como SaaS B2B Multi-Tenant

> **Status**: Provisório (ativa-se quando decisão de vender o assistente separadamente for tomada).
> **Decisão arquitetural**: `docs/adr/0001-fase-2-telemedicina-saas.md`.
> **Arquitetura detalhada**: `arquitetura-assistente-voz-mvp.md`, seção "Aditivo Fase 2 e Fase 3".
> **Custos**: `custos.md`.
> **Predecessor**: `PRD-fase2.md` (componente integrado ao app de telemedicina próprio).
> **Trigger**: decisão formal de vender o assistente como SaaS independente a outros apps/clínicas.

---

## Problem Statement

A Fase 2 entrega o assistente como **componente integrado ao app de telemedicina do produtor**. Quando o produtor decidir **vender o assistente separadamente** a outros apps de telemedicina, clínicas e redes, o componente precisa virar **plataforma SaaS B2B multi-tenant** — com isolamento estrito de dados entre Tenants, billing por uso, IdP próprio, fairness de recursos e (opcionalmente) compliance estendida para Tenants que não têm app de telemedicina próprio.

O risco de adiar a Fase 3 indefinidamente é arquitetural: decisões da Fase 2 que não consideraram multi-tenant readiness criam dívida técnica destrutiva (migração com downtime, dashboards de observabilidade quebrados, schema sem `tenant_id`). Por isso a Fase 2 já entrega **multi-tenant ready** (schema preparado, métricas com label, hash tag Redis) — a Fase 3 só "liga" o que ficou escrito-e-desligado.

---

## Solution

Plataforma SaaS B2B multi-tenant entregue em três sub-fases:

### Fase 3.0 — Multi-Tenant Operacional

Liga RLS no Postgres, adiciona IdP próprio (Keycloak), introduz isolamento e fairness por Tenant, billing usage-based via Stripe, kill switch granular, status page público. Tenants externos passam a ter contas independentes com isolamento auditável.

### Fase 3.1 — Add-on "Suite Compliance" (opcional)

Vendido como add-on contratado a Tenants que **não têm app de telemedicina próprio** (clínicas pequenas, médicos autônomos). Adiciona ao componente, **apenas para Tenants que ativam**: SignatureService ICP-Brasil cloud, IdentityService (KYC), ConsentService granular público, DSRPortal whitelabel, RetentionPolicy dual, envelope encryption per-Tenant, AuditLog em S3 Object Lock COMPLIANCE.

### Fase 3.2 — SKU Enterprise

Para Tenants regulados (hospitais, redes) que exigem isolamento físico: VPC dedicada por Tenant, DB-per-tenant Postgres, KMS dedicado por SKU. Reabertura formal de A3 (hospital-on-prem) se ≥ 3 Tenants enterprise solicitarem.

---

## User Stories

### Tenant Admin (administrador de clínica/app cliente)

1. Como Tenant Admin, quero cadastrar e desativar Médicos da minha organização via API admin, para gerir acesso sem suporte do fornecedor.
2. Como Tenant Admin, quero escolher quais Perfis (medical, interviewer, etc.) ficam disponíveis para meus Médicos, escolhendo do catálogo global publicado.
3. Como Tenant Admin, quero customizar Perfis publicados (texto de prompts, listas de campos) via API admin com audit log.
4. Como Tenant Admin, quero ver consumo (minutos STT, tokens LLM, Sessões abertas) por Médico e por mês na minha organização.
5. Como Tenant Admin, quero ser avisado quando minha quota mensal estiver próxima do limite, para fazer upgrade antes do bloqueio.
6. Como Tenant Admin, quero contratar o add-on "Suite Compliance" (Fase 3.1) se minha organização não tem app de telemedicina próprio, para que o assistente cubra ICP-Brasil + KYC + DSR.
7. Como Tenant Admin enterprise (Fase 3.2), quero contratar SKU com VPC dedicada e DB-per-tenant, para atender requisitos regulatórios da minha rede hospitalar.

### Médico (usuário final em Tenant cliente)

8. Como Médico em Tenant cliente, quero fazer login via SSO da minha clínica (federado) ou via IdP do produto (Keycloak), sem credenciais novas.
9. Como Médico em Tenant cliente, quero a experiência ser idêntica à do app de telemedicina nativo do produtor — diferença é só comercial, não técnica.

### Paciente (em Tenant que tem app de telemedicina próprio)

10. Como Paciente, quero clicar no link do app do meu Tenant e entrar na Consulta — autenticação é do app, não do assistente. Mesmo modelo da Fase 2.

### Paciente (em Tenant com add-on "Suite Compliance" ativo)

11. Como Paciente em Tenant com add-on, quero consentir com cláusulas opt-in independentes (tratamento clínico, gravação, retenção, compartilhamento com providers, ambiente privado CFM 2.314) antes do início.
12. Como Paciente em Tenant com add-on, quero comprovar minha identidade via gov.br ou doc+selfie (KYC) antes da Consulta.
13. Como Paciente em Tenant com add-on, quero exercer meus direitos LGPD (acesso, correção, portabilidade, eliminação respeitando retenção legal) por DSRPortal whitelabel do meu Tenant.

### DPO (em Tenant cliente)

14. Como DPO de Tenant cliente, quero responder a um pedido LGPD Art. 18 em < 15 dias com auditoria completa, usando os endpoints do DSRPortal (se add-on ativo) ou recebendo dados do assistente para responder pelo meu próprio sistema.
15. Como DPO de Tenant, quero ver a trilha imutável de quem do meu Tenant acessou qual Sessão de qual Paciente, isolada de outros Tenants.

### SRE (operador da plataforma)

16. Como SRE, quero traces e logs com label `tenant_id` real para filtrar incidente por Tenant em segundos.
17. Como SRE, quero teste de penetração cross-tenant rodando no CI semanal — 0 leakage permitido.
18. Como SRE, quero CI bloquear PR que crie tabela sem `tenant_id NOT NULL` ou sem RLS policy.
19. Como SRE, quero kill switch granular por Tenant (tabela `tenants(status)` + flag Redis `KILL_SWITCH:{tenant_id}`) para suspender Tenant em < 1 minuto sem deploy.
20. Como SRE, quero feature flags por Tenant via Unleash, para canary de feature nova em Tenant beta antes de geral.
21. Como SRE, quero fairness de fila STT/LLM por Tenant (token bucket), para Tenant N não degradar Tenant M.
22. Como SRE, quero monitor de margem por Tenant (custo unitário vs receita) com alerta quando custo > 50% da receita do Tenant.

### Comercial / Customer Success

23. Como comercial, quero status page público com SLO ≥ 99,5% mensal, para responder a auditoria de cliente sem ticket.
24. Como Customer Success, quero ver consumo por plano (Básico/Médio/Premium) por Tenant, para detectar Tenant com uso anômalo.
25. Como comercial, quero oferecer SKU Enterprise (Fase 3.2) com VPC dedicada e DB-per-tenant a ≥ 3× preço shared, para fechar com cliente regulado.
26. Como comercial, quero oferecer SKU on-prem (fora do escopo de 3.x) para hospital que exige áudio nunca sair da rede — ativar quando ≥ 3 Tenants enterprise pedirem.
27. Como comercial, quero o add-on "Suite Compliance" (Fase 3.1) como upsell para Tenants que entram sem app próprio, com preço sugerido R$ 1.500-2.500/mês e margem ~80%.

### Compliance contratual

28. Como responsável pelo produto, quero DPA padrão com cada Tenant cliente (template reusado da Fase 2 interna), para formalizar relação Controlador/Operador.
29. Como responsável pelo produto, quero DPA com Deepgram/OpenAI/Anthropic em vigor antes de qualquer Tenant cliente (já vem da Fase 2.1 — preservado).
30. Como responsável pelo produto, quero providers preferencialmente com BR-residency ou opt-out de uso para treinamento, para reduzir lock-in regulatório.

### Dev (interno)

31. Como dev, quero `TenantResolver` middleware extraindo `tenant_id` do JWT e fazendo `SET LOCAL app.tenant_id = <uuid>` em toda transação Postgres.
32. Como dev, quero migration "ligar RLS" sem refactor de código (policy escrita já existe desde a Fase 2.0; só ALTER TABLE ENABLE ROW LEVEL SECURITY).
33. Como dev, quero `ProfileResolver(SessionContext)` aceitar `tenant_id` real (não mais `"default"`) sem mudança de assinatura.
34. Como dev, quero schemas `schemas/ws/v1/`, `schemas/events/v1/`, OpenAPI `/v1/` versionados — eventual mudança breaking vira `/v2/`.

---

## Implementation Decisions

### Decisões fixadas

- **IdP**: Keycloak self-hosted (recomendado por custo) ou Auth0. SSO federado por Tenant via SAML/OIDC.
- **Postgres**: RLS ligada via `current_setting('app.tenant_id', true)` + middleware `SET LOCAL`. Lint de PR no CI bloqueia tabela sem `tenant_id NOT NULL` ou sem policy.
- **Redis**: Cluster real com `WAIT 1 100` em escritas críticas. Hash tag `{tenant_id}:{session_id}` (já existente da Fase 2).
- **Billing**: Stripe Billing usage-based; webhook `invoice.failed` aciona kill switch com 7 dias grace.
- **Kill switch**: granular por Tenant (`tenants.status ∈ {active, suspended, deleted}` + flag Redis).
- **Status page**: BetterStack (recomendado) ou Statuspage; SLO ≥ 99,5% mensal.
- **Feature flags**: OpenFeature SDK + Unleash self-hosted; flag-set por Tenant.
- **ICP-Brasil cloud (add-on)**: Soluti (Bird ID) como provider primário; Bry/Vidaas como fallback.
- **KYC (add-on)**: gov.br como primário (gratuito); provider comercial como fallback.
- **VPC dedicada (3.2)**: Terraform module por Tenant enterprise; provisionamento via API admin.

### Módulos novos por sub-fase

**Fase 3.0**:
- `TenantResolver` — middleware HTTP/WS extraindo `tenant_id` do JWT.
- `RateLimiter` por Tenant — token bucket; quota mensal; ramping na criação de Sessão.
- `ProfileCatalog` por Tenant — Tenant escolhe entre Perfis publicados + customização YAML via API admin com audit.
- `BillingExporter` — Stripe usage-based; agregação real-time em Redis Streams + batch horário.
- `KillSwitch` granular — tabela + flag Redis.
- `StatusPage` integration — BetterStack/Statuspage.

**Fase 3.1 (add-on)**:
- `ConsentService` — cláusulas opt-in versionadas.
- `IdentityService` — KYC gov.br + fallback comercial.
- `SignatureService` — ICP-Brasil cloud (Soluti) com assinatura por evento.
- `DSRPortal` — endpoints LGPD Art. 18 whitelabel.
- `RetentionPolicy` — dual: clínico 20 anos / não-clínico configurável.
- `EncryptionAtRest` envelope per-Tenant — master vira KEK.
- `AuditLog WORM` — S3 Object Lock COMPLIANCE como source-of-truth.

**Fase 3.2**:
- `EnterpriseTenant` provisioner — Terraform module por Tenant.
- `DBPerTenant` migration tooling.
- KMS dedicado por SKU.

### Contratos preservados

Tudo da Fase 2 (núcleo + novos paralelos) continua estável. Mudanças exigem nova ADR.

### Mudanças aditivas (não-breaking)

- Métricas e logs deixam de usar `tenant_id="default"` e passam a usar UUID real.
- `ProfileResolver(SessionContext)` agora resolve por `(tenant_id, session_kind) → Profile`.
- API HTTP ganha endpoints admin: `/v1/admin/tenants`, `/v1/admin/profiles`, `/v1/admin/usage`.

### Cobrança e pricing

- **Plano Básico**: R$ 0,15/min Sessão.
- **Plano Médio**: R$ 0,49/min Sessão.
- **Plano Premium**: R$ 1,29/min Sessão.
- **Add-on Suite Compliance**: R$ 1.500-2.500/mês por Tenant que ativa (preço fixo + variável por uso).
- **SKU Enterprise**: ≥ 3× preço shared; cotação caso a caso.

### SLOs por sub-fase

- **3.0**: 0 leakage cross-tenant em CI; fairness — Tenant N não degrada > 20% quando Tenant M satura; restore Postgres < 1h; DSR < 15 dias (assistente colabora; SLA é do Controlador exceto add-on).
- **3.1**: 100% das Sessões em Tenants com add-on com Consentimento granular; 100% dos Prontuários assinados com cadeia ICP-Brasil válida; KYC com cache + revalidação anual; verificação periódica de hash chain S3 sem gaps.
- **3.2**: VPC dedicada provisionada em < 4h; DB-per-tenant restore < 1h independente.

---

## Testing Decisions

### Testes novos

- **TenantResolver + RLS** (3.0): teste de penetração — query sem `SET LOCAL` falha; query com `tenant_id` errado retorna 0 linhas.
- **RateLimiter / Quota** (3.0): limite por Tenant; fairness sob saturação simultânea de Tenants.
- **BillingExporter** (3.0): eventos de uso emitidos em real-time; agregação batch coincide com soma; Stripe webhook handling idempotente.
- **ConsentService** (3.1): ausência de cláusula obrigatória bloqueia início; versão do termo registrada.
- **SignatureService** (3.1): assinatura por evento via Soluti mock; cadeia de assinaturas verificável; revogação por evento sem invalidar Prontuário inteiro.
- **DSRService** (3.1): SLA de 15 dias monitorado; respostas auditadas.

### Testes de carga

- **k6** com cenário multi-tenant: 5 Tenants × 10 Sessões cada × 30 min; fairness verificado.
- **Pen test cross-tenant** semanal no CI — falha bloqueia release.

### Prior art

- Estrutura de testes da Fase 2 preservada com prefixo de fase (`tests/fase3/test_issueNNN_...py`).

---

## Out of Scope (vai para Fase 4 ou posterior, ou nunca)

- **SKU on-prem completo** (A3 hospital-on-prem com Whisper + LLM 100% local na rede do hospital): roadmap separado se ≥ 3 Tenants enterprise solicitarem; modelo licença + suporte, não SaaS.
- **Multi-região ativa**: Fase 3 fica em `sa-east-1`; expansão multi-região é projeto separado.
- **TTS** (resposta em voz): possível em Fase 3.x quando WebRTC entrar; não prioritário.
- **Tradução automática multi-idioma**: produto é pt-BR.
- **Marketplace de Perfis** (Tenants publicarem Perfis para outros Tenants comprarem): ideia futura, não prioritária.
- **Diarização por modelo do provider STT em Sessão remota**: ainda não justifica custo; fica em diarização por roteamento (uma stream WS por Participante).

---

## Further Notes

### Documentos relacionados

- `PRD.md` — PRD do MVP.
- `PRD-fase2.md` — PRD do componente.
- `arquitetura-assistente-voz-mvp.md` — arquitetura completa.
- `docs/adr/0001-fase-2-telemedicina-saas.md` — ADR.
- `custos.md` — estimativas.
- `docs/glossario.md` — vocabulário canônico.

### Triggers que ativam esta fase

- **Fase 3.0**: decisão formal de vender o assistente separadamente; primeiro Tenant cliente prospectado.
- **Fase 3.1**: primeiro Tenant cliente sem app de telemedicina próprio que peça compliance estendida.
- **Fase 3.2**: ≥ 1 Tenant enterprise (hospital, rede, operadora) solicitar VPC dedicada ou DB-per-tenant.

### Estimativa de custo total (consultar `custos.md` para detalhes)

- Fase 3.0 inicial (5 Tenants externos): ~US$ 1.180-1.576/mês.
- Fase 3.1 add-on por Tenant que ativa: +US$ 437/mês marginal por Tenant; preço sugerido R$ 1.500-2.500 = margem ~80%.
- Fase 3.2 enterprise: caso a caso; preço ≥ 3× shared.

### Risco regulatório residual

Mesmo na Fase 3 completa, riscos residuais permanecem:
- ANPD pode emitir orientação alterando base legal aceita ou exigindo DPIA atualizada por Tenant.
- CFM pode publicar resolução nova; impacta principalmente Tenants com add-on Compliance.
- Transferência internacional de dados (DPA com providers): fiscalização ANPD pode exigir comprovação contratual recorrente.

### Crescimento de custo unitário com escala

Para 100 simultâneas: custo unitário cresce de US$ 0,55 (Premium 10 simultâneas) para ~US$ 1,89 conforme overhead fixo de DPO, auditoria, e infra dilui melhor em volume alto. Receita também cresce 10× — margem segue ~83%. Detalhes em `custos.md` Cenário F.
