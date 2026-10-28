# ADR 0006 — `ws_token` vinculado à Sessão/tenant; tenant do WebSocket resolvido sem header obrigatório

- **Status**: Aceito
- **Data**: 2026-10-27
- **Módulo**: `server/routers/audio.py`; PRD-fase2 "Auth"

## Contexto

O handshake do WebSocket validava apenas a existência do `ws_token` no Redis — um token válido de qualquer Sessão abria o endpoint de áudio de qualquer outra Sessão do mesmo tenant. Além disso o endpoint exigia o header `X-Tenant-Id`, que a API WebSocket dos navegadores não permite enviar; o consumidor real (app de telemedicina no browser) não conseguiria conectar.

## Decisão

O registro do token no Redis (`ws:token:{tenant}:{token}` → `{session_id, tenant_id, plan, expires_at}`) é a fonte de verdade: o handshake exige `session_id` e `tenant_id` do registro iguais aos da conexão, senão fecha com `4001`. O tenant é resolvido nesta ordem: query `tenant_id` → header `X-Tenant-Id` (servidor-para-servidor) → tenant default configurado (`DEFAULT_TENANT_ID`, Fase 2.0 single-tenant). `expires_at` do registro alimenta o aviso `token_expiring_in`.

## Alternativas rejeitadas

- **Token no subprotocolo `Sec-WebSocket-Protocol`**: funciona no browser mas é hack semântico e não resolve o tenant; fica como opção se o token na query string virar problema de logs (hoje mitigado: logs de acesso não devem registrar query strings do `/audio`).
- **Derivar o tenant apenas do registro do token**: ideal para a Fase 3, mas a chave Redis carrega `{tenant_id}` como hash tag (invariante da Issue #10), então é preciso conhecer o tenant antes do lookup.

## Consequências

- Na Fase 3.0 (multi-tenant operacional) o tenant default deixa de ser aceitável e a query `tenant_id` passa a ser obrigatória — mudança de contrato a declarar no PRD da Fase 3.
- O token continua opaco e com TTL igual ao cap do plano; o refresh nunca estende além de `min(created_at + session_max_minutes, auto_finalize_at)`.
