# ADR 0007 — Auto-finalize de Sessão como tarefa in-process em cada container, sem scheduler externo

- **Status**: Aceito
- **Data**: 2026-10-27
- **Módulos**: `server/reaper.py`, `server/session_lifecycle.py`

## Contexto

O PRD fixa hard cap de 4h e auto-finalize em 6h, mas `auto_finalize_at` era apenas gravado: uma Sessão cujo app caísse sem chamar `/close` ficava aberta para sempre, com token válido e sem draft. O fechamento precisa executar exatamente a mesma transação do `/close` (revogar token, `EXPIRE` do stream, draft `pending`, AuditLog) e disparar a materialização.

## Decisão

Um `SessionReaper` roda como tarefa do lifespan em **todo** container (`SESSION_REAPER_INTERVAL_S`, padrão 60 s; `0` desliga). A cada ciclo ele lê Sessões abertas vencidas (única leitura cross-tenant sancionada, `SystemSessionsRepository`), relê cada uma `FOR UPDATE` pelo repositório do próprio tenant e aplica `close_session_tx` — a mesma função usada pelo router — com `actor_sub=system:reaper` e evento `session.auto_finalized`. Row lock + recheck de status tornam reapers concorrentes idempotentes.

## Alternativas rejeitadas

- **`pg_cron` / ECS scheduled task**: a lógica de fechamento envolve Redis e AuditLog, não só SQL; um job externo duplicaria o código do `/close` ou precisaria chamar a API com credenciais de sistema.
- **Lazy-close no próximo acesso à Sessão**: não cobre Sessões que ninguém mais consulta — justamente o caso de falha.

## Consequências

- Cada container faz uma consulta leve por minuto; aceitável em 2.x. Se o número de containers crescer, basta eleger um via lock Redis sem mudar o contrato.
- Testes com `TestClient` em modo lifespan usam `SESSION_REAPER_INTERVAL_S=0` ou toleram o primeiro ciclo (que só ocorre após o intervalo).
