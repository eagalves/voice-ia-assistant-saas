# Big Picture — Função de Cada Fase

## Objetivo Final do Produto
Entregar um assistente clínico de voz em pt-BR, integrado ao app de telemedicina, confiável em produção e pronto para escalar para modelo SaaS quando fizer sentido de negócio.

## Fase 1 (MVP local)
Função: validar o núcleo clínico e técnico do assistente (STT, LLM, fluxo de entrevista, red flags, sessão).

Resultado esperado:
- Domínio validado.
- Contratos centrais estáveis.
- Base pronta para evoluir sem reescrever o núcleo.

## Fase 2.0 (integração com o app)
Função: transformar o assistente em componente integrável no app de telemedicina (API HTTP + WebSocket).

Resultado esperado:
- Integração funcional ponta a ponta com o app.
- Sessão remota funcionando com transcrição e draft.
- Operação inicial com observabilidade, auditoria mínima e SLOs definidos.

## Fase 2.1 (produção robusta)
Função: endurecer operação para uso real com estabilidade, segurança e recuperação de desastre.

Resultado esperado:
- Hardening operacional (HA, DR, runbooks, carga recorrente, on-call).
- Governança de Operador LGPD operacionalizada (DPA, DPIA, processos).
- Confiabilidade de produção para pacientes reais.

## Fase 3.x (SaaS B2B multi-tenant)
Função: evoluir de componente interno para produto vendável para múltiplos clientes.

Resultado esperado:
- Multi-tenant operacional completo.
- Isolamento, billing, identidade e controles por tenant.
- SKUs avançadas (compliance/enterprise) conforme demanda comercial.

## Regra de Foco (para não perder a big picture)
- Agora: foco em Fase 2.0.
- Tudo que não desbloqueia integração real com o app fica para 2.1 ou 3.x.
- Qualquer mudança que ameace contratos do núcleo exige decisão explícita (ADR).
