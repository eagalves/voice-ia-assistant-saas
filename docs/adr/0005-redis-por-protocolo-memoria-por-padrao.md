# ADR 0005 — Redis atrás do protocolo `EventStreamClient`; em memória por padrão, Redis real por `REDIS_URL`

- **Status**: Aceito
- **Data**: 2026-10-27
- **Módulos**: `server/event_stream.py`, `server/stream_client.py`

## Contexto

Toda a camada de Sessão da Fase 2 (stream de eventos, snapshots, `ws_token`, revogação pub/sub) foi escrita contra um protocolo mínimo de Redis e testada com um `FakeEventStream` em memória. Até esta revisão não existia implementação real: em produção o servidor subia com o fake, o `MaterializationWorker` abria um fake *novo* (vazio) e o watcher de revogação lia um atributo privado do fake — ou seja, o componente "funcionava" apenas em testes.

## Decisão

Manter o protocolo `EventStreamClient` (xadd/xrange/xrevrange/xlen/set/get/expire/delete/publish/subscribe) como única fronteira com o Redis e fornecer duas implementações: `FakeEventStream` (padrão quando `REDIS_URL` está vazio — desenvolvimento single-process e testes) e `RedisEventStream` (`redis.asyncio`, `decode_responses=True`, selecionado por `REDIS_URL`). O watcher de revogação usa `subscribe` do protocolo em ambas. Um teste de conformidade do adaptador real roda com o marker `redis_real` quando `TEST_REDIS_URL` está definido; o CI padrão continua sem Redis.

## Alternativas rejeitadas

- **Exigir Redis sempre (sem fake)**: encareceria cada teste de router e o Voice CLI de desenvolvimento, sem ganho de cobertura para a lógica de Sessão.
- **`fakeredis` como fake**: adiciona dependência e API maior que o protocolo; o fake próprio tem 150 linhas e espelha só o que usamos.

## Consequências

- Sem `REDIS_URL` não há durabilidade nem visibilidade entre containers: reconexão só funciona no mesmo processo. O `/health` expõe `event_stream: memory|redis` e o boot loga um aviso.
- O `FakeEventStream` precisa acompanhar cada novo método do protocolo (ex.: `subscribe`); o teste `redis_real` é o contrato que impede divergência semântica entre os dois.
